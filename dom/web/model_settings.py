"""Owner settings JSON contract: models, providers and permissions.

GET /api/models returns ModelSettings. POST /api/models replaces one ModelUpdate
role and returns ModelSettings. POST /api/models/test takes ProviderTest and
returns TestResult. Index 0 is the role's primary, followed by its own fallbacks.
For search, omitting index (null) tests the saved chain in order and reports
which provider answered. GET /api/permissions returns Permissions; POST
/api/permissions sets one PermissionUpdate and returns Permissions. Every POST
takes the GET revision (a hash of config.toml, shared by both GETs) so a stale
page cannot modify or test a moved slot.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypeVar, get_args
from urllib.parse import urlsplit

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dom.config import (
    MODEL_ROLES,
    SEARCH_PROVIDERS,
    Config,
    ModelRole,
    ProviderEndpoint,
    SearchProvider,
    _replace_file,
    validate_config,
)
from dom.doctor import _probe_hint, is_zdr_refusal
from dom.llm import ChatClient, FallbackClient, MissingProviderKey
from dom.search import PROVIDER_INFO, SearchClient, search_provider_kind
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN
from dom.usage import ModelCallSkipped, model_request, on_own_network, provider_address, unroutable_address
from dom.web.pages import _bounded_stream, owner_app, panel

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
API_PATH = "/api/models"
PERMISSIONS_PATH = "/api/permissions"
JSON_POSTS = {API_PATH, API_PATH + "/test", PERMISSIONS_PATH}
MAX_BODY = 32 * 1024
MAX_FALLBACKS = 5
URL_RULE = ("Use an HTTPS API base URL without credentials, a query or a fragment. "
            "HTTP is accepted only for localhost, a private address or a local network name.")
TEST_TIMEOUT = 8
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
REVISION = r"^[a-f0-9]{64}$"
CREDENTIAL_PATTERNS = (API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN)


def _looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in CREDENTIAL_PATTERNS)


def _validate_url(value: str) -> str:
    if not value or any(char.isspace() or ord(char) < 32 or char in "\\?#" for char in value):
        raise ValueError(URL_RULE)
    try:
        parts = urlsplit(value)
        port = parts.port
        parsed = httpx.URL(value)
        if (
            parts.scheme in ("http", "https") and parts.hostname and parsed.host
            and parts.username is None and "%" not in parts.netloc and not parts.netloc.endswith(":")
            and (port is None or port > 0) and not _looks_like_secret(value)
            and not unroutable_address(parsed.host)
            and (parts.scheme == "https" or on_own_network(parsed.host))
        ):
            return value
    except (ValueError, httpx.InvalidURL):
        pass
    raise ValueError(URL_RULE)


class ProviderInput(ProviderEndpoint):
    model_config = ConfigDict(extra="forbid")

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if any(char.isspace() or not char.isprintable() for char in value) or _looks_like_secret(value):
            raise ValueError("Enter a model ID without spaces or credentials.")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _key_env(cls, value: str | None) -> str | None:
        if value is not None and (not ENV_NAME.fullmatch(value) or _looks_like_secret(value)):
            raise ValueError("Enter an environment variable name or leave it unset.")
        return value


def _validate_slot_for_role(role: ModelRole, slot: ProviderInput) -> ProviderInput:
    if role != "search":
        if slot.provider is not None:
            raise ValueError("Only the search role accepts a search provider type.")
        if not slot.model:
            raise ValueError("Enter a model ID without spaces or credentials.")
        return slot
    kind = search_provider_kind(slot)
    if slot.reasoning_effort is not None:
        raise ValueError("Search providers do not take a reasoning effort.")
    if kind == "openrouter":
        if not slot.model:
            raise ValueError("Enter a model ID without spaces or credentials.")
    else:
        if slot.model:
            raise ValueError("Leave model ID empty for SearXNG, DeepSeek web search and Tavily.")
    if kind in ("deepseek", "tavily") and not slot.api_key_env:
        raise ValueError("Enter an API key variable for this cloud search provider.")
    return slot.model_copy(update={"provider": kind})


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    role: ModelRole
    primary: ProviderInput | None
    fallbacks: list[ProviderInput] = Field(max_length=MAX_FALLBACKS)

    @model_validator(mode="after")
    def _validate_role_slots(self) -> ModelUpdate:
        if self.primary is None and (self.role not in ("fallback", "search", "escalation") or self.fallbacks):
            raise ValueError("Only fallback, search and escalation may be left empty.")
        if self.primary is not None:
            self.primary = _validate_slot_for_role(self.role, self.primary)
        self.fallbacks = [_validate_slot_for_role(self.role, provider) for provider in self.fallbacks]
        return self


class KeyPairing(ValueError):
    """A key variable would be sent to a host it was never paired with."""


def _address(url: str) -> str | None:
    """Scheme, host and port: a key paired with an https endpoint doesn't follow it to http on the same host."""
    try:
        parsed = httpx.URL(url)
        return f"{parsed.scheme}://{provider_address(parsed)}"
    except httpx.InvalidURL:
        return None


