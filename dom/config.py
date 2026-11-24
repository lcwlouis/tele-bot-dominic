"""Instance configuration: ``DOM_HOME/config.toml`` plus secrets from ``.env``."""

from __future__ import annotations

import copy
import ipaddress
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit, urlunsplit

import httpx

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SearchProvider = Literal["searxng", "openrouter", "deepseek", "tavily"]
SEARCH_PROVIDERS = get_args(SearchProvider)

ModelRole = Literal["chat", "fallback", "media", "drafting", "search", "escalation"]
MODEL_ROLES = get_args(ModelRole)

# The service a saved provider talks to: any OpenAI-compatible endpoint, OpenRouter, Google Gemini (direct, for media)
# or a search service.
ProviderKind = Literal["openai", "openrouter", "gemini", "searxng", "deepseek", "tavily"]
PROVIDER_KINDS = get_args(ProviderKind)
# What a saved provider is for (owner decision E70): chat completions (the model roles), studio media (the cloud
# backups), OpenRouter's Decisions API (triage) or web search. One service can be saved once per type, each with
# its own key variable.
ProviderType = Literal["chat", "media", "decisions", "search"]
PROVIDER_TYPES = get_args(ProviderType)
SEARCH_KINDS = ("searxng", "deepseek", "tavily")
TYPE_KINDS: dict[str, tuple[str, ...]] = {
    "chat": ("openai", "openrouter"),
    "media": ("openrouter", "gemini"),
    "decisions": ("openrouter",),
    "search": SEARCH_KINDS,
}
# A media provider's address is fixed by its service: the studio's cloud backups call these (StudioFallback.base_url).
MEDIA_BASE_URLS = {"openrouter": "https://openrouter.ai/api/v1", "gemini": "https://generativelanguage.googleapis.com/v1beta"}


def default_type(kind: str) -> ProviderType:
    """The type of a provider table that names none: search for the search services, else chat."""
    return "search" if kind in SEARCH_KINDS else "chat"

# A provider reports its own effort names (low, medium, high, and more); config.toml takes any one lowercase word.
EFFORT_PATTERN = r"^[a-z][a-z0-9_-]{0,23}$"
PROVIDER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$"


def deepseek_chat_host(host: str | None) -> bool:
    return bool(host and host.rstrip(".").lower() == "api.deepseek.com")


# DeepSeek's own API: thinking stays off unless a route asks for one of these levels (api-docs.deepseek.com/guides/thinking_mode).
DEEPSEEK_EFFORTS = ("low", "high", "max")
DEEPSEEK_EFFORT_ERROR = "Direct DeepSeek takes a low, high or max reasoning effort, or none to keep thinking off."

# What OpenRouter may do with a studio backup's request (its provider routing preferences). zdr routes only to
# zero-data-retention endpoints; no_training excludes endpoints that may train on or sell the data
# (data_collection "deny") but allows ones that keep it; any adds no preference. The account's own privacy settings
# apply on top: "ZDR endpoints only" there still refuses every non-ZDR endpoint. Only media backups take a policy:
# routes that carry chat content through OpenRouter always send zdr.
DataPolicy = Literal["zdr", "no_training", "any"]
DATA_POLICIES = get_args(DataPolicy)
DATA_POLICY_FIELD = (
    "OpenRouter data policy for this media backup: zdr (zero data retention, the default), no_training (providers that "
    "may keep but never train on the data) or any. Ignored by direct Gemini."
)
MEDIA_KEY_RULE = (
    "A studio backup needs its own key variable, such as OPENROUTER_MEDIA_API_KEY: {name} is also used by a model "
    "role or a saved provider. Use a separate variable for studio backups."
)
MEDIA_PROVIDER_KEY_RULE = (
    "A media provider needs its own key variable, such as OPENROUTER_MEDIA_API_KEY: {name} is also used by a model "
    "role or a chat or decisions provider. Use a separate variable for media."
)
MEDIA_ADDRESS_RULE = "A media provider uses its service's fixed address: {url}."


def routing_preferences(policy: DataPolicy) -> dict:
    """The `provider` preferences an OpenRouter request carries for a route's data policy."""
    if policy == "zdr":
        return {"zdr": True}
    if policy == "no_training":
        return {"data_collection": "deny"}
    return {}


class ProviderEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str = Field(description="Provider API base URL, including its API version path.")
    model: str = Field(
        "",
        description="Provider model ID. Required for chat, fallback, media, drafting, escalation, triage and OpenRouter web search; leave empty for SearXNG, DeepSeek web search and Tavily.",
    )
    api_key_env: str | None = Field(None, description="Secret variable name. Leave unset for a compatible endpoint without authentication.")
    reasoning_effort: str | None = Field(
        None, pattern=EFFORT_PATTERN,
        description="Thinking effort sent to the provider, such as low, medium or high (any level the provider lists). "
        "Omit it to use the provider default. Higher effort takes longer and costs more.",
    )
    provider: SearchProvider | None = Field(
        None, description="Search provider for models.search: searxng, openrouter, deepseek or tavily.",
    )

    @model_validator(mode="after")
    def _direct_deepseek_effort(self) -> ProviderEndpoint:
        if self.reasoning_effort is not None and self.reasoning_effort not in DEEPSEEK_EFFORTS:
            try:
                host = httpx.URL(self.base_url).host
            except httpx.InvalidURL:
                return self
            if deepseek_chat_host(host):
                raise ValueError(DEEPSEEK_EFFORT_ERROR)
        return self


