"""Owner settings JSON contract: models, providers and permissions.

GET /api/models returns ModelSettings. POST /api/models replaces one ModelUpdate
role and returns ModelSettings. POST /api/models/test takes ProviderTest and
returns TestResult. Index 0 is the role's primary, followed by its own fallbacks. Triage (Jev, a decision model)
is edited and tested like a role but has no fallbacks or effort; its test is a real decisions request.
ModelSettings.providers lists the saved `[providers.<name>]` tables plus every other address the roles use;
dom/web/provider_settings.py adds, edits, tests and removes them and fetches their model lists.
For search, omitting index (null) tests the saved chain in order and reports
which provider answered. GET /api/permissions returns Permissions; POST
/api/permissions sets one PermissionUpdate and returns Permissions. Every POST
takes the GET revision (a hash of config.toml, shared by both GETs) so a stale
page cannot modify or test a moved slot. POST /api/permissions/desktop-steps sets
the step budget of the owner's desktop tasks (DesktopStepsUpdate) and returns Permissions.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypeVar, get_args
from urllib.parse import urlsplit

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dom.config import (
    MAX_DESKTOP_STEPS,
    MIN_DESKTOP_STEPS,
    MODEL_ROLES,
    SEARCH_PROVIDERS,
    Config,
    ProviderEndpoint,
    ProviderKind,
    SearchProvider,
    _replace_file,
    validate_config,
)
from dom import model_lists
from dom.doctor import _probe_hint, is_zdr_refusal
from dom.jev import JevClient, JevUnavailable
from dom.llm import ChatClient, FallbackClient, MissingProviderKey, openrouter_host
from dom.search import PROVIDER_INFO, SearchClient, search_provider_kind
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN
from dom.usage import ModelCallSkipped, model_request, on_own_network, provider_address, unroutable_address
from dom.web.shared import _bounded_stream, owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
API_PATH = "/api/models"
PERMISSIONS_PATH = "/api/permissions"
DESKTOP_STEPS_PATH = PERMISSIONS_PATH + "/desktop-steps"
DESKTOP_STEPS = ("desktop", "max_steps")
# Serialises the dashboard's config.toml writes, which run in a thread.
_CONFIG_WRITE = asyncio.Lock()
STUDIO_MOVE_PATH = API_PATH + "/studio-move"
JSON_POSTS = {API_PATH, API_PATH + "/test", STUDIO_MOVE_PATH, PERMISSIONS_PATH, DESKTOP_STEPS_PATH}
MAX_BODY = 32 * 1024
MAX_FALLBACKS = 5
URL_RULE = ("Use an HTTPS API base URL without credentials, a query or a fragment. "
            "HTTP is accepted only for localhost, a private address or a local network name.")
TEST_TIMEOUT = 8
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
REVISION = r"^[a-f0-9]{64}$"
CREDENTIAL_PATTERNS = (API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN)
# Decision models (Jev's decisions API) edited beside the routing roles. Each has one endpoint: no fallbacks, no effort.
DECISION_ROLES = ("triage",)
SettingsRole = Literal["chat", "fallback", "media", "drafting", "search", "escalation", "triage"]
SETTINGS_ROLES = get_args(SettingsRole)
assert SETTINGS_ROLES == (*MODEL_ROLES, *DECISION_ROLES)


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


def _validate_slot_for_role(role: SettingsRole, slot: ProviderInput) -> ProviderInput:
    if role in DECISION_ROLES:
        if slot.provider is not None or slot.reasoning_effort is not None:
            raise ValueError("Triage takes a decisions endpoint, a model ID and a key variable only.")
        if not slot.model:
            raise ValueError("Enter a model ID without spaces or credentials.")
        return slot
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
    role: SettingsRole
    primary: ProviderInput | None
    fallbacks: list[ProviderInput] = Field(max_length=MAX_FALLBACKS)

    @model_validator(mode="after")
    def _validate_role_slots(self) -> ModelUpdate:
        if self.role in DECISION_ROLES and (self.primary is None or self.fallbacks):
            raise ValueError("Triage takes exactly one endpoint.")
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


def _keyed_routes(config: Config) -> list:
    """Everything in config.toml that sends a key variable to an address, after the starter defaults are applied."""
    return [*(endpoint for _, endpoint in config.model_endpoints()), *config.provider_setups.values(),
            *config.studio.voice_fallbacks, *config.studio.image_fallbacks]


def _paired(config: Config) -> dict[str, set[str | None]]:
    paired: dict[str, set[str | None]] = {}
    for endpoint in _keyed_routes(config):
        if endpoint.api_key_env:
            paired.setdefault(endpoint.api_key_env, set()).add(_address(endpoint.base_url))
    return paired


def _check_effective_keys(disk: Config, saved: Config) -> None:
    """The rule _check_keys applies to what the owner typed, applied to what the saved file will actually route:
    the starter defaults can fill in a key the owner left blank (an OpenRouter search table takes
    OPENROUTER_API_KEY), so a key variable that holds a value must not reach a host config.toml doesn't pair it with."""
    paired = _paired(disk)
    for endpoint in _keyed_routes(saved):
        name = endpoint.api_key_env
        if name and _address(endpoint.base_url) not in paired.get(name, ()) and disk.secret(name):
            raise KeyPairing(
                f"This change would send {name} to {_address(endpoint.base_url)}, which it isn't paired with "
                "(a slot left without a key variable can take the starter's). Give that address its own key variable, "
                "or edit config.toml by hand.")