def _check_keys(disk: Config, providers: list[ProviderEndpoint]) -> None:
    """A key variable may go only to the scheme://host:port that config.toml already pairs it with (so triage's
    key is allowed on its paired host such as https://openrouter.ai, but not elsewhere). A new name may be used
    for one host, unless it already holds a value, names a non-model instance credential (the bot token, desktop
    keys, KLIPY_API_KEY, or an unpaired JEV_API_KEY), or belongs to another host. Pairing an existing key with a
    new host stays a hand edit of config.toml."""
    paired: dict[str, set[str | None]] = {}
    for _, endpoint in disk.model_endpoints():
        if endpoint.api_key_env:
            paired.setdefault(endpoint.api_key_env, set()).add(_address(endpoint.base_url))
    instance = disk.model_copy(update={"models": {}}).secret_names()
    claimed: dict[str, set[str | None]] = {}
    for provider in providers:
        name, address = provider.api_key_env, _address(provider.base_url)
        if not name or address in paired.get(name, ()):
            continue
        if name in paired:
            raise KeyPairing(
                "That key variable is already used for a different host. To pair an existing key with a new host, "
                "edit config.toml by hand.")
        if name in instance:
            raise KeyPairing(
                "That key variable holds another credential and cannot be used for a provider here. "
                "To use it anyway, edit config.toml by hand.")
        if disk.secret(name):  # a variable that already holds a value never gains a host here (no laundering via remove + re-add)
            raise KeyPairing(
                "That key variable already holds a value, so it can't be pointed at a new host from the dashboard. "
                "Use a new variable name and add its value to .env afterwards, or edit config.toml by hand.")
        if len(claimed.setdefault(name, set()) | {address}) > 1:
            raise KeyPairing("A new key variable can be used for one host only.")
        claimed[name].add(address)


class SearchProviderInfo(BaseModel):
    id: SearchProvider
    label: str
    default_base_url: str
    default_api_key_env: str
    needs_model: bool
    needs_key: bool
    data_retention: str
    policy_url: str | None = None
    policy_label: str | None = None


class ProviderView(ProviderEndpoint):
    kind: Literal["local", "cloud"]
    key_set: bool
    data_retention: str | None = None
    policy_url: str | None = None
    policy_label: str | None = None


class TriageView(BaseModel):
    """Jev, the TypeSafe Decisions API: a classifier, shown here but not routable to a chat model."""

    model: str
    base_url: str
    key_set: bool


class RoleView(BaseModel):
    primary: ProviderView | None
    fallbacks: list[ProviderView]


class ModelSettings(BaseModel):
    revision: str
    roles: dict[ModelRole, RoleView]
    triage: TriageView
    search_providers: list[SearchProviderInfo]
    restart_required: bool