class ModelEndpoint(ProviderEndpoint):
    fallbacks: tuple[ProviderEndpoint, ...] = Field((), description="Additional providers to try in order when this role's primary fails.")


class ProviderSetup(BaseModel):
    """A provider saved from the dashboard's Providers tab. Model roles and studio backups keep their own address and
    key variable, so routing never depends on this table; the dashboard uses it to offer each role the providers of
    its type and to fetch their model lists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProviderKind = Field(
        "openai", description="The service: openai (any OpenAI-compatible endpoint), openrouter, gemini (direct Google "
        "Gemini, media only), searxng, deepseek or tavily.",
    )
    type: ProviderType = Field(
        "chat", description="What the provider is for: chat (model roles), media (studio cloud backups), decisions "
        "(triage) or search. Unset means search for the search services, else chat.",
    )
    base_url: str = Field(description="Provider API base URL, including its API version path.")
    api_key_env: str | None = Field(None, description="Secret variable name. Leave unset for an endpoint without authentication.")

    @model_validator(mode="before")
    @classmethod
    def _typed(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if data.get("kind") == "jev":  # before v0.8.3 a decisions provider was its own kind, always OpenRouter's API
            data = {**data, "kind": "openrouter", "type": data.get("type", "decisions")}
        if "type" not in data:
            data = {**data, "type": default_type(data.get("kind", "openai"))}
        return data

    @model_validator(mode="after")
    def _kind_fits_type(self) -> ProviderSetup:
        if self.kind not in TYPE_KINDS[self.type]:
            raise ValueError(f"A {self.type} provider uses {' or '.join(TYPE_KINDS[self.type])}, not {self.kind}.")
        fixed = MEDIA_BASE_URLS.get(self.kind) if self.type == "media" else None
        if fixed and self.base_url.rstrip("/") != fixed:
            raise ValueError(MEDIA_ADDRESS_RULE.format(url=fixed))
        return self


class OpenersConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(False, description="Allow Dom to start conversations in the owner's DM.")
    groups: bool = Field(False, description="Allow Dom to start conversations in approved groups.")
    friends: bool = Field(False, description="Allow Dom to start conversations in approved friends' DMs.")
    max_per_day: int = Field(2, ge=0, description="Maximum number of openers per chat per day in the configured timezone.")


class SelfWorkConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(
        True,
        description=(
            "Let Dom plan up to three small tasks of his own in his nightly daydream and run them in a local desktop "
            "container once a verified network guard blocks the LAN. Inert without a local desktop host."
        ),
    )
    delay_hours: float = Field(3, ge=0, le=48, allow_inf_nan=False, description="Hours after the plan is announced before a task may start.")
    max_minutes: int = Field(20, ge=1, le=60, description="Largest working-time budget, in minutes, that one task may plan.")
    max_steps: int = Field(25, ge=1, le=60, description="Largest step budget (tool calls and model turns) that one task may plan.")


class PresenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    seen_reaction: bool = Field(
        True,
        description=(
            "While a DM is online, react 👀 to the newest message Dom read when he answers with nothing (never in groups). "
            "It stands in for a read receipt and is never sent while he is asleep or offline."
        ),
    )


class WebConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(False, description="Serve the owner dashboard inside the bot process.")
    bind: str = Field("127.0.0.1", description="Dashboard listener address. Use loopback with a development token.")
    port: int = Field(8080, ge=1, le=65535, description="Dashboard listener TCP port.")
    access_team: str = Field("", description="Bare Cloudflare Access team name.")
    access_aud: str = Field("", description="Cloudflare Access application audience tag.")
    access_emails: list[str] = Field([], description="Optional allowlist for the Access assertion's email claim.")
    dev_token_env: str = Field("", description="Variable holding a local dashboard bearer token, at least 32 characters. Mutually exclusive with Access settings.")
    public_url: str = Field("", description="HTTPS dashboard base URL for links in desktop handoff messages.")


HOST_NAME = r"^[a-z0-9][a-z0-9-]{0,23}$"
LOCAL_MEMORY_FLOOR_MB = 512  # what a local host keeps free, besides the container's own limit, unless set


class DesktopHost(BaseModel):
    """A machine that runs desktop containers. A local host is this machine's Docker; a remote host is one
    sandbox API that Dom connects to (it cannot start containers there)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=HOST_NAME, description="Host name: lowercase letters, digits and hyphens. A remote host's name is also its container's name.")
    mode: Literal["local", "remote"] = Field(description="local runs Docker containers on this machine. remote connects to one sandbox API.")
    url: str = Field("", description="Sandbox API base URL of a remote host.")
    api_key_env: str = Field("SANDBOX_API_KEY", description="Secret variable for a remote host's sandbox API key.")
    remote_isolation_confirmed: bool = Field(False, description="Set after verifying the remote host cannot reach your LAN or Dom host.")
    max_containers: int = Field(1, ge=1, description="Most containers that may run at once on a local host. A remote host runs one.")
    min_free_memory_mb: int | None = Field(
        None, ge=0,
        description="Memory the host must still have free after a container's own limit. Dom refuses to start a container that would breach it. Unset means 512 for a local host and 0 (unchecked) for a remote one.",
    )

    @model_validator(mode="after")
    def _limits(self) -> DesktopHost:
        if self.mode == "remote" and self.max_containers != 1:
            raise ValueError("a remote host runs one container; list another host for more")
        return self

    @property
    def memory_floor_mb(self) -> int:
        if self.min_free_memory_mb is not None:
            return self.min_free_memory_mb
        return LOCAL_MEMORY_FLOOR_MB if self.mode == "local" else 0