def _check_keys(disk: Config, providers: list[ProviderEndpoint]) -> None:
    """A key variable may go only to the scheme://host:port that config.toml already pairs it with, in a model
    role or a saved `[providers.<name>]` table (so triage's
    key is allowed on its paired host such as https://openrouter.ai, but not elsewhere). A new name may be used
    for one host, unless it already holds a value, names a non-model instance credential (the bot token, desktop
    keys, KLIPY_API_KEY, or an unpaired JEV_API_KEY), or belongs to another host. Pairing an existing key with a
    new host stays a hand edit of config.toml."""
    paired = _paired(disk)
    instance = disk.model_copy(update={"models": {}, "provider_setups": {}}).secret_names()
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


class RoleView(BaseModel):
    primary: ProviderView | None
    fallbacks: list[ProviderView]


class ProviderUse(BaseModel):
    role: SettingsRole
    index: int


class ProviderSetupView(BaseModel):
    """A provider: a saved `[providers.<name>]` table, or (saved=False) an address a role uses that isn't saved yet."""

    name: str
    kind: ProviderKind
    base_url: str
    api_key_env: str | None
    key_set: bool
    is_local: bool
    saved: bool
    listable: bool
    uses: list[ProviderUse]


StudioChain = Literal["voice", "image"]


class StudioFallbackView(BaseModel):
    """One cloud backup for the local studio ([[studio.<chain>_fallbacks]]); a Gemini voice needs paid=true to run."""

    provider: str
    model: str
    api_key_env: str
    key_set: bool
    paid: bool