class ProviderTest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    role: ModelRole
    index: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _index_required_except_search(self) -> ProviderTest:
        if self.role != "search" and self.index is None:
            raise ValueError("Choose a provider slot to test.")
        return self


class TestResult(BaseModel):
    ok: bool
    latency_ms: int
    reply: str | None = None
    error: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class Snapshot:
    config: Config
    text: str

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _snapshot(home: Path) -> Snapshot:
    try:
        text = (home / "config.toml").read_bytes().decode("utf-8")
        return Snapshot(validate_config(home, tomllib.loads(text)), text)
    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(409, "Fix config.toml before editing model settings.") from None


def _check_revision(current: Snapshot, revision: str) -> None:
    if current.revision != revision:
        raise HTTPException(409, "Configuration changed. Reload model settings and try again.")


def _dump_slot(role: ModelRole, provider: ProviderInput) -> dict:
    data = provider.model_dump(exclude_none=True)
    if role == "search" and search_provider_kind(provider) != "openrouter":
        data.pop("model", None)
    return data


def _save(current: Snapshot, update: ModelUpdate) -> Snapshot:
    _check_revision(current, update.revision)
    try:
        _check_keys(current.config, [*([update.primary] if update.primary else []), *update.fallbacks])
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    home = current.config.home
    document = tomlkit.parse(current.text)

    def models():
        # A fresh view per step: when other tables sit between the [models.*] ones, tomlkit's view of them goes stale
        # after a write (it forgets a key it just inserted), so one view must never span two writes.
        return document["models"]

    if "chat_fallback" in models():
        legacy = models().pop("chat_fallback")
        if update.role != "fallback" and "fallback" not in models():
            models()["fallback"] = legacy
    if update.primary is None:
        models().pop(update.role, None)
    else:
        primary = _dump_slot(update.role, update.primary)
        if update.fallbacks:
            primary["fallbacks"] = [_dump_slot(update.role, provider) for provider in update.fallbacks]
        models()[update.role] = primary

    text = tomlkit.dumps(document)
    saved = Snapshot(validate_config(home, tomllib.loads(text)), text)
    if "chat" not in saved.config.models:
        raise HTTPException(422, "Configure a primary chat provider.")
    _write(current, saved)
    return saved


def _write(current: Snapshot, saved: Snapshot) -> None:
    """Replace config.toml with a validated snapshot, keeping the first original and the last version beside it."""
    home = current.config.home
    path = home / "config.toml"
    if saved.text != current.text:
        if path.read_bytes() != current.text.encode("utf-8"):
            raise HTTPException(409, "Configuration changed. Reload settings and try again.")
        if not (home / "config.toml.orig").exists():  # the file as it was before the first save from here
            _replace_file(home / "config.toml.orig", current.text, mode=0o600)
        _replace_file(home / "config.toml.bak", current.text, mode=0o600)
        _replace_file(path, saved.text)


def _provider_view(config: Config, endpoint: ProviderEndpoint, *, role: str = "") -> ProviderView:
    # Older hand-written files may include a credential where the settings API expects a name or URL.
    try:
        url = _validate_url(endpoint.base_url)
    except ValueError:
        url = ""
    host = httpx.URL(url).host if url else ""
    variable = endpoint.api_key_env or None
    if variable and (not ENV_NAME.fullmatch(variable) or _looks_like_secret(variable)):
        variable = None
    try:
        key_set = bool(variable and config.secret(variable))
    except (OSError, ValueError):
        key_set = False
    search_kind = search_provider_kind(endpoint) if role == "search" else None
    info = PROVIDER_INFO[search_kind] if search_kind else None
    return ProviderView(
        base_url=url,
        model="" if _looks_like_secret(endpoint.model) else endpoint.model,
        api_key_env=variable,
        key_set=key_set,
        kind="local" if host and on_own_network(host) else "cloud",
        reasoning_effort=endpoint.reasoning_effort,
        provider=search_kind,
        data_retention=info.data_retention if info else None,
        policy_url=info.policy_url if info else None,
        policy_label=info.policy_label if info else None,
    )


