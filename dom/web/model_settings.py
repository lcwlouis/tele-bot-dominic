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
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypeVar, get_args
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dom.config import (
    ACP_NO_TOOLS_OFF,
    DEEPSEEK_EFFORT_ERROR,
    MAX_BOT_DEPTH_LIMIT,
    MAX_BOT_REPLY_INTERVAL_SECONDS,
    MAX_DESKTOP_STEPS,
    MAX_TRIAGE_CONFIDENCE,
    MEDIA_ADDRESS_RULE,
    MEDIA_KEY_RULE,
    MEDIA_PROVIDER_KEY_RULE,
    MIN_BOT_DEPTH_LIMIT,
    MIN_BOT_REPLY_INTERVAL_SECONDS,
    MIN_DESKTOP_STEPS,
    MIN_TRIAGE_CONFIDENCE,
    MODEL_ROLES,
    SEARCH_PROVIDERS,
    STUDIO_CHAINS,
    MEDIA_BASE_URLS,
    Config,
    DataPolicy,
    ProviderEndpoint,
    ProviderKind,
    ProviderType,
    SearchProvider,
    StudioChain,
    StudioFallback,
    _replace_file,
    set_dotenv,
    set_dotenv_many,
    validate_config,
)
from dom import model_lists
from dom.doctor import _probe_hint, is_zdr_refusal
from dom.embedding import EmbeddingProvider
from dom.jev import DEFAULT_BASE_URL as DECISIONS_BASE_URL, JevClient, JevUnavailable
from dom.llm import ChatClient, FallbackClient, MissingProviderKey, ProviderBudgetError, openrouter_host
from dom.provider_budget import budget_line
from dom.search import PROVIDER_INFO, SearchClient, search_provider_kind
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN
from dom.usage import ModelCallSkipped, model_request, on_own_network, provider_address, unroutable_address
from dom.studio.policy import switch_servable
from dom.studio.profile import PROFILE_AT
from dom.web.shared import _bounded_stream, owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
API_PATH = "/api/models"
PERMISSIONS_PATH = "/api/permissions"
DESKTOP_STEPS_PATH = PERMISSIONS_PATH + "/desktop-steps"
DESKTOP_STEPS = ("desktop", "max_steps")
PROFILE_INTERVAL_PATH = PERMISSIONS_PATH + "/profile-interval"
PROFILE_INTERVAL = ("studio", "profile_photo_every_days")
REPLY_CONTROLS_PATH = PERMISSIONS_PATH + "/reply-controls"
# Serialises the dashboard's config.toml writes, which run in a thread.
_CONFIG_WRITE = asyncio.Lock()
JSON_POSTS = {
    API_PATH, API_PATH + "/test", PERMISSIONS_PATH, DESKTOP_STEPS_PATH, PROFILE_INTERVAL_PATH, REPLY_CONTROLS_PATH,
}
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
SettingsRole = Literal["chat", "fallback", "media", "drafting", "search", "escalation", "embedding", "triage"]
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
        if role == "embedding" and slot.reasoning_effort is not None:
            raise ValueError("Embedding models do not take a reasoning effort.")
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
        if self.primary is None and (self.role not in ("fallback", "search", "escalation", "embedding") or self.fallbacks):
            raise ValueError("Only fallback, search, escalation and embedding may be left empty.")
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
            *config.studio.fallbacks]


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
    """A slot that uses a provider: a role's chain position, or a studio backup chain's (voice, image, video)."""

    role: SettingsRole | StudioChain
    index: int


class ProviderSetupView(BaseModel):
    """A provider: a saved `[providers.<name>]` table, or (saved=False) an address a role uses that isn't saved yet."""

    name: str
    kind: ProviderKind
    type: ProviderType
    base_url: str
    api_key_env: str | None
    key_set: bool
    is_local: bool
    saved: bool
    listable: bool
    uses: list[ProviderUse]


class StudioFallbackView(BaseModel):
    """One cloud backup for the local studio ([[studio.<chain>_fallbacks]]); a Gemini voice needs paid=true to run."""

    provider: str
    model: str
    api_key_env: str
    key_set: bool
    paid: bool
    voice: str | None = None
    supports_reference: bool
    allow_without_reference: bool
    data_policy: DataPolicy


class ProviderPreset(BaseModel):
    name: str
    base_url: str
    api_key_env: str | None = None
    kind: ProviderKind = "openai"
    type: ProviderType = "chat"
    model: str | None = None