class StudioMove(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    chain: StudioChain
    index: int = Field(ge=1, le=64)  # moves this backup one place up


class ModelSettings(BaseModel):
    revision: str
    # Routing roles, then the decision roles (triage): triage has a primary and never fallbacks.
    roles: dict[SettingsRole, RoleView]
    providers: list[ProviderSetupView]
    studio: dict[StudioChain, list[StudioFallbackView]]
    search_providers: list[SearchProviderInfo]
    restart_required: bool


class ProviderTest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    role: SettingsRole
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
        raise HTTPException(409, "Fix config.toml before editing settings.") from None


def _check_revision(current: Snapshot, revision: str) -> None:
    if current.revision != revision:
        raise HTTPException(409, "Configuration changed. Reload settings and try again.")


def chain(config: Config, role: SettingsRole) -> tuple[ProviderEndpoint, ...]:
    """A role's saved endpoints in order; triage always has exactly one (the starter default when unset)."""
    return (config.models["triage"],) if role in DECISION_ROLES else config.providers(role)


def _dump_slot(role: SettingsRole, provider: ProviderEndpoint) -> dict:
    data = provider.model_dump(exclude_none=True, exclude={"fallbacks"})
    if role == "search" and search_provider_kind(provider) != "openrouter":
        data.pop("model", None)
    return data


def set_role(document: tomlkit.TOMLDocument, role: SettingsRole, slots: list[ProviderEndpoint]) -> None:
    """Write one role's whole chain into config.toml's document, or drop the role when `slots` is empty."""

    def models():
        # A fresh view per step: when other tables sit between the [models.*] ones, tomlkit's view of them goes stale
        # after a write (it forgets a key it just inserted), so one view must never span two writes.
        return document["models"]

    if "chat_fallback" in models():
        legacy = models().pop("chat_fallback")
        if role != "fallback" and "fallback" not in models():
            models()["fallback"] = legacy
    if not slots:
        models().pop(role, None)
    else:
        primary = _dump_slot(role, slots[0])
        if slots[1:]:
            primary["fallbacks"] = [_dump_slot(role, provider) for provider in slots[1:]]
        models()[role] = primary


def validated(current: Snapshot, document: tomlkit.TOMLDocument) -> Snapshot:
    text = tomlkit.dumps(document)
    saved = Snapshot(validate_config(current.config.home, tomllib.loads(text)), text)
    if "chat" not in saved.config.models:
        raise HTTPException(422, "Configure a primary chat provider.")
    try:
        _check_effective_keys(current.config, saved.config)
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    return saved


def _save(current: Snapshot, update: ModelUpdate) -> Snapshot:
    """The settings with `update` applied, validated but not written."""
    _check_revision(current, update.revision)
    try:
        _check_keys(current.config, [*([update.primary] if update.primary else []), *update.fallbacks])
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    document = tomlkit.parse(current.text)
    set_role(document, update.role, [*([update.primary] if update.primary else []), *update.fallbacks])
    return validated(current, document)


async def commit(app: App, change: Callable[[Snapshot], Snapshot], failure: str) -> Snapshot:
    """Apply `change` to config.toml and the running routes, or to neither. The new settings and every client they
    route to are built before the file is replaced, so a route that can't be built keeps the old file and runtime.
    One config write at a time; the disk work runs off the loop, and a cancelled request still finishes the swap."""

    async def run() -> Snapshot:
        async with _CONFIG_WRITE:
            try:
                current = await asyncio.to_thread(_snapshot, app.config.home)
                saved = await asyncio.to_thread(change, current)
                routes = app.model_routes(saved.config)
                await asyncio.to_thread(_write, current, saved)
            except (OSError, ValueError, httpx.InvalidURL):
                raise HTTPException(409, failure) from None
            app.use_model_routes(routes)
            return saved

    return await asyncio.shield(asyncio.ensure_future(run()))


def _write(current: Snapshot, saved: Snapshot) -> None:
    """Replace config.toml with a validated snapshot, keeping the first original and the last version beside it."""
    home = current.config.home
    path = home / "config.toml"
    if saved.text != current.text:
        if path.read_bytes() != current.text.encode("utf-8"):
            raise HTTPException(409, "Configuration changed. Reload settings and try again.")
        if not (home / "config.toml.orig").exists():  # the file as it was before the first save from here
            _replace_file(home / "config.toml.orig", current.text, mode=0o600, durable=False)
        _replace_file(home / "config.toml.bak", current.text, mode=0o600, durable=False)
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


def slot_kind(role: SettingsRole, endpoint: ProviderEndpoint) -> ProviderKind:
    """The provider kind a role's slot implies, for an address not saved under [providers] yet."""
    if role in DECISION_ROLES:
        return "jev"
    if role == "search":
        return search_provider_kind(endpoint)
    try:
        return "openrouter" if openrouter_host(httpx.URL(endpoint.base_url).host) else "openai"
    except httpx.InvalidURL:
        return "openai"


KIND_LABELS: dict[str, str] = {"openrouter": "OpenRouter", "jev": "Jev decisions"} | {
    name: PROVIDER_INFO[name].label for name in ("searxng", "deepseek", "tavily")
}


def kind_fits(kind: ProviderKind, role: SettingsRole, endpoint: ProviderEndpoint) -> bool:
    """Whether a provider of this kind can serve the slot: triage takes Jev, search its own kind, the rest chat."""
    if role in DECISION_ROLES:
        return kind == "jev"
    if role == "search":
        return kind == search_provider_kind(endpoint)
    return kind in ("openai", "openrouter")


def family(kind: ProviderKind) -> str:
    """Providers at one address and key variable are one provider, unless they serve different kinds of call."""
    return kind if kind in ("jev", "searxng", "deepseek", "tavily") else "chat"


def _key_set(config: Config, variable: str | None) -> bool:
    try:
        return bool(variable and config.secret(variable))
    except (OSError, ValueError):
        return False


def provider_catalog(config: Config) -> list[ProviderSetupView]:
    """Saved providers first, then each other address (and key variable) a role uses, with every slot that uses it."""
    found: dict[tuple[str, str | None, str], ProviderSetupView] = {}
    for name, setup in config.provider_setups.items():
        found.setdefault((setup.base_url, setup.api_key_env, family(setup.kind)), ProviderSetupView(
            name=name, kind=setup.kind, base_url=setup.base_url, api_key_env=setup.api_key_env,
            key_set=_key_set(config, setup.api_key_env), is_local=_is_local(setup.base_url), saved=True,
            listable=model_lists.listable(setup.kind), uses=[],
        ))
    names = {view.name.lower() for view in found.values()}
    for role in SETTINGS_ROLES:
        for index, endpoint in enumerate(chain(config, role)):
            view = _provider_view(config, endpoint, role=role)
            if not view.base_url:  # a hand-written address the settings API can't show
                continue
            kind = slot_kind(role, endpoint)
            key = (view.base_url, view.api_key_env, family(kind))
            if key not in found:
                base = re.sub(r"[^A-Za-z0-9 ._-]+", " ", KIND_LABELS.get(kind) or httpx.URL(view.base_url).host).strip()[:36] or "Provider"
                name, n = base, 2
                while name.lower() in names:
                    name, n = f"{base} {n}", n + 1
                names.add(name.lower())
                found[key] = ProviderSetupView(
                    name=name, kind=kind, base_url=view.base_url, api_key_env=view.api_key_env, key_set=view.key_set,
                    is_local=view.kind == "local", saved=False, listable=model_lists.listable(kind), uses=[],
                )
            found[key].uses.append(ProviderUse(role=role, index=index))
    return list(found.values())


def _settings(app: App, snapshot: Snapshot) -> ModelSettings:
    config = snapshot.config
    roles = {}
    for role in SETTINGS_ROLES:
        primary, *fallbacks = chain(config, role) or (None,)
        roles[role] = RoleView(
            primary=_provider_view(config, primary, role=role) if primary else None,
            fallbacks=[_provider_view(config, provider, role=role) for provider in fallbacks],
        )
    return ModelSettings(
        revision=snapshot.revision,
        roles=roles,
        providers=provider_catalog(config),
        studio={
            chain: [
                StudioFallbackView(provider=item.provider, model=item.model, api_key_env=item.api_key_env,
                                   key_set=_key_set(config, item.api_key_env), paid=item.paid)
                for item in getattr(config.studio, f"{chain}_fallbacks")
            ]
            for chain in ("voice", "image")
        },
        search_providers=_search_provider_infos(),
        restart_required=any(chain(config, role) != chain(app.config, role) for role in SETTINGS_ROLES),
    )


Input = TypeVar("Input", bound=BaseModel)


def is_json(request: Request) -> bool:
    return request.headers.get("Content-Type", "").partition(";")[0].strip().lower() == "application/json"


async def _input(request: Request, model: type[Input], max_bytes: int = MAX_BODY) -> Input:
    if not is_json(request):
        raise HTTPException(415, "Expected application/json.")
    body = bytearray()
    async for chunk in _bounded_stream(request, max_bytes):
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
                detail = "Choose one of the supported values."
        raise HTTPException(422, detail) from None


@router.get(API_PATH)
async def read_models(request: Request) -> ModelSettings:
    app = owner_app(request)
    return _settings(app, _snapshot(app.config.home))


def _probe_tuple(role: SettingsRole, endpoint: ProviderEndpoint) -> tuple[str, str, str | None, str | None, str | None]:
    """What a probe sends: a slot whose tuple is already in the chain was tested when it was added."""
    kind = search_provider_kind(endpoint) if role == "search" else None
    return (endpoint.base_url, endpoint.model, endpoint.api_key_env or None, kind, endpoint.reasoning_effort)


# A probe is sent as the role will send it, reasoning effort included; reasoning needs room before the reply.
PROBE_TOKENS = 256
REASONING_PROBE_TOKENS = 2048
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
    # An effort is checked against the provider's fetched list when it knows the model (D9).
    effort_failures = []
    for index, provider in enumerate(submitted):
        listed = await model_lists.cached(app.store, provider.base_url, provider.api_key_env, fresh_only=False)
        if error := model_lists.effort_error(listed, provider.model, provider.reasoning_effort):
            effort_failures.append({"index": index, "error": error})
    if effort_failures:
        detail = "; ".join(f"{_slot_label(item['index'])}: {item['error']}" for item in effort_failures)
        return JSONResponse(status_code=422, content={"detail": detail, "errors": effort_failures})

    # Built first, so the key pairing is checked on the routes as they will be saved (with the starter's defaults
    # filled in), and each slot is tested as it will be sent.
    try:
        candidate = await asyncio.to_thread(_save, current, update)
    except (OSError, ValueError):
        raise HTTPException(409, "Could not save valid model settings. Check config.toml and its permissions.") from None
    # A slot already in the chain (at any position) was tested when it was added; a changed model or effort is tested.
    existing = {_probe_tuple(update.role, provider) for provider in chain(current.config, update.role)}
    to_test = [
        (index, provider)
        for index, provider in enumerate(chain(candidate.config, update.role))
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

    saved = await commit(app, lambda current: _save(current, update),
                         "Could not save valid model settings. Check config.toml and its permissions.")
    return _settings(app, saved)


def _move_studio(current: Snapshot, move: StudioMove) -> Snapshot:
    _check_revision(current, move.revision)
    key = f"{move.chain}_fallbacks"
    chain = list(getattr(current.config.studio, key))
    if move.index >= len(chain):
        raise HTTPException(404, "There is no studio backup at that position.")
    chain[move.index - 1], chain[move.index] = chain[move.index], chain[move.index - 1]
    document = tomlkit.parse(current.text)
    tables = tomlkit.aot()
    for item in chain:
        tables.append(tomlkit.item(item.model_dump(exclude_defaults=True)))
    document["studio"][key] = tables
    saved = validated(current, document)
    _write(current, saved)
    return saved


@router.post(STUDIO_MOVE_PATH)
async def move_studio_fallback(request: Request) -> ModelSettings:
    """Reorder a studio cloud chain. Nothing new is reached, so nothing is tested; it applies at once."""
    app = owner_app(request)
    move = await _input(request, StudioMove)
    async with _CONFIG_WRITE:
        try:
            saved = await asyncio.to_thread(lambda: _move_studio(_snapshot(app.config.home), move))
        except (OSError, ValueError):
            raise HTTPException(409, "Could not save the order. Check config.toml and its permissions.") from None
        app.config = app.config.model_copy(update={"studio": saved.config.studio})
    return _settings(app, saved)


@router.post(API_PATH + "/test")
async def test_provider(request: Request) -> TestResult:
    app = owner_app(request)
    test = await _input(request, ProviderTest)
    snapshot = _snapshot(app.config.home)
    _check_revision(snapshot, test.revision)
    providers = chain(snapshot.config, test.role)
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
    result = await _probe(app, current.http, snapshot.config, provider, role=test.role)
    if result.ok and test.role in DECISION_ROLES:
        app.refresh_triage()  # a key added to .env after saving takes effect once it works
    return result


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
    Permission("studio_voice", ("studio", "voice"), "Send voice notes",
               "Dom can use your configured media server to send occasional voice notes."),
    Permission("studio_video", ("studio", "video_notes"), "Send round video notes",
               "Dom can send occasional round videos."),
    Permission("studio_images", ("studio", "images"), "Send photos",
               "Dom can send occasional generated photos."),
    Permission("studio_special", ("studio", "special"), "Send special video notes",
               "Dom can send a special video for a milestone or when you ask in your DM."),
    Permission("studio_profile", ("studio", "profile_photo_follows_preset"), "Match the Everywhere preset’s profile photo",
               "Dom updates his Telegram profile photo to match his Everywhere preset, at most once an hour."),
)
PermissionId = Literal[
    "memory_self_edit", "openers", "ephemeral_replies", "leak_check",
    "studio_voice", "studio_video", "studio_images", "studio_special", "studio_profile",
]
assert set(get_args(PermissionId)) == {item.id for item in PERMISSIONS}


class PermissionView(BaseModel):
    id: PermissionId
    label: str
    description: str
    enabled: bool


class DesktopStepsView(BaseModel):
    value: int
    min: int = MIN_DESKTOP_STEPS
    max: int = MAX_DESKTOP_STEPS


class Permissions(BaseModel):
    revision: str
    items: list[PermissionView]
    desktop_steps: DesktopStepsView


class DesktopStepsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    max_steps: int = Field(ge=MIN_DESKTOP_STEPS, le=MAX_DESKTOP_STEPS)


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
    ], desktop_steps=DesktopStepsView(value=snapshot.config.desktop.max_steps))