def _search_provider_infos() -> list[SearchProviderInfo]:
    return [
        SearchProviderInfo(
            id=info.id,
            label=info.label,
            default_base_url=info.default_base_url,
            default_api_key_env=info.default_api_key_env,
            needs_model=info.needs_model,
            needs_key=info.needs_key,
            data_retention=info.data_retention,
            policy_url=info.policy_url,
            policy_label=info.policy_label,
        )
        for info in (PROVIDER_INFO[name] for name in SEARCH_PROVIDERS)
    ]


def _settings(app: App, snapshot: Snapshot) -> ModelSettings:
    config = snapshot.config
    roles = {}
    for role in MODEL_ROLES:
        primary, *fallbacks = config.providers(role) or (None,)
        roles[role] = RoleView(
            primary=_provider_view(config, primary, role=role) if primary else None,
            fallbacks=[_provider_view(config, provider, role=role) for provider in fallbacks],
        )
    triage = _provider_view(config, config.models["triage"])
    return ModelSettings(
        revision=snapshot.revision,
        roles=roles,
        triage=TriageView(model=triage.model, base_url=triage.base_url, key_set=triage.key_set),
        search_providers=_search_provider_infos(),
        restart_required=any(config.providers(role) != app.config.providers(role) for role in MODEL_ROLES),
    )


Input = TypeVar("Input", bound=BaseModel)


def is_json(request: Request) -> bool:
    return request.headers.get("Content-Type", "").partition(";")[0].strip().lower() == "application/json"


async def _input(request: Request, model: type[Input]) -> Input:
    if not is_json(request):
        raise HTTPException(415, "Expected application/json.")
    body = bytearray()
    async for chunk in _bounded_stream(request, MAX_BODY):
        body.extend(chunk)
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        # Never echo submitted values or unknown field names in validation errors.
        if any(error["type"] == "extra_forbidden" for error in exc.errors(include_input=False)):
            detail = "Unexpected fields. Use only the documented settings fields."
        else:
            error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
            detail = error["msg"]
            if error["type"] == "literal_error":
                detail = "Choose a supported role, search provider or reasoning effort."
        raise HTTPException(422, detail) from None


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    app = owner_app(request)
    settings = _settings(app, _snapshot(app.config.home))
    return panel(request, "settings", settings=settings)


@router.get(API_PATH)
async def read_models(request: Request) -> ModelSettings:
    app = owner_app(request)
    return _settings(app, _snapshot(app.config.home))


def _probe_tuple(role: ModelRole, endpoint: ProviderEndpoint) -> tuple[str, str, str | None, str | None]:
    kind = search_provider_kind(endpoint) if role == "search" else None
    return (endpoint.base_url, endpoint.model, endpoint.api_key_env or None, kind)


MISSING_KEY = "Not tested: this provider's key variable has no value in .env yet. Add it, then press Test."


def _slot_label(index: int) -> str:
    return "Primary" if index == 0 else f"Fallback {index}"