# Chat presets use the OpenAI-compatible endpoint, including DeepSeek (the separate
# "deepseek" kind in this editor is for search). OpenRouter comes once per type, each with its own key variable: a
# media key may take a looser data policy, so it never serves chat.
PROVIDER_PRESETS = (
    ProviderPreset(name="OpenRouter", base_url="https://openrouter.ai/api/v1",
                   api_key_env="OPENROUTER_API_KEY", kind="openrouter"),
    ProviderPreset(name="OpenRouter media", base_url=MEDIA_BASE_URLS["openrouter"],
                   api_key_env="OPENROUTER_MEDIA_API_KEY", kind="openrouter", type="media"),
    ProviderPreset(name="OpenRouter decisions", base_url=DECISIONS_BASE_URL,
                   api_key_env="OPENROUTER_API_KEY", kind="openrouter", type="decisions"),
    ProviderPreset(name="Google Gemini media", base_url=MEDIA_BASE_URLS["gemini"],
                   api_key_env="GEMINI_MEDIA_API_KEY", kind="gemini", type="media"),
    ProviderPreset(name="OpenAI", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY"),
    ProviderPreset(name="Anthropic", base_url="https://api.anthropic.com/v1", api_key_env="ANTHROPIC_API_KEY"),
    ProviderPreset(name="Google Gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                   api_key_env="GEMINI_API_KEY"),
    ProviderPreset(name="Groq", base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY"),
    ProviderPreset(name="DeepSeek", base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY", model="deepseek-flash"),
    ProviderPreset(name="Mistral", base_url="https://api.mistral.ai/v1", api_key_env="MISTRAL_API_KEY"),
    ProviderPreset(name="Together", base_url="https://api.together.ai/v1", api_key_env="TOGETHER_API_KEY"),
    ProviderPreset(name="Fireworks", base_url="https://api.fireworks.ai/inference/v1", api_key_env="FIREWORKS_API_KEY"),
    ProviderPreset(name="Ollama", base_url="http://localhost:11434/v1"),
    ProviderPreset(name="LM Studio", base_url="http://localhost:1234/v1"),
    ProviderPreset(name="llama.cpp", base_url="http://llama-server:8080/v1"),
    ProviderPreset(name="vLLM", base_url="http://vllm:8000/v1"),
)


class ModelSettings(BaseModel):
    revision: str
    # Routing roles, then the decision roles (triage): triage has a primary and never fallbacks.
    roles: dict[SettingsRole, RoleView]
    providers: list[ProviderSetupView]
    provider_presets: tuple[ProviderPreset, ...] = PROVIDER_PRESETS
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


def _kept_inputs(table) -> dict[tuple[str, str], tuple[str, ...]]:
    """The `input` overrides in a role's saved table (and its fallbacks), by provider address and model: the
    dashboard's forms don't carry them, so a save puts them back on the providers that stay."""
    if table is None:
        return {}
    found = {}
    for entry in (table, *table.get("fallbacks", ())):
        if entry.get("input") is not None and "base_url" in entry:
            found[(str(entry["base_url"]), str(entry.get("model", "")))] = tuple(entry["input"])
    return found


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
    kept = _kept_inputs(models().get(role))
    slots = [
        slot.model_copy(update={"input": kept[(slot.base_url, slot.model)]})
        if slot.input is None and (slot.base_url, slot.model) in kept else slot
        for slot in slots
    ]
    if not slots:
        models().pop(role, None)
    else:
        primary = _dump_slot(role, slots[0])
        if slots[1:]:
            primary["fallbacks"] = [_dump_slot(role, provider) for provider in slots[1:]]
        models()[role] = primary


def set_backups(document: tomlkit.TOMLDocument, studio_chain: StudioChain, backups: list[StudioFallback]) -> None:
    """Write one studio backup chain ([[studio.<chain>_fallbacks]]) into config.toml's document, or drop it."""
    if "studio" not in document:
        document["studio"] = tomlkit.table()
    key = f"{studio_chain}_fallbacks"
    if backups:
        tables = tomlkit.aot()
        for item in backups:
            tables.append(tomlkit.item(item.model_dump(exclude_defaults=True)))
        document["studio"][key] = tables
    else:
        document["studio"].pop(key, None)


def validated(current: Snapshot, document: tomlkit.TOMLDocument) -> Snapshot:
    text = tomlkit.dumps(document)
    try:
        saved = Snapshot(validate_config(current.config.home, tomllib.loads(text)), text)
    except ValidationError as exc:
        # These validation messages use trusted templates and tell the owner how to fix the setting.
        rules = (MEDIA_KEY_RULE.partition("{name}")[2], MEDIA_PROVIDER_KEY_RULE.partition("{name}")[2],
                 MEDIA_ADDRESS_RULE.partition("{url}")[0], DEEPSEEK_EFFORT_ERROR)
        message = next(
            (error["msg"] for error in exc.errors(include_input=False) if any(rule in error["msg"] for rule in rules)),
            None,
        )
        if message is None:
            raise
        raise HTTPException(422, message.removeprefix("Value error, ")) from None
    if "chat" not in saved.config.models:
        raise HTTPException(422, "Configure a primary chat provider.")
    try:
        _check_effective_keys(current.config, saved.config)
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    return saved


TYPE_NAMES = {"chat": "a chat", "media": "a media", "decisions": "a decisions", "search": "a web search"}


def _check_role_types(config: Config, role: SettingsRole, slots: list[ProviderEndpoint]) -> None:
    """A slot on a saved provider's address and key variable must use a provider of the role's type (E70); a custom
    address that matches no saved provider is still allowed."""
    for index, slot in enumerate(slots):
        matches = [(name, setup) for name, setup in config.provider_setups.items()
                   if same_url(setup.base_url, slot.base_url) and setup.api_key_env == (slot.api_key_env or None)]
        if matches and not any(kind_fits(setup.kind, setup.type, role, slot) for _, setup in matches):
            name, setup = matches[0]
            raise HTTPException(422, f"{_slot_label(index)}: {name} is {TYPE_NAMES[setup.type]} provider, which "
                                     f"{role} can't use. Pick a provider of the right type.")


def _save(current: Snapshot, update: ModelUpdate) -> Snapshot:
    """The settings with `update` applied, validated but not written."""
    _check_revision(current, update.revision)
    submitted = [*([update.primary] if update.primary else []), *update.fallbacks]
    _check_role_types(current.config, update.role, submitted)
    try:
        _check_keys(current.config, submitted)
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    document = tomlkit.parse(current.text)
    set_role(document, update.role, submitted)
    saved = validated(current, document)
    # Starter defaults (e.g. OpenRouter search's default api_key_env) are filled in by validated();
    # re-check role types against the effective chain so an omitted key cannot land on a decisions provider.
    _check_role_types(saved.config, update.role, list(chain(saved.config, update.role)))
    return saved



async def commit(app: App, change: Callable[[Snapshot], Snapshot], failure: str) -> Snapshot:
    """Apply `change` to config.toml and the running routes, or to neither. The new settings and every client they
    route to are built before the file is replaced, so a route that can't be built keeps the old file and runtime.
    One config write at a time; the disk work runs off the loop, and a cancelled request still finishes the swap."""

    async def run() -> Snapshot:
        async with _CONFIG_WRITE:
            try:
                current = await asyncio.to_thread(_snapshot, app.config.home)
                saved = await asyncio.to_thread(change, current)
                chains = [name for name in STUDIO_CHAINS if getattr(current.config.studio, f"{name}_fallbacks")
                          != getattr(saved.config.studio, f"{name}_fallbacks")]
                routes = app.model_routes(saved.config, studio_chains=chains)
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


def slot_kind(role: SettingsRole, endpoint: ProviderEndpoint) -> tuple[ProviderKind, ProviderType]:
    """The service and type a role's slot implies, for an address not saved under [providers] yet: triage is a
    decisions provider, search a search service (or OpenRouter chat, for OpenRouter's web search), the rest chat."""
    if role in DECISION_ROLES:
        return "openrouter", "decisions"
    if role == "search":  # its provider is explicit: an OpenRouter search route on any host stays OpenRouter
        kind = search_provider_kind(endpoint)
        return kind, "chat" if kind == "openrouter" else "search"
    try:
        return ("openrouter" if openrouter_host(httpx.URL(endpoint.base_url).host) else "openai"), "chat"
    except httpx.InvalidURL:
        return "openai", "chat"


TYPE_LABELS: dict[str, str] = {"chat": "", "media": "media", "decisions": "decisions", "search": ""}
KIND_LABELS: dict[str, str] = {"openrouter": "OpenRouter", "gemini": "Google Gemini"} | {
    name: PROVIDER_INFO[name].label for name in ("searxng", "deepseek", "tavily")
}


def kind_fits(kind: ProviderKind, type_: ProviderType, role: SettingsRole | StudioChain, endpoint) -> bool:
    """Whether a provider can serve the slot (E70): triage takes a decisions provider, a studio backup a media provider
    of its own service, search its own service (OpenRouter's search is a chat provider), the other roles chat."""
    if role in STUDIO_CHAINS:
        return type_ == "media" and kind == endpoint.kind
    if role in DECISION_ROLES:
        return type_ == "decisions"
    if role == "search":
        return kind == search_provider_kind(endpoint) and type_ in ("search", "chat")
    return type_ == "chat"


def _key_set(config: Config, variable: str | None) -> bool:
    try:
        return bool(variable and config.secret(variable))
    except (OSError, ValueError):
        return False


def same_url(a: str, b: str) -> bool:
    """Whether two base URLs name the same address: a trailing slash doesn't make a different provider."""
    return a.rstrip("/") == b.rstrip("/")


def _uses(config: Config) -> Iterator[tuple[SettingsRole | StudioChain, int, ProviderEndpoint | StudioFallback]]:
    """Every slot a provider can serve: each role's chain, then each studio backup chain."""
    for role in SETTINGS_ROLES:
        for index, endpoint in enumerate(chain(config, role)):
            yield role, index, endpoint
    for studio_chain in STUDIO_CHAINS:
        for index, backup in enumerate(getattr(config.studio, f"{studio_chain}_fallbacks")):
            yield studio_chain, index, backup


def provider_catalog(config: Config) -> list[ProviderSetupView]:
    """Saved providers first, then each other address, key variable and type a role or studio backup uses, with every
    slot that uses it. One address and key is one provider per type (E70), and per search service."""
    found: dict[tuple[str, str | None, str, str | None], ProviderSetupView] = {}
    for name, setup in config.provider_setups.items():
        search_kind = setup.kind if setup.type == "search" else None
        found.setdefault((setup.base_url.rstrip("/"), setup.api_key_env, setup.type, search_kind), ProviderSetupView(
            name=name, kind=setup.kind, type=setup.type, base_url=setup.base_url, api_key_env=setup.api_key_env,
            key_set=_key_set(config, setup.api_key_env), is_local=_is_local(setup.base_url), saved=True,
            listable=model_lists.listable(setup.kind, setup.type, setup.base_url), uses=[],
        ))
    names = {view.name.lower() for view in found.values()}
    for role, index, slot in _uses(config):
        if role in STUDIO_CHAINS:
            kind, type_, base_url, variable = slot.kind, "media", slot.base_url, slot.api_key_env
            if not ENV_NAME.fullmatch(variable) or _looks_like_secret(variable):
                continue  # a hand-written variable the settings API can't show
            key_set = _key_set(config, variable)
        else:
            view = _provider_view(config, slot, role=role)
            if not view.base_url:  # a hand-written address the settings API can't show
                continue
            (kind, type_), base_url, variable, key_set = slot_kind(role, slot), view.base_url, view.api_key_env, view.key_set
        key = (base_url.rstrip("/"), variable, type_, kind if type_ == "search" else None)  # a trailing slash names the same address
        if key not in found:
            service = KIND_LABELS.get(kind) if kind != "openrouter" or openrouter_host(httpx.URL(base_url).host) else None
            label = " ".join(filter(None, (service or httpx.URL(base_url).host, TYPE_LABELS[type_])))
            base = re.sub(r"[^A-Za-z0-9 ._-]+", " ", label).strip()[:36] or "Provider"
            name, n = base, 2
            while name.lower() in names:
                name, n = f"{base} {n}", n + 1
            names.add(name.lower())
            found[key] = ProviderSetupView(
                name=name, kind=kind, type=type_, base_url=base_url, api_key_env=variable, key_set=key_set,
                is_local=_is_local(base_url), saved=False, listable=model_lists.listable(kind, type_, base_url), uses=[],
            )
        found[key].uses.append(ProviderUse(role=role, index=index))
    return list(found.values())


def _studio_view(config: Config, item, secrets: frozenset[str]) -> StudioFallbackView:
    """A backup as the page shows it. A hand-edited config.toml may hold a credential where a model ID or a variable
    name belongs; such a field is shown blank."""
    def shown(value: str) -> str:
        return "" if _looks_like_secret(value) or any(secret in value for secret in secrets) else value

    return StudioFallbackView(**item.model_dump() | {"model": shown(item.model), "api_key_env": shown(item.api_key_env)},
                              key_set=_key_set(config, item.api_key_env))


def _settings(app: App, snapshot: Snapshot) -> ModelSettings:
    """The settings view. It reads .env (key states, secret filtering), so routes build it off the event loop."""
    config = snapshot.config
    secrets = model_lists.secret_values(config)
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
            chain: [_studio_view(config, item, secrets) for item in getattr(config.studio, f"{chain}_fallbacks")]
            for chain in STUDIO_CHAINS
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
    return await asyncio.to_thread(lambda: _settings(app, _snapshot(app.config.home)))


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
    secrets = model_lists.secret_values(current.config)  # the 422 below quotes upstream effort labels
    for index, provider in enumerate(submitted):
        listed = await model_lists.cached(
            app.store, provider.base_url, provider.api_key_env, fresh_only=False, secrets=secrets,
        )
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
    return await asyncio.to_thread(_settings, app, saved)


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


ACP_DESCRIPTION = (
    "{agent} on your {plan} plan, through {command}, for escalation and drafting in your DM only. Not zero data "
    "retention: {provider} keeps your prompts under its consumer terms and may train on them unless you opt out. "
    "Running a bot on a subscription may break {provider}'s terms. Install it and sign in on the machine that runs "
    "it first; see docs/acp-agents.md."
)
# What Dom may do: switches the owner flips from the dashboard. Each is read from app.config on every use, so a save
# applies at once, without a restart.
PERMISSIONS = (
    Permission("memory_self_edit", ("memory_self_edit",), "Let Dom edit his own memory",
               "Dom proposes each change to his notes in your DM; nothing changes until you approve it."),
    Permission("openers", ("openers", "enabled"), "Start conversations in your DM",
               "Dom may message you first a few times a day."),
    Permission("assistant", ("assistant", "enabled"), "Assistant mode",
               "Dom keeps your day in view in your DM: a morning plan, an evening wrap-up and check-ins on reminders "
               "and goals. Off, he is the friend he is. Never in groups or friends’ DMs."),
    Permission("openers_groups", ("openers", "groups"), "Start conversations in groups",
               "Dom may message first in approved groups after three quiet hours."),
    Permission("openers_friends", ("openers", "friends"), "Start conversations in friends’ DMs",
               "Dom may message first in approved friends’ DMs after 45 quiet minutes."),
    Permission("ephemeral_replies", ("ephemeral_replies",), "Private replies in groups",
               "Dom may answer you privately inside a group where he is an admin, or in your DM where he is not."),
    Permission("bot_media", ("dispatch", "bot_media"), "See and hear media from other bots",
               "Dom may inspect photos, stickers, voice notes and videos sent by other bots (never documents). "
               "Off by default so another bot cannot spend your media budget."),
    Permission("workspace_share", ("workspace", "share_with_chats"), "Let groups and friends read the shared folder",
               "Dom may list and read the files in the workspace's shared folder for groups and friends’ DMs, never "
               "write, and nothing else in the workspace. Documents they send then land there, readable by their chat only."),
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
               "Dom updates his Telegram profile photo when his Everywhere preset’s outfit changes, at most once per "
               "the interval you choose."),
    # Subscription agents (pilot, v0.8.6 W7). Shown on the Models tab, not under What Dom may do; each switch rebuilds
    # the model routes so it applies at once.
    Permission("acp_codex", ("acp", "codex", "enabled"), "Codex (OpenAI subscription)", ACP_DESCRIPTION.format(
        agent="Codex", plan="ChatGPT", provider="OpenAI", command="codex-acp")),
    Permission("acp_claude", ("acp", "claude", "enabled"), "Claude (Anthropic subscription)", ACP_DESCRIPTION.format(
        agent="Claude", plan="Claude", provider="Anthropic", command="claude-agent-acp")),
    Permission("acp_antigravity", ("acp", "antigravity", "enabled"), "Antigravity (Google subscription)", ACP_DESCRIPTION.format(
        agent="Antigravity", plan="Google AI", provider="Google", command="agy acp")),
)
# What a studio switch needs before it can be turned on, and where the owner sets that up.
STUDIO_NEEDS = {
    "voice": "Set up the studio or a voice backup first.",
    "video_notes": "Set up the studio or a video backup first.",
    "images": "Set up the studio or a photo backup first.",
    "special": "Set up the studio or a video backup first.",
    "profile_photo_follows_preset": "Set up the local studio first: the profile photo uses its portraits.",
}
PermissionId = Literal[
    "memory_self_edit", "openers", "assistant", "openers_groups", "openers_friends", "ephemeral_replies", "bot_media",
    "workspace_share", "leak_check", "studio_voice", "studio_video", "studio_images", "studio_special", "studio_profile",
    "acp_codex", "acp_claude", "acp_antigravity",
]
assert set(get_args(PermissionId)) == {item.id for item in PERMISSIONS}


class PermissionView(BaseModel):
    id: PermissionId
    label: str
    description: str
    enabled: bool
    # False when nothing set up can serve this studio switch, or a subscription agent cannot be started; `needs` says
    # what to set up first, or why not. A switch that is on although nothing serves it stays on (a studio one sends
    # text instead; an agent is refused at each call) and can always be turned off.
    available: bool = True
    needs: str | None = None


class DesktopStepsView(BaseModel):
    value: int
    min: int = MIN_DESKTOP_STEPS
    max: int = MAX_DESKTOP_STEPS


class BoundedFloat(BaseModel):
    value: float
    min: float
    max: float


class BoundedInt(BaseModel):
    value: int
    min: int
    max: int


class ReplyControlsView(BaseModel):
    triage_confidence: BoundedFloat
    bot_depth_limit: BoundedInt
    bot_reply_interval_seconds: BoundedInt


class Permissions(BaseModel):
    revision: str
    items: list[PermissionView]
    desktop_steps: DesktopStepsView
    profile_photo_every_days: Literal[1, 7, 14, 30]
    reply_controls: ReplyControlsView


class ProfileIntervalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    days: Literal[1, 7, 14, 30]


class DesktopStepsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    max_steps: int = Field(ge=MIN_DESKTOP_STEPS, le=MAX_DESKTOP_STEPS)


class ReplyControlsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    triage_confidence: float | int = Field(ge=MIN_TRIAGE_CONFIDENCE, le=MAX_TRIAGE_CONFIDENCE, allow_inf_nan=False)
    bot_depth_limit: int = Field(ge=MIN_BOT_DEPTH_LIMIT, le=MAX_BOT_DEPTH_LIMIT)
    bot_reply_interval_seconds: int = Field(ge=MIN_BOT_REPLY_INTERVAL_SECONDS, le=MAX_BOT_REPLY_INTERVAL_SECONDS)

    @field_validator("triage_confidence", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("triage_confidence must be a number")
        return value


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


def _unserved(config: Config, permission: Permission) -> str | None:
    """What stops the switch from being turned on, or None when it can be: a studio switch needs something set up that
    can serve it (a backup counts once its key has a value), and a subscription agent with no way to turn its own
    tools off never starts. Reads .env: call it off the event loop."""
    def media_key(name: str) -> bool:  # a backup whose key is also chat's never serves
        try:
            return bool(config.media_secret(name))
        except (OSError, ValueError):
            return False

    if permission.path[0] == "acp":
        reason = ACP_NO_TOOLS_OFF.get(permission.path[1])
        return f"Can't be switched on: {reason}." if reason else None
    if permission.path[0] == "studio" and not switch_servable(config.studio, permission.path[1], media_key):
        return STUDIO_NEEDS[permission.path[1]]
    return None


def _permissions(snapshot: Snapshot) -> Permissions:
    config = snapshot.config
    items = []
    for item in PERMISSIONS:
        needs = _unserved(config, item)
        items.append(PermissionView(
            id=item.id, label=item.label, description=item.description, enabled=_permission_value(config, item),
            available=needs is None, needs=needs,
        ))
    return Permissions(
        revision=snapshot.revision, items=items,
        desktop_steps=DesktopStepsView(value=config.desktop.max_steps),
        profile_photo_every_days=config.studio.profile_photo_every_days,
        reply_controls=ReplyControlsView(
            triage_confidence=BoundedFloat(
                value=config.dispatch.triage_confidence, min=MIN_TRIAGE_CONFIDENCE, max=MAX_TRIAGE_CONFIDENCE,
            ),
            bot_depth_limit=BoundedInt(
                value=config.dispatch.bot_depth_limit, min=MIN_BOT_DEPTH_LIMIT, max=MAX_BOT_DEPTH_LIMIT,
            ),
            bot_reply_interval_seconds=BoundedInt(
                value=config.dispatch.bot_reply_interval_seconds,
                min=MIN_BOT_REPLY_INTERVAL_SECONDS, max=MAX_BOT_REPLY_INTERVAL_SECONDS,
            ),
        ),
    )


def _with_permission(config: Config, path: tuple[str, ...], value: bool | int) -> Config:
    """A copy of the running config with one setting changed; everything else stays as it runs now."""
    key, *rest = path
    if not rest:
        return config.model_copy(update={key: value})
    return config.model_copy(update={key: _with_permission(getattr(config, key), tuple(rest), value)})


@router.get(PERMISSIONS_PATH)
async def read_permissions(request: Request) -> Permissions:
    app = owner_app(request)
    return await asyncio.to_thread(lambda: _permissions(_snapshot(app.config.home)))


@router.post(PERMISSIONS_PATH)
async def update_permission(request: Request) -> Permissions:
    app = owner_app(request)
    update = await _input(request, PermissionUpdate)
    permission = next(item for item in PERMISSIONS if item.id == update.id)
    # Reading, checking and replacing config.toml waits on the disk; it runs off the event loop, one save at a time,
    # so a slow disk never stalls Dom's other work.
    def refuse_unserved(config: Config) -> None:  # turning a switch off is always allowed
        if update.enabled and (needs := _unserved(config, permission)):
            raise HTTPException(409, needs)

    async def turned_on(was_on: bool) -> None:
        if permission.id == "studio_profile" and update.enabled and not was_on:
            await app.store.kv_set(PROFILE_AT, "")  # the first sync after turning it on may happen at once
        if permission.path[0] == "acp":
            app.apply_model_routes(app.config)  # the agents sit in the escalation and drafting chains, built at once

    saved = await _save_now(app, update.revision, permission.path, update.enabled, refuse_unserved, turned_on)
    return await asyncio.to_thread(_permissions, saved)


async def _save_now(
    app: App, revision: str, path: tuple[str, ...], value: bool | int,
    check: Callable[[Config], None] | None = None, after: Callable[[bool], Awaitable[None]] | None = None,
) -> Snapshot:
    """Save one setting to config.toml and the running config, or to neither: one config write at a time, the disk
    work off the loop, and shielded, so a cancelled request still finishes the swap (and `after`, given whether the
    setting was on before)."""

    async def run() -> Snapshot:
        async with _CONFIG_WRITE:
            was_on = bool(_permission_value(app.config, Permission("", path, "", "")))
            saved = await asyncio.to_thread(_save_setting, app.config.home, revision, path, value, check)
            app.config = _with_permission(app.config, path, value)
            if after is not None:
                await after(was_on)
            return saved

    return await asyncio.shield(asyncio.ensure_future(run()))


@router.post(PROFILE_INTERVAL_PATH)
async def update_profile_interval(request: Request) -> Permissions:
    """How often the profile photo may change at most. The next sync reads it, so a save applies at once."""
    app = owner_app(request)
    update = await _input(request, ProfileIntervalUpdate)
    saved = await _save_now(app, update.revision, PROFILE_INTERVAL, update.days)
    return await asyncio.to_thread(_permissions, saved)


@router.post(DESKTOP_STEPS_PATH)
async def update_desktop_steps(request: Request) -> Permissions:
    """The step budget of the owner's desktop tasks. A task reads it when it starts, so a save applies to the next one."""
    app = owner_app(request)
    update = await _input(request, DesktopStepsUpdate)
    saved = await _save_now(app, update.revision, DESKTOP_STEPS, update.max_steps)
    return await asyncio.to_thread(_permissions, saved)


@router.post(REPLY_CONTROLS_PATH)
async def update_reply_controls(request: Request) -> Permissions:
    """Triage confidence and bot-to-bot loop guards. Dispatch reads them per turn, so a save applies at once."""
    app = owner_app(request)
    update = await _input(request, ReplyControlsUpdate)
    changes: dict[tuple[str, ...], bool | int | float | str | None] = {
        ("dispatch", "triage_confidence"): round(float(update.triage_confidence), 4),
        ("dispatch", "bot_depth_limit"): update.bot_depth_limit,
        ("dispatch", "bot_reply_interval_seconds"): update.bot_reply_interval_seconds,
    }
    saved = await save_values(
        app, update.revision, changes,
        lambda config: {"dispatch": config.dispatch},
        "Could not save reply and bot-loop settings.",
    )
    return await asyncio.to_thread(_permissions, saved)


async def save_values(
    app: App, revision: str, changes: dict[tuple[str, ...], bool | int | float | str | None],
    live: Callable[[Config], dict], failure: str, env: dict[str, str | None] | None = None,
    prepare: Callable[[tomlkit.TOMLDocument, dict], dict] | None = None,
    after: Callable[[], None] | None = None,
) -> Snapshot:
    """Write config.toml values (and `env` secrets to .env, once the new config has validated) and hand the running
    config only what this save owns, so a disk-only edit elsewhere is not applied with it. One config write at a time,
    off the loop; a cancelled request still finishes the swap."""

    def write() -> Snapshot:
        current = _snapshot(app.config.home)
        _check_revision(current, revision)
        document = tomlkit.parse(current.text)
        plain = prepare(document, changes) if prepare is not None else changes  # `prepare` takes the paths it owns
        for path, value in plain.items():
            set_value(document, path, value)
        text = tomlkit.dumps(document)
        saved = Snapshot(validate_config(current.config.home, tomllib.loads(text)), text)
        if env:
            set_dotenv_many(current.config.home, env)
        _write(current, saved)
        return saved

    async def run() -> Snapshot:
        async with _CONFIG_WRITE:
            try:
                saved = await asyncio.to_thread(write)
            except (OSError, ValueError):
                raise HTTPException(409, failure) from None
            app.config = app.config.model_copy(update=live(saved.config))
            if after is not None:
                after()
            return saved

    return await asyncio.shield(asyncio.ensure_future(run()))


def _save_setting(
    home: Path, revision: str, path: tuple[str, ...], setting: bool | int,
    check: Callable[[Config], None] | None = None,
) -> Snapshot:
    current = _snapshot(home)
    _check_revision(current, revision)
    if check is not None:
        check(current.config)
    document = tomlkit.parse(current.text)
    set_value(document, path, setting)
    text = tomlkit.dumps(document)
    try:
        saved = Snapshot(validate_config(current.config.home, tomllib.loads(text)), text)
        _write(current, saved)
    except (OSError, ValueError):
        raise HTTPException(409, "Could not save the setting. Check config.toml and its permissions.") from None
    return saved


def set_value(document: tomlkit.TOMLDocument, path: tuple[str, ...], setting: bool | int | str | None) -> None:
    """Set one value in config.toml's document, creating its tables and keeping the comment beside it; None drops the
    key, so its default applies."""
    table = document
    *parents, key = path
    for name in parents:
        if name not in table:
            if setting is None:
                return
            table[name] = tomlkit.table()
        table = table[name]
    if setting is None:
        table.pop(key, None)
        return
    value = tomlkit.item(setting)
    if key in table and hasattr(table[key], "trivia"):  # keep the comment beside the value
        value.trivia.comment_ws = table[key].trivia.comment_ws
        value.trivia.comment = table[key].trivia.comment
    table[key] = value


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


DECISIONS_REFUSALS = {
    401: "The provider refused the key. Check its value in .env.",
    402: "The provider refused it: the account is out of credits.",
    403: "The provider refused it: this key may not use this model.",
    404: "The provider doesn't offer that decisions model. Pick one from the list.",
    429: "The provider is limiting requests on this key. Try again in a minute.",
}


def _decisions_refusal(response: httpx.Response) -> str:
    """A refused decisions request in one sentence. A zero-data-retention refusal (the key's policy) says so first."""
    if is_zdr_refusal(response):
        return ("The provider refused it: no endpoint for this model meets your zero data retention policy. "
                "Pick another decisions model.")
    return DECISIONS_REFUSALS.get(response.status_code, f"Provider returned HTTP {response.status_code}.")


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
            error = _decisions_refusal(cause.response)
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
    if role == "embedding":
        return await _probe_embedding(app, http, config, provider)
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
    except ProviderBudgetError as exc:
        error = budget_line(exc.refusal, ZoneInfo(config.timezone))
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


async def _probe_embedding(app: App, http: httpx.AsyncClient, config: Config, provider: ProviderEndpoint) -> TestResult:
    """Embed one short text: the same request the knowledge index makes, recorded in usage like it."""
    local = _is_local(provider.base_url)

    async def allowed() -> bool:  # a local endpoint costs nothing, so the spend cap never blocks testing it
        return local or not await app.dispatch.usage.over_cap(app.config, app.bot, sleep=app.sleep)

    start = monotonic()
    key: str | None = None
    try:
        _validate_slot_for_role(
            "embedding", ProviderInput(base_url=provider.base_url, model=provider.model, api_key_env=provider.api_key_env or None),
        )
        _check_keys(config, [provider])
        key = config.secret(provider.api_key_env) if provider.api_key_env else None
        client = EmbeddingProvider(provider, key, http, usage=app.dispatch.usage)
        async with asyncio.timeout(TEST_TIMEOUT):
            with model_request("embedding", None, allowed):
                batch = await client.embed(["OK"])
    except KeyPairing as exc:
        error = str(exc)
    except ValidationError:
        error = "Fix this provider's URL, model or key variable in settings."
    except ModelCallSkipped:
        error = "The daily spend cap has been reached."
    except MissingProviderKey:
        error = MISSING_KEY
    except (TimeoutError, httpx.TimeoutException):
        error = "Provider test timed out."
    except httpx.HTTPStatusError as exc:
        error = f"Provider returned HTTP {exc.response.status_code}."
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        error = "Could not reach the provider."
    except (ValueError, KeyError, IndexError, TypeError):
        error = "Provider did not return a valid embedding."
    else:
        dimensions = len(batch.vectors[0]) if batch.vectors else 0
        return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=f"{dimensions} dimensions")
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=_redact(error, key))