MIN_DESKTOP_STEPS, MAX_DESKTOP_STEPS = 10, 100


class DesktopConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    image: str = Field("ghcr.io/agent-infra/sandbox:latest", description="Pre-pulled local sandbox image, used by every local container.")
    url: str = Field("", description="Sandbox API base URL in remote mode.")
    api_key_env: str = Field("SANDBOX_API_KEY", description="Secret variable for the remote sandbox API key. Local mode generates a key.")
    start_timeout_seconds: float = Field(
        180, gt=0, allow_inf_nan=False, description="Seconds for the local sandbox API and browser to become ready.",
    )
    idle_minutes: float = Field(10, gt=0, allow_inf_nan=False, description="Idle minutes before a local desktop container stops. Each container has its own timer.")
    profile_dir: str = Field("", description="Local browser profile outside the instance home. Empty uses the sibling desktop-profile directory. A container named x keeps its profile in the same directory name plus -x.")
    network: str = Field("dom-desktop", description="Docker bridge name for local isolation. Every local container joins it.")
    subnet: str = Field("172.30.0.0/24", description="IPv4 CIDR of the desktop bridge. Choose a subnet separate from your LAN and other bridges.")
    port: int = Field(18080, ge=1, le=65535, description="Loopback host port for the default local container. Further containers take the next free ports.")
    max_steps: int = Field(
        30, ge=MIN_DESKTOP_STEPS, le=MAX_DESKTOP_STEPS,
        description="Step budget (tool calls and model turns) of a desktop task the owner asked for. When a task is about "
        "to run out, the owner may extend it, up to three times this budget in all. Dom's own tasks keep their planned budgets.",
    )
    task_cost_cap_usd: float = Field(0.5, ge=0, allow_inf_nan=False, description="Per-desktop-task USD limit. Zero disables this limit.")
    lan_probes: list[str] = Field([], description="LAN targets local mode must prove it cannot reach, as IPv4:port or <name>.local:port (resolved on every check). Required in local mode.")
    probe_image: str = Field("alpine:3.22", description="Pre-pulled image used for local isolation probes.")
    probe_control: str = Field("1.1.1.1:443", description="Public IPv4:port target that the local isolation probe must reach.")
    remote_isolation_confirmed: bool = Field(False, description="Set after verifying the remote desktop cannot reach your LAN or Dom host.")
    fallback_usd_per_1k_tokens: float = Field(0.002, ge=0, allow_inf_nan=False, description="USD per 1,000 tokens when a desktop call reports no cost. Used for the task limit only.")
    hosts: list[DesktopHost] = Field([], description="Desktop hosts, as [[desktop.hosts]] tables. Empty means one host built from desktop.mode and the keys above.")

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, hosts: list[DesktopHost]) -> list[DesktopHost]:
        if len({host.name for host in hosts}) != len(hosts):
            raise ValueError("desktop host names must be unique")
        if sum(host.mode == "local" for host in hosts) > 1:
            raise ValueError("list at most one local host: Docker runs on this machine")
        return hosts

    @field_validator("lan_probes")
    @classmethod
    def _probe_targets(cls, targets: list[str]) -> list[str]:
        for target in targets:
            host, _, _ = target.rpartition(":")
            if mdns_name(host):
                _port(target)
            else:
                _ipv4_target(target)
        return targets

    @field_validator("probe_control")
    @classmethod
    def _control_target(cls, target: str) -> str:
        if not _ipv4_target(target).is_global:
            raise ValueError("probe_control must be a public IPv4 address")
        return target


_MDNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