@router.post(API_PATH)
async def update_models(request: Request):
    app = owner_app(request)
    update = await _input(request, ModelUpdate)
    current = _snapshot(app.config.home)
    _check_revision(current, update.revision)
    submitted = [*([update.primary] if update.primary else []), *update.fallbacks]
    try:
        _check_keys(current.config, submitted)
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None

    # A slot already in the chain (at any position) was tested when it was added; the probe ignores reasoning effort,
    # so a change to that alone tests nothing new either.
    existing = {_probe_tuple(update.role, provider) for provider in current.config.providers(update.role)}
    to_test = [
        (index, provider)
        for index, provider in enumerate(submitted)
        if _probe_tuple(update.role, provider) not in existing
    ]
    if to_test:
        current_client = app.client.primary if isinstance(app.client, FallbackClient) else app.client
        results = await asyncio.gather(
            *(_probe(app, current_client.http, current.config, provider, role=update.role) for _, provider in to_test)
        )
        failures = [
            {"index": index, "error": result.error or "Provider test failed."}
            for (index, _), result in zip(to_test, results)
            if not result.ok and result.error != MISSING_KEY  # a new key variable is filled in after saving (see _check_keys)
        ]
        if failures:
            detail = "; ".join(f"{_slot_label(item['index'])}: {item['error']}" for item in failures)
            return JSONResponse(status_code=422, content={"detail": detail, "errors": failures})

    # No await between reading, replacing the file, and publishing the new runtime snapshot.
    try:
        saved = _save(_snapshot(app.config.home), update)
    except (OSError, ValueError):
        raise HTTPException(409, "Could not save valid model settings. Check config.toml and its permissions.") from None
    try:
        app.apply_model_routes(saved.config)
    except (OSError, ValueError, httpx.InvalidURL):
        pass  # the response reports whether the running routes still differ
    return _settings(app, saved)


@router.post(API_PATH + "/test")
async def test_provider(request: Request) -> TestResult:
    app = owner_app(request)
    test = await _input(request, ProviderTest)
    snapshot = _snapshot(app.config.home)
    _check_revision(snapshot, test.revision)
    providers = snapshot.config.providers(test.role)
    if not providers and test.role == "escalation":
        providers = snapshot.config.providers("drafting")
    current = app.client.primary if isinstance(app.client, FallbackClient) else app.client
    if test.role == "search" and test.index is None:
        if not providers:
            raise HTTPException(404, "There are no saved search providers to test.")
        return await _probe_search_chain(app, current.http, snapshot.config, providers)
    assert test.index is not None
    if test.index >= len(providers):
        raise HTTPException(404, "There is no saved provider at that position.")
    provider = providers[test.index]
    return await _probe(app, current.http, snapshot.config, provider, role=test.role)


@dataclass(frozen=True)
class Permission:
    id: str
    path: tuple[str, ...]  # its key in config.toml
    label: str
    description: str


# What Dom may do: switches the owner flips from the dashboard. Each is read from app.config on every use, so a save
# applies at once, without a restart.
PERMISSIONS = (
    Permission("memory_self_edit", ("memory_self_edit",), "Let Dom edit his own memory",
               "Dom proposes each change to his notes in your DM; nothing changes until you approve it."),
    Permission("openers", ("openers", "enabled"), "Start conversations",
               "Dom may start a conversation in your DM, a few times a day at most."),
    Permission("ephemeral_replies", ("ephemeral_replies",), "Private replies in groups",
               "Dom may answer you privately inside a group where he is an admin, or in your DM where he is not."),
    Permission("leak_check", ("leak_check",), "Check replies for leaks",
               "Jev screens what Dom sends and what network plugins send for private information first."),
)
PermissionId = Literal["memory_self_edit", "openers", "ephemeral_replies", "leak_check"]
assert set(get_args(PermissionId)) == {item.id for item in PERMISSIONS}


class PermissionView(BaseModel):
    id: PermissionId
    label: str
    description: str
    enabled: bool


class Permissions(BaseModel):
    revision: str
    items: list[PermissionView]


class PermissionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    id: PermissionId
    enabled: bool


def _permission_value(config: Config, permission: Permission) -> bool:
    value = config
    for key in permission.path:
        value = getattr(value, key)
    return bool(value)


def _permissions(snapshot: Snapshot) -> Permissions:
    return Permissions(revision=snapshot.revision, items=[
        PermissionView(id=item.id, label=item.label, description=item.description,
                       enabled=_permission_value(snapshot.config, item))
        for item in PERMISSIONS
    ])


def _with_permission(config: Config, path: tuple[str, ...], enabled: bool) -> Config:
    """A copy of the running config with one switch changed; everything else stays as it runs now."""
    key, *rest = path
    if not rest:
        return config.model_copy(update={key: enabled})
    return config.model_copy(update={key: _with_permission(getattr(config, key), tuple(rest), enabled)})