def _with_permission(config: Config, path: tuple[str, ...], value: bool | int) -> Config:
    """A copy of the running config with one setting changed; everything else stays as it runs now."""
    key, *rest = path
    if not rest:
        return config.model_copy(update={key: value})
    return config.model_copy(update={key: _with_permission(getattr(config, key), tuple(rest), value)})


@router.get(PERMISSIONS_PATH)
async def read_permissions(request: Request) -> Permissions:
    app = owner_app(request)
    return _permissions(_snapshot(app.config.home))


@router.post(PERMISSIONS_PATH)
async def update_permission(request: Request) -> Permissions:
    app = owner_app(request)
    update = await _input(request, PermissionUpdate)
    permission = next(item for item in PERMISSIONS if item.id == update.id)
    # Reading, checking and replacing config.toml waits on the disk; it runs off the event loop, one save at a time,
    # so a slow disk never stalls Dom's other work.
    async with _CONFIG_WRITE:
        saved = await asyncio.to_thread(_save_setting, app.config.home, update.revision, permission.path, update.enabled)
        app.config = _with_permission(app.config, permission.path, update.enabled)
    return _permissions(saved)


@router.post(DESKTOP_STEPS_PATH)
async def update_desktop_steps(request: Request) -> Permissions:
    """The step budget of the owner's desktop tasks. A task reads it when it starts, so a save applies to the next one."""
    app = owner_app(request)
    update = await _input(request, DesktopStepsUpdate)
    async with _CONFIG_WRITE:
        saved = await asyncio.to_thread(_save_setting, app.config.home, update.revision, DESKTOP_STEPS, update.max_steps)
        app.config = _with_permission(app.config, DESKTOP_STEPS, update.max_steps)
    return _permissions(saved)


