"""Owner model settings JSON contract.

GET /api/models returns ModelSettings. POST /api/models replaces one ModelUpdate
role and returns ModelSettings. POST /api/models/test takes ProviderTest and
returns TestResult. Index 0 is the role's primary, followed by its own fallbacks.
Both POSTs take the GET revision so a stale page cannot modify or test a moved slot.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypeVar
from urllib.parse import urlsplit

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dom.config import MODEL_ROLES, Config, ModelRole, ProviderEndpoint, _replace_file, validate_config
from dom.llm import ChatClient, FallbackClient, MissingProviderKey
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN
from dom.usage import ModelCallSkipped, model_request, on_own_network, provider_address, unroutable_address
from dom.web.pages import _bounded_stream, owner_app, panel

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
API_PATH = "/api/models"
JSON_POSTS = {API_PATH, API_PATH + "/test"}
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
        if not value or any(char.isspace() or not char.isprintable() for char in value) or _looks_like_secret(value):
            raise ValueError("Enter a model ID without spaces or credentials.")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _key_env(cls, value: str | None) -> str | None:
        if value is not None and (not ENV_NAME.fullmatch(value) or _looks_like_secret(value)):
            raise ValueError("Enter an environment variable name or leave it unset.")
        return value


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    role: ModelRole
    primary: ProviderInput | None
    fallbacks: list[ProviderInput] = Field(max_length=MAX_FALLBACKS)

    @model_validator(mode="after")
    def _primary_required(self) -> ModelUpdate:
        if self.primary is None and (self.role != "fallback" or self.fallbacks):
            raise ValueError("Only the fallback role may be left empty.")
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
    """A key variable may go only to the host config.toml already pairs it with. A new name may be used for one
    host, unless it names an instance credential (the bot token, Jev's key, a desktop key...) or another host's key.
    Pairing an existing key with a new host stays a hand edit of config.toml."""
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


class ProviderView(ProviderEndpoint):
    kind: Literal["local", "cloud"]
    key_set: bool


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
    restart_required: bool


class ProviderTest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    role: ModelRole
    index: int = Field(ge=0)


class TestResult(BaseModel):
    ok: bool
    latency_ms: int
    reply: str | None = None
    error: str | None = None


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


def _save(current: Snapshot, update: ModelUpdate) -> Snapshot:
    _check_revision(current, update.revision)
    try:
        _check_keys(current.config, [*([update.primary] if update.primary else []), *update.fallbacks])
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    home = current.config.home
    path = home / "config.toml"
    document = tomlkit.parse(current.text)
    models = document["models"]
    if "chat_fallback" in models:
        legacy = models.pop("chat_fallback")
        if "fallback" not in models:
            models["fallback"] = legacy
    if update.primary is None:
        models.pop(update.role, None)
    else:
        primary = update.primary.model_dump(exclude_none=True)
        if update.fallbacks:
            primary["fallbacks"] = [provider.model_dump(exclude_none=True) for provider in update.fallbacks]
        models[update.role] = primary

    text = tomlkit.dumps(document)
    saved = Snapshot(validate_config(home, tomllib.loads(text)), text)
    if "chat" not in saved.config.models:
        raise HTTPException(422, "Configure a primary chat provider.")
    if text != current.text:
        if path.read_bytes() != current.text.encode("utf-8"):
            raise HTTPException(409, "Configuration changed. Reload model settings and try again.")
        if not (home / "config.toml.orig").exists():  # the file as it was before the first save from here
            _replace_file(home / "config.toml.orig", current.text, mode=0o600)
        _replace_file(home / "config.toml.bak", current.text, mode=0o600)
        _replace_file(path, text)
    return saved


def _provider_view(config: Config, endpoint: ProviderEndpoint) -> ProviderView:
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
    return ProviderView(
        base_url=url, model="" if _looks_like_secret(endpoint.model) else endpoint.model,
        api_key_env=variable, key_set=key_set, kind="local" if host and on_own_network(host) else "cloud",
        reasoning_effort=endpoint.reasoning_effort,
    )


def _settings(app: App, snapshot: Snapshot) -> ModelSettings:
    config = snapshot.config
    roles = {}
    for role in MODEL_ROLES:
        primary, *fallbacks = config.providers(role) or (None,)
        roles[role] = RoleView(
            primary=_provider_view(config, primary) if primary else None,
            fallbacks=[_provider_view(config, provider) for provider in fallbacks],
        )
    triage = _provider_view(config, config.models["triage"])
    return ModelSettings(
        revision=snapshot.revision, roles=roles,
        triage=TriageView(model=triage.model, base_url=triage.base_url, key_set=triage.key_set),
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
                detail = "Choose a supported role or reasoning effort."
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


@router.post(API_PATH)
async def update_models(request: Request) -> ModelSettings:
    app = owner_app(request)
    update = await _input(request, ModelUpdate)
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
    if test.index >= len(providers):
        raise HTTPException(404, "There is no saved provider at that position.")
    provider = providers[test.index]
    current = app.client.primary if isinstance(app.client, FallbackClient) else app.client
    return await _probe(app, current.http, snapshot.config, provider)


async def _probe(app: App, http: httpx.AsyncClient, config: Config, provider: ProviderEndpoint) -> TestResult:
    async def allowed() -> bool:
        return not await app.dispatch.usage.over_cap(app.config, app.bot)

    start = monotonic()
    try:
        ProviderInput(
            base_url=provider.base_url, model=provider.model, api_key_env=provider.api_key_env or None,
            reasoning_effort=provider.reasoning_effort,
        )
        _check_keys(config, [provider])
        key = config.secret(provider.api_key_env) if provider.api_key_env else None
        client = ChatClient(provider, key, http, usage=app.dispatch.usage)
        async with asyncio.timeout(TEST_TIMEOUT):
            with model_request("chat", None, allowed):
                message = await client.complete(
                    [{"role": "user", "content": "Reply with OK."}], [],
                    once=True, max_tokens=32, timeout=TEST_TIMEOUT,
                )
        reply = message.get("content") if isinstance(message, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError
        if key:
            reply = reply.replace(key, "[redacted]")
        for pattern in CREDENTIAL_PATTERNS:
            reply = pattern.sub("[redacted]", reply)
    except KeyPairing as exc:
        error = str(exc)
    except ValidationError:
        error = "Fix this provider's URL, model or key variable in settings."
    except ModelCallSkipped:
        error = "The daily spend cap has been reached."
    except MissingProviderKey:
        error = "Set this provider's key in .env."
    except (TimeoutError, httpx.TimeoutException):
        error = "Provider test timed out."
    except httpx.HTTPStatusError as exc:
        error = f"Provider returned HTTP {exc.response.status_code}."
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        error = "Could not reach the provider."
    except (KeyError, IndexError, TypeError, ValueError):
        error = "Provider did not return a valid text reply."
    else:
        return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=reply[:2000])
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=error)