@router.get(PERMISSIONS_PATH)
async def read_permissions(request: Request) -> Permissions:
    app = owner_app(request)
    return _permissions(_snapshot(app.config.home))


@router.post(PERMISSIONS_PATH)
async def update_permission(request: Request) -> Permissions:
    app = owner_app(request)
    update = await _input(request, PermissionUpdate)
    current = _snapshot(app.config.home)
    _check_revision(current, update.revision)
    permission = next(item for item in PERMISSIONS if item.id == update.id)
    document = tomlkit.parse(current.text)
    table = document
    *parents, key = permission.path
    for name in parents:
        if name not in table:
            table[name] = tomlkit.table()
        table = table[name]
    value = tomlkit.item(update.enabled)
    if key in table and hasattr(table[key], "trivia"):  # keep the comment beside the value
        value.trivia.comment_ws = table[key].trivia.comment_ws
        value.trivia.comment = table[key].trivia.comment
    table[key] = value
    text = tomlkit.dumps(document)
    try:
        saved = Snapshot(validate_config(current.config.home, tomllib.loads(text)), text)
        _write(current, saved)
    except (OSError, ValueError):
        raise HTTPException(409, "Could not save the setting. Check config.toml and its permissions.") from None
    app.config = _with_permission(app.config, permission.path, update.enabled)
    return _permissions(saved)


def _redact(text: str, key: str | None) -> str:
    if key:
        text = text.replace(key, "[redacted]")
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _is_local(base_url: str) -> bool:
    try:
        host = httpx.URL(base_url).host
    except httpx.InvalidURL:
        return False
    return bool(host and on_own_network(host))


async def _probe_search(
    app: App, http: httpx.AsyncClient, config: Config, provider: ProviderEndpoint, *, fallback: bool = False,
) -> TestResult:
    local = _is_local(provider.base_url)

    async def allowed() -> bool:  # a local endpoint costs nothing, so the spend cap never blocks testing or saving it
        return local or not await app.dispatch.usage.over_cap(app.config, app.bot, sleep=app.sleep)

    kind = search_provider_kind(provider)
    start = monotonic()
    try:
        validated = _validate_slot_for_role(
            "search",
            ProviderInput(
                base_url=provider.base_url,
                model=provider.model,
                api_key_env=provider.api_key_env or None,
                reasoning_effort=provider.reasoning_effort,
                provider=provider.provider,
            ),
        )
        _check_keys(config, [validated])
        key = config.secret(validated.api_key_env) if validated.api_key_env else None
        client = SearchClient(validated, key, http, usage=app.dispatch.usage)
        async with asyncio.timeout(TEST_TIMEOUT):
            with model_request("search", None, allowed):
                outcome = await client.search_with_meta("test", fallback=fallback, timeout=TEST_TIMEOUT)
        detail = (
            f"Answered by {outcome.label} ({outcome.result_count} results)"
            if outcome.result_count is not None
            else f"Answered by {outcome.label}"
        )
        reply = _redact(detail, key)
    except KeyPairing as exc:
        error = str(exc)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, ValidationError) and "SearXNG" in str(exc):
            error = "SearXNG did not return valid JSON results. Check that format=json is enabled."
        elif isinstance(exc, ValidationError):
            error = "Fix this provider's URL, model or key variable in settings."
        else:
            error = "Provider did not return valid search results."
    except ModelCallSkipped:
        error = "The daily spend cap has been reached."
    except MissingProviderKey:
        error = MISSING_KEY
    except (TimeoutError, httpx.TimeoutException):
        error = "Provider test timed out."
    except httpx.HTTPStatusError as exc:
        if kind == "searxng" and exc.response.status_code == 403:
            error = "SearXNG returned HTTP 403. Enable JSON format (formats: [html, json]) in SearXNG settings.yml."
        elif is_zdr_refusal(exc.response):
            error = _probe_hint(exc.response)
        else:
            error = f"Provider returned HTTP {exc.response.status_code}."
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        error = "Could not reach the provider."
    except (KeyError, IndexError, TypeError):
        error = "Provider did not return valid search results."
    else:
        return TestResult(
            ok=True,
            latency_ms=round((monotonic() - start) * 1000),
            reply=reply[:2000],
            provider=outcome.provider,
        )
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=error, provider=kind)