def _save_setting(home: Path, revision: str, path: tuple[str, ...], setting: bool | int) -> Snapshot:
    current = _snapshot(home)
    _check_revision(current, revision)
    document = tomlkit.parse(current.text)
    table = document
    *parents, key = path
    for name in parents:
        if name not in table:
            table[name] = tomlkit.table()
        table = table[name]
    value = tomlkit.item(setting)
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
    return saved


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


async def _probe_decisions(app: App, http: httpx.AsyncClient, config: Config, provider: ProviderEndpoint) -> TestResult:
    """A real Jev decisions request with one yes/no question, recorded as triage usage."""
    local = _is_local(provider.base_url)

    async def allowed() -> bool:
        return local or not await app.dispatch.usage.over_cap(app.config, app.bot, sleep=app.sleep)

    start = monotonic()
    key = None
    try:
        _validate_slot_for_role("triage", ProviderInput(
            base_url=provider.base_url, model=provider.model, api_key_env=provider.api_key_env or None,
        ))
        _check_keys(config, [provider])
        key = config.secret(provider.api_key_env) if provider.api_key_env else None
        if provider.api_key_env and not key:
            raise MissingProviderKey
        client = JevClient(http, key or "", provider.model, provider.base_url, usage=app.dispatch.usage)
        async with asyncio.timeout(TEST_TIMEOUT):
            with model_request("triage", None, allowed):
                answers = await client.decide("A settings test from the owner's dashboard.", {"check": {
                    "type": "choice", "instructions": "Answer yes.",
                    "criteria": {"yes": "The check reached the decisions API.", "no": "The check did not reach it."},
                }})
        choice = (answers.get("check") or {}).get("choice")
        if choice not in ("yes", "no"):
            raise JevUnavailable
    except KeyPairing as exc:
        error = str(exc)
    except (ValidationError, ValueError):
        error = "Fix this provider's URL, model or key variable in settings."
    except ModelCallSkipped:
        error = "The daily spend cap has been reached."
    except MissingProviderKey:
        error = MISSING_KEY
    except TimeoutError:
        error = "Provider test timed out."
    except JevUnavailable as exc:
        cause = exc.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            error = (_probe_hint(cause.response) if is_zdr_refusal(cause.response)
                     else f"Provider returned HTTP {cause.response.status_code}.")
        elif isinstance(cause, httpx.TimeoutException):
            error = "Provider test timed out."
        elif isinstance(cause, httpx.HTTPError):
            error = "Could not reach the provider."
        else:
            error = "The decisions API did not return a usable answer."
    else:
        return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=f"Decided: {choice}")
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=_redact(error, key))


async def _probe(
    app: App, http: httpx.AsyncClient, config: Config, provider: ProviderEndpoint, *, role: SettingsRole = "chat",
) -> TestResult:
    if role in DECISION_ROLES:
        return await _probe_decisions(app, http, config, provider)
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
        kind = "escalation" if role == "escalation" else "chat"
        client = ChatClient(provider, key, http, sleep=app.sleep, usage=app.dispatch.usage, kind=kind)
        async with asyncio.timeout(TEST_TIMEOUT):
            with model_request(kind, escalation_owner, allowed):
                message = await client.complete(
                    [{"role": "user", "content": "Reply with OK."}], [],
                    once=True, max_tokens=PROBE_TOKENS if provider.reasoning_effort is None else REASONING_PROBE_TOKENS,
                    timeout=TEST_TIMEOUT,
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