def mdns_name(host: str) -> bool:
    """A multicast DNS host name such as `<name>.local`: the LAN resolves it, so the address may change."""
    labels = host.split(".")
    return (
        len(host) <= 253 and len(labels) >= 2 and labels[-1].lower() == "local"
        and all(label.isascii() and _MDNS_LABEL.fullmatch(label) for label in labels[:-1])
    )


def _port(target: str) -> int:
    port = target.rpartition(":")[2]
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("probe targets must look like ipv4:port or <name>.local:port")
    return int(port)


def _ipv4_target(target: str) -> ipaddress.IPv4Address:
    host, _, port = target.rpartition(":")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError("probe targets must look like ipv4:port or <name>.local:port") from None
    _port(target)
    return address


class T3Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(False, description="Allow the T3 Code bridge in the owner's DM.")
    port: int = Field(39731, ge=1, le=65535, description="Loopback port for the T3 Code user service.")
    base_url: str = Field(
        default_factory=lambda data: f"http://127.0.0.1:{data['port']}",
        description="Loopback HTTP origin for T3 OAuth and MCP. Defaults to 127.0.0.1 at the configured port.",
    )
    version: str = Field(
        "0.1.10",
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$",
        description="Exact version of the installed t3 npm package.",
    )
    node: str = Field("~/.local/node/bin/node", description="Path to the user-local Node.js executable.")


REPO_URL = "https://github.com/lcwlouis/tele-bot-dominic.git"


class UpdateConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo_url: str = Field(REPO_URL, description="Public Git repository whose vX.Y.Z tags /update offers and deploys (https).")

    @field_validator("repo_url")
    @classmethod
    def _repo(cls, url: str) -> str:
        # https only (file:// for local mirrors): no ssh prompts, no git transports that run commands.
        if not re.fullmatch(r"(?:https://[^\s/@]+|file://)/[^\s]+", url):
            raise ValueError("update.repo_url must be an https:// (or file://) Git URL")
        return url


KNOWN_SECRETS = ("TELEGRAM_BOT_TOKEN", "JEV_API_KEY", "KLIPY_API_KEY")


OUTFIT_NAME = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"


class StudioTier(BaseModel):
    voice_daily: int = Field(1, ge=0, description="Voice notes per rolling 24 hours.")
    video_weekly: int = Field(1, ge=0, description="Round video notes per rolling week when the daily limit is unset.")
    video_daily: int | None = Field(None, ge=0, description="Round video notes per rolling 24 hours when set.")
    image_daily: int | None = Field(None, ge=0, description="Images per rolling 24 hours when set.")
    image_weekly: int = Field(1, ge=0, description="Images per rolling week when the daily limit is unset.")
    special_daily: int = Field(0, ge=0, description="Special video notes per rolling 24 hours. Only the owner DM can use them.")
    gap_minutes: int = Field(360, ge=0, le=10080, description="Minimum minutes between generation deliveries in a chat.")


class OwnerStudioTier(StudioTier):
    voice_daily: int = Field(3, ge=0)
    video_daily: int | None = Field(1, ge=0)
    image_daily: int | None = Field(2, ge=0)
    special_daily: int = Field(1, ge=0)
    gap_minutes: int = Field(30, ge=0, le=10080)


class FriendStudioTier(StudioTier):
    video_weekly: int = Field(2, ge=0)
    image_daily: int | None = Field(1, ge=0)
    gap_minutes: int = Field(180, ge=0, le=10080)


class StudioLimits(BaseModel):
    owner_dm: OwnerStudioTier = Field(default_factory=OwnerStudioTier)
    friend_dm: FriendStudioTier = Field(default_factory=FriendStudioTier)
    group: StudioTier = Field(default_factory=StudioTier)
    daily: int = Field(12, ge=0, description="Studio items across all chats in a rolling 24 hours.")


class StudioTimeouts(BaseModel):
    health: float = Field(3, gt=0, allow_inf_nan=False, description="Health check request deadline in seconds.")
    voice: float = Field(65, gt=0, allow_inf_nan=False, description="Voice request deadline in seconds.")
    video_notes: float = Field(185, gt=0, allow_inf_nan=False, description="Round video request deadline in seconds.")
    special: float = Field(425, gt=0, allow_inf_nan=False, description="Special video request deadline in seconds.")
    images: float = Field(125, gt=0, allow_inf_nan=False, description="Image request deadline in seconds.")
    portrait: float = Field(10, gt=0, allow_inf_nan=False, description="Wardrobe portrait request deadline in seconds.")


StudioProvider = Literal["openrouter_image", "openrouter_tts", "gemini_tts", "openrouter_video"]
STUDIO_PROVIDERS = get_args(StudioProvider)