async def _probe_search_chain(
    app: App, http: httpx.AsyncClient, config: Config, providers: tuple[ProviderEndpoint, ...],
) -> TestResult:
    start = monotonic()
    last: TestResult | None = None
    for index, provider in enumerate(providers):
        result = await _probe_search(app, http, config, provider, fallback=index > 0)
        if result.ok:
            slot = _slot_label(index)
            reply = f"{result.reply} ({slot})" if result.reply else f"Answered ({slot})"
            return TestResult(
                ok=True,
                latency_ms=round((monotonic() - start) * 1000),
                reply=reply[:2000],
                provider=result.provider,
            )
        last = result
        if result.error == "The daily spend cap has been reached.":
            break
    return TestResult(
        ok=False,
        latency_ms=round((monotonic() - start) * 1000),
        error=last.error if last else "No search provider answered.",
        provider=last.provider if last else None,
    )


async def _probe(
    app: App, http: httpx.AsyncClient, config: Config, provider: ProviderEndpoint, *, role: ModelRole = "chat",
) -> TestResult:
    if role == "search":
        return await _probe_search(app, http, config, provider)
    local = _is_local(provider.base_url)
    escalation_owner = app.owner_id if role == "escalation" else None

    async def allowed() -> bool:  # a local endpoint costs nothing, so the spend cap never blocks testing or saving it
        if escalation_owner is not None and not await app.dispatch.model_allowed(escalation_owner):
            return False
        return local or not await app.dispatch.usage.over_cap(app.config, app.bot, sleep=app.sleep)

    start = monotonic()
    try:
        _validate_slot_for_role(
            role,
            ProviderInput(
                base_url=provider.base_url, model=provider.model, api_key_env=provider.api_key_env or None,
                reasoning_effort=provider.reasoning_effort,
            ),
        )
        _check_keys(config, [provider])
        key = config.secret(provider.api_key_env) if provider.api_key_env else None
        probe_endpoint = provider.model_copy(update={"reasoning_effort": None})
        kind = "escalation" if role == "escalation" else "chat"
        client = ChatClient(probe_endpoint, key, http, sleep=app.sleep, usage=app.dispatch.usage, kind=kind)
        async with asyncio.timeout(TEST_TIMEOUT):
            with model_request(kind, escalation_owner, allowed):
                message = await client.complete(
                    [{"role": "user", "content": "Reply with OK."}], [],
                    once=True, max_tokens=256, timeout=TEST_TIMEOUT,
                )
        if not isinstance(message, dict):
            raise ValueError
        content = message.get("content")
        reply = _redact(content if isinstance(content, str) and content.strip() else "OK", key)
    except KeyPairing as exc:
        error = str(exc)
    except (ValidationError, ValueError) as exc:
        error = (
            "Fix this provider's URL, model or key variable in settings."
            if isinstance(exc, ValidationError) or "model ID" in str(exc) or "search provider" in str(exc)
            else "Provider did not return a valid text reply."
        )
    except ModelCallSkipped:
        error = "The daily spend cap has been reached."
    except MissingProviderKey:
        error = MISSING_KEY
    except (TimeoutError, httpx.TimeoutException):
        error = "Provider test timed out."
    except httpx.HTTPStatusError as exc:
        error = (
            _probe_hint(exc.response)
            if is_zdr_refusal(exc.response)
            else f"Provider returned HTTP {exc.response.status_code}."
        )
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        error = "Could not reach the provider."
    except (KeyError, IndexError, TypeError):
        error = "Provider did not return a valid text reply."
    else:
        return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=reply[:2000])
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=error)