class StudioFallback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: StudioProvider = Field(description="Cloud media provider.")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", description="Provider model ID.")
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="Secret variable paired with the provider's fixed HTTPS origin.")
    paid: bool = Field(False, description="Owner confirms this direct Gemini key uses Paid Services. Required for Gemini. Gemini reports tokens, not a price, so its calls are recorded at no cost and do not count toward daily_spend_cap_usd; only OpenRouter-reported costs do.")
    supports_reference: bool = Field(True, description="For OpenRouter photo models that accept an image reference.")
    allow_without_reference: bool = Field(False, description="Allow cloud photos when an approved reference cannot be sent.")
    data_policy: DataPolicy = Field(
        "zdr", description=DATA_POLICY_FIELD + " OpenRouter video keeps its output for download, so a video backup "
        "needs no_training or any.",
    )

    @property
    def base_url(self) -> str:
        return MEDIA_BASE_URLS[self.kind]

    @property
    def kind(self) -> ProviderKind:
        """The media provider service this backup calls."""
        return "gemini" if self.provider == "gemini_tts" else "openrouter"

    @model_validator(mode="after")
    def _model_id(self) -> StudioFallback:
        if self.provider == "gemini_tts" and ("/" in self.model or ":" in self.model):
            raise ValueError("Direct Gemini requires a bare model ID")
        if self.provider == "openrouter_video" and self.data_policy == "zdr":
            raise ValueError(
                "OpenRouter video keeps each video for download, so no video endpoint offers zero data retention. "
                "Set data_policy to no_training or any for a video backup."
            )
        return self


StudioChain = Literal["voice", "image", "video"]
STUDIO_CHAINS = get_args(StudioChain)
CHAIN_PROVIDERS: dict[str, tuple[str, ...]] = {
    "voice": ("openrouter_tts", "gemini_tts"), "image": ("openrouter_image",), "video": ("openrouter_video",),
}


class StudioConfig(BaseModel):
    enabled: bool = Field(False, description="Connect to a self-hosted media studio.")
    base_url: str = Field("", description="Studio server origin, without /v1.")
    token_env: str = Field("STUDIO_TOKEN", description="Environment variable holding the studio bearer token.")
    voice: bool = Field(False, description="Allow outbound voice notes.")
    video_notes: bool = Field(False, description="Allow everyday round video notes.")
    images: bool = Field(False, description="Allow outbound photos.")
    special: bool = Field(False, description="Allow special video notes in the owner DM.")
    profile_photo_follows_preset: bool = Field(False, description="Use the Everywhere preset's portrait as the bot profile photo.")
    profile_photo_every_days: Literal[1, 7, 14, 30] = Field(
        14, description="Days between profile photo changes at most: 1, 7, 14 or 30. The first change after the switch "
        "is turned on may happen at once, and the photo changes only when the outfit has.",
    )
    default_outfit: str = Field("", pattern=r"^(?:[a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?$",
                               description="Wardrobe ID to use when the active preset has no outfit list.")
    limits: StudioLimits = Field(default_factory=StudioLimits)
    timeouts: StudioTimeouts = Field(default_factory=StudioTimeouts)

    voice_fallbacks: tuple[StudioFallback, ...] = Field((), description="Ordered cloud backups for voice notes.")
    image_fallbacks: tuple[StudioFallback, ...] = Field((), description="Ordered cloud backups for photos.")
    video_fallbacks: tuple[StudioFallback, ...] = Field(
        (), description="Ordered cloud backups for everyday and special round video notes (openrouter_video).",
    )

    @model_validator(mode="after")
    def _fallback_kinds(self) -> StudioConfig:
        for chain in STUDIO_CHAINS:
            kinds = CHAIN_PROVIDERS[chain]
            if any(p.provider not in kinds for p in getattr(self, f"{chain}_fallbacks")):
                raise ValueError(f"studio.{chain}_fallbacks takes {' or '.join(kinds)} providers")
        return self

    @property
    def fallbacks(self) -> tuple[StudioFallback, ...]:
        return self.voice_fallbacks + self.image_fallbacks + self.video_fallbacks

    @field_validator("base_url")
    @classmethod
    def _origin(cls, value: str) -> str:
        if not value:
            return value
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("studio.base_url must be an HTTP(S) origin") from error
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or url.path != "/" or url.query or url.fragment):
            raise ValueError("studio.base_url must be an HTTP(S) origin")
        return str(url).rstrip("/")


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    home: Path
    owner_id: int | None = Field(None, description="Telegram user ID recorded after a successful /claim.")
    timezone: str = Field("UTC", description="IANA timezone for presence and daily jobs. Init writes the machine timezone or the supplied --timezone.")
    models: dict[str, ModelEndpoint] = {}
    provider_setups: dict[str, ProviderSetup] = Field(
        default_factory=dict, alias="providers",
        description="Saved providers by name, the `[providers.<name>]` tables the dashboard's Providers tab writes.",
    )
    desktop_mode: Literal["local", "remote", "off"] = Field("off", description="Desktop mode. Use off to disable it, local for Docker, or remote for a separate sandbox host.")
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    window_budget_tokens: int = Field(
        32000, ge=2000, description="Prompt size at which a chat's window is trimmed. The window grows without sliding "
        "until the chat's prompt (system prefix, notes and messages, as the model reports it) reaches this, so the "
        "model host's prompt cache keeps hitting; then one trim drops the oldest messages down to "
        "`window_keep_tokens`. The default is about 3.5 times Dom's usual prompt (9k tokens), so a trim comes every "
        "few hundred messages, and far below the 262k context of the home model: a bigger budget keeps more history "
        "and trims less often, but every cache miss (after a trim or a note change) then recomputes a longer prompt, "
        "and long prompts answer worse.",
    )
    window_keep_tokens: int = Field(
        20000, ge=1000, description="Prompt size a trim brings the window down to (about; messages are estimated at "
        "one token per three bytes). It must be below `window_budget_tokens`; the gap sets how many messages go in one trim.",
    )
    window_size: int = Field(
        500, ge=10, description="Safety bound on the messages in one chat's window, for chats of very short messages: "
        "past it a trim also happens, down to 60% of it. Not the normal trigger.",
    )
    daily_spend_cap_usd: float = Field(1.0, ge=0, allow_inf_nan=False, description="Daily USD cap for non-owner activity and desktop tasks. Zero disables the cap.")
    openers: OpenersConfig = Field(default_factory=OpenersConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    self_work: SelfWorkConfig = Field(default_factory=SelfWorkConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    t3: T3Config = Field(default_factory=T3Config)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    ephemeral_replies: bool = Field(False, description="Allow owner-only ephemeral replies in groups. Telegram sends these only from a bot that is an admin of the group; Dom knows whether he is one; where he is not, a private reply goes straight to the owner's DM, with a note saying why.")
    memory_self_edit: bool = Field(False, description="Allow Dom to propose edits to his notes in the owner's DM. Each proposal appears on an approval card with a unified diff and a one-line reason; nothing changes until the owner approves.")
    leak_check: bool = Field(True, description="Use Jev to screen eligible outbound text and network plugin calls for private information.")
    leak_threshold: float = Field(1.2, ge=0, le=3, description="Minimum Jev score on a 0–3 scale that holds back an outbound reply or online plugin call.")

    @model_validator(mode="after")
    def _window_marks(self) -> Config:
        if self.window_keep_tokens >= self.window_budget_tokens:
            raise ValueError("window_keep_tokens must be below window_budget_tokens")
        return self

    @model_validator(mode="after")
    def _fallbacks_only_on_roles(self) -> Config:
        for name, endpoint in self.models.items():
            if endpoint.fallbacks and name not in MODEL_ROLES:
                raise ValueError(f"models.{name} cannot have fallbacks; supported roles: {', '.join(MODEL_ROLES)}")
            endpoints = (endpoint, *endpoint.fallbacks)
            if name != "search":
                for item in endpoints:
                    if item.provider is not None:
                        raise ValueError(f"models.{name} cannot set provider; only models.search can")
                    if not item.model:
                        raise ValueError(f"models.{name} requires a model ID")
            else:
                for item in endpoints:
                    if (item.provider or "openrouter") == "openrouter" and not item.model:
                        raise ValueError("OpenRouter search requires a model ID")
        return self

    @model_validator(mode="after")
    def _studio_key_pairing(self) -> Config:
        fallbacks = self.studio.fallbacks
        if not fallbacks:
            return self
        studio = self.studio.model_copy(update={f"{chain}_fallbacks": () for chain in STUDIO_CHAINS})
        reserved = self.model_copy(update={"models": {}, "provider_setups": {}, "studio": studio}).secret_names()
        model_routes = [*(endpoint for _, endpoint in self.model_endpoints()), *self.provider_setups.values()]
        chat_keys = self.chat_keys()
        for provider in fallbacks:
            if provider.api_key_env in chat_keys:  # a media key's looser data policy never reaches chat content
                raise ValueError(MEDIA_KEY_RULE.format(name=provider.api_key_env))
            origin = httpx.URL(provider.base_url).copy_with(path="/")
            if provider.api_key_env in reserved or any(
                endpoint.api_key_env == provider.api_key_env
                and httpx.URL(endpoint.base_url).copy_with(path="/", query=None, fragment=None) != origin
                for endpoint in (*model_routes, *fallbacks)
            ):
                raise ValueError("A studio key variable must be paired only with its provider's HTTPS origin")
        return self

    @model_validator(mode="after")
    def _media_keys_apart(self) -> Config:
        """A media provider's key may take a looser data policy (E66), so no chat or decisions route may share it."""
        chat_keys = self.chat_keys()
        for setup in self.provider_setups.values():
            if setup.type == "media" and setup.api_key_env in chat_keys:
                raise ValueError(MEDIA_PROVIDER_KEY_RULE.format(name=setup.api_key_env))
        return self

    def check_key_rules(self) -> Config:
        """Run the studio and media key rules again, for a config built with model_copy. Raises ValueError."""
        return self._studio_key_pairing()._media_keys_apart()

    def chat_keys(self) -> set[str]:
        """Key variables that carry conversation content: the model roles' and the chat, decisions and search
        providers'. Studio backups and media providers never use one."""
        routes = [*(endpoint for _, endpoint in self.model_endpoints()),
                  *(setup for setup in self.provider_setups.values() if setup.type != "media")]
        return {route.api_key_env for route in routes if route.api_key_env}

    def media_key_shared(self, name: str) -> bool:
        """Whether a studio backup's key variable holds the same value as a key a chat, decisions or search route uses
        under another name. Config validation compares names only; this compares the values, read from .env on each
        call, so a value changed there counts at once."""
        env = _read_dotenv(self.home / ".env")
        value = env.get(name) or os.environ.get(name)
        return bool(value) and any(
            (env.get(other) or os.environ.get(other)) == value for other in self.chat_keys() if other != name)

    def media_secret(self, name: str) -> str | None:
        """A studio backup's key value, or None when it is unset or also a chat route's key (E66): a media key may
        take a looser data policy, so it never carries a credential that chat content uses."""
        return None if self.media_key_shared(name) else self.secret(name)

    @field_validator("provider_setups")
    @classmethod
    def _provider_names(cls, value: dict[str, ProviderSetup]) -> dict[str, ProviderSetup]:
        for name in value:
            if not re.fullmatch(PROVIDER_NAME_PATTERN, name):
                raise ValueError("Provider names use letters, digits, spaces, dots, dashes or underscores (at most 40).")
        return value

    def providers(self, role: ModelRole) -> tuple[ProviderEndpoint, ...]:
        primary = self.models.get(role)
        return (primary, *primary.fallbacks) if primary is not None else ()

    def model_endpoints(self) -> Iterator[tuple[str, ProviderEndpoint]]:
        for name, endpoint in self.models.items():
            yield name, endpoint
            if name in MODEL_ROLES:
                for index, provider in enumerate(endpoint.fallbacks):
                    yield f"{name}.fallbacks[{index}]", provider

    @property
    def desktop_hosts(self) -> list[DesktopHost]:
        """The hosts Dom may use: the [[desktop.hosts]] tables, or one host made from the single-desktop keys."""
        if self.desktop_mode == "off":
            return []
        if self.desktop.hosts:
            return list(self.desktop.hosts)
        return [DesktopHost(
            name="default", mode=self.desktop_mode, url=self.desktop.url, api_key_env=self.desktop.api_key_env,
            remote_isolation_confirmed=self.desktop.remote_isolation_confirmed,
        )]

    def secret_names(self) -> set[str]:
        """Every environment variable that holds an instance credential: the keys named by any `*_env` setting
        (so a new one is covered as soon as it exists) and the fixed names Dom reads."""
        names = set(KNOWN_SECRETS)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(key, str) and key.endswith("_env") and isinstance(item, str) and item:
                        names.add(item)
                    else:
                        walk(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    walk(item)

        walk(self.model_dump(mode="python"))
        return names

    def secret(self, name: str) -> str | None:
        """Read a secret from ``home/.env`` first, then the process environment."""
        return _read_dotenv(self.home / ".env").get(name) or os.environ.get(name)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def starter_config() -> dict:
    starter = resources.files("dom").joinpath("starter_home", "config.toml")
    return tomllib.loads(starter.read_text(encoding="utf-8"))


def load_config(home: Path) -> Config:
    return validate_config(home, tomllib.loads((home / "config.toml").read_text()))


def validate_config(home: Path, raw: dict) -> Config:
    """Apply starter defaults and validate an instance's parsed TOML table."""
    defaults = starter_config()["models"]
    models = {key: defaults[key] for key in ("triage", "media", "drafting")}
    models.update(raw.get("models", {}))
    if "chat_fallback" in models:
        models.setdefault("fallback", models.pop("chat_fallback"))
    if "search" in models and isinstance(models["search"], dict):
        search_raw = models["search"]
        # An OpenRouter search table (the only kind before v0.8.2) keeps taking omitted fields from the starter.
        if search_raw.get("provider") in (None, "openrouter"):
            models["search"] = {**defaults["search"], **search_raw}
    desktop = raw.get("desktop", {})
    mode = desktop.get("mode")
    if mode is None:
        hosts = [host.get("mode") for host in desktop.get("hosts", []) if isinstance(host, dict)]
        # Listing hosts turns the desktop on; the mode key can still switch it off.
        mode = ("local" if "local" in hosts else "remote") if hosts else Config.model_fields["desktop_mode"].default
    return Config.model_validate({**raw, "home": home, "models": models, "desktop_mode": mode})


_OWNER_LINE = re.compile(r"^\s*owner_id\s*=.*$", re.MULTILINE)
_FIRST_TABLE = re.compile(r"^\s*\[", re.MULTILINE)


def save_owner(home: Path, owner_id: int) -> None:
    """Set the root-level ``owner_id`` in config.toml, leaving every other line as it was.

    The new text is parsed and checked before it replaces the file (atomically), and the file is
    checked again afterwards, so a claim is never reported for an owner that was not saved.
    """
    path = home / "config.toml"
    text = path.read_text()
    line = f"owner_id = {owner_id}"
    table = _FIRST_TABLE.search(text)
    at = table.start() if table else len(text)
    root, rest = text[:at], text[at:]  # a key inside a [table] is not the root key
    if _OWNER_LINE.search(root):
        root = _OWNER_LINE.sub(line, root, count=1)
    else:
        if root and not root.endswith("\n"):
            root += "\n"
        root += f"{line}\n" + ("\n" if rest else "")
    new_text = root + rest
    if tomllib.loads(new_text).get("owner_id") != owner_id:
        raise ValueError("could not set owner_id in config.toml")
    _replace_file(path, new_text)
    if tomllib.loads(path.read_text()).get("owner_id") != owner_id:
        raise ValueError("owner_id was not saved to config.toml")


def _replace_file(path: Path, text: str, *, mode: int | None = None, durable: bool = True) -> None:
    """Atomically replace `path`. `durable` waits for the disk (fsync), which can take seconds on an SD card:
    a backup copy that a crash may lose skips it."""
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), mode)
            file.write(text.encode("utf-8"))
            file.flush()
            if durable:
                os.fsync(file.fileno())
            file.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _url_hosts(raw: dict):
    """Each URL-valued key that names a host, as (key path, table, field): model base URLs and desktop sandbox URLs."""
    for name, endpoint in (raw.get("models") or {}).items():
        if isinstance(endpoint, dict) and isinstance(endpoint.get("base_url"), str):
            yield f"models.{name}.base_url", endpoint, "base_url"
        if isinstance(endpoint, dict) and isinstance(endpoint.get("fallbacks"), list):
            for index, provider in enumerate(endpoint["fallbacks"]):
                if isinstance(provider, dict) and isinstance(provider.get("base_url"), str):
                    yield f"models.{name}.fallbacks[{index}].base_url", provider, "base_url"
    desktop = raw.get("desktop") or {}
    if isinstance(desktop.get("url"), str):
        yield "desktop.url", desktop, "url"
    for number, host in enumerate(desktop.get("hosts") or []):
        if isinstance(host, dict) and isinstance(host.get("url"), str):
            yield f"desktop.hosts.{host.get('name', number)}.url", host, "url"


def host_places(raw: dict) -> dict[str, list[str]]:
    """Each host named by a model or desktop URL or a desktop LAN probe, with the keys that name it."""
    places: dict[str, list[str]] = {}
    for key, table, field in _url_hosts(raw):
        try:
            host = urlsplit(table[field]).hostname
        except ValueError:
            host = None
        if host:
            places.setdefault(host, []).append(key)
    for target in (raw.get("desktop") or {}).get("lan_probes") or []:
        if isinstance(target, str) and ":" in target:
            places.setdefault(target.rpartition(":")[0], []).append("desktop.lan_probes")
    return {host: list(dict.fromkeys(keys)) for host, keys in places.items()}


def _moved(raw: dict, old: str, new: str) -> dict:
    """`raw` with IPv4 host `old` replaced by `new` in model and desktop URLs and LAN probes."""
    raw = copy.deepcopy(raw)
    for _, table, field in _url_hosts(raw):
        parts = urlsplit(table[field])
        if parts.hostname == old:
            table[field] = urlunsplit(parts._replace(netloc=parts.netloc.replace(old, new, 1)))
    desktop = raw.get("desktop") or {}
    if isinstance(desktop.get("lan_probes"), list):
        desktop["lan_probes"] = [
            f"{new}:{target.rpartition(':')[2]}" if isinstance(target, str) and target.rpartition(":")[0] == old
            else target for target in desktop["lan_probes"]
        ]
    return raw


def replace_host(home: Path, old: str, new: str) -> None:
    """Move a LAN host from one IPv4 address to another in config.toml, leaving every other line as it was.

    The edited text must parse to exactly the expected change, or nothing is written.
    """
    old, new = str(ipaddress.IPv4Address(old)), str(ipaddress.IPv4Address(new))
    path = home / "config.toml"
    text = path.read_text()
    raw = tomllib.loads(text)
    expected = _moved(raw, old, new)
    if expected == raw:
        raise ValueError(f"config.toml no longer uses {old}")
    new_text = re.sub(rf"(?<![\w.-]){re.escape(old)}(?![\w-]|\.\w)", new, text)
    if tomllib.loads(new_text) != expected:
        raise ValueError(f"config.toml uses {old} somewhere else too; change it by hand")
    validate_config(home, expected)
    _replace_file(path, new_text)
