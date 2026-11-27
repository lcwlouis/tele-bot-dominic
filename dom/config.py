"""Instance configuration: ``DOM_HOME/config.toml`` plus secrets from ``.env``."""

from __future__ import annotations

import copy
import fcntl
import ipaddress
import logging
import os
import re
import stat
import tempfile
import threading
import tomllib
from collections.abc import Iterable, Iterator
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal, get_args
from urllib.parse import urlsplit, urlunsplit

import httpx

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SearchProvider = Literal["searxng", "openrouter", "deepseek", "tavily"]
SEARCH_PROVIDERS = get_args(SearchProvider)

ModelRole = Literal["chat", "fallback", "media", "drafting", "search", "escalation", "embedding"]
# Roles edited by Settings → Models: every routed role. Embedding serves meaning search (dom/knowledge.py).
MODEL_ROLES = ("chat", "fallback", "media", "drafting", "search", "escalation", "embedding")
MODEL_ROUTE_ROLES = get_args(ModelRole)

# The kinds of paid work a monthly limit can cover (dom/usage.py maps each usage kind to one).
SpendArea = Literal["chat", "media", "search", "desktop", "memory"]
SPEND_AREAS = get_args(SpendArea)

# How firmly the outbound leak check holds a message back. Jev scores a message 0-3 against four anchors: 0 nothing
# private, 1 a vague allusion, 2 reveals a private fact, 3 reveals several or sensitive facts. A message is held when its
# score reaches the cutoff. Standard (1.2) sits just past "a vague allusion", the cutoff before levels existed. Strict
# (0.7) holds from a little more than "nothing private", so a hint is enough. Relaxed (2.0) holds only what reveals a
# private fact, so allusions and innocent mentions pass.
LeakScan = Literal["strict", "standard", "relaxed", "off"]
LEAK_SCAN_CUTOFFS = {"strict": 0.7, "standard": 1.2, "relaxed": 2.0}
LEAK_LEVEL_TOLERANCE = 0.1  # a legacy `leak_threshold` this close to a level's cutoff is shown as that level

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
        description="Provider model ID. Required for chat, fallback, media, drafting, escalation, embedding, triage and OpenRouter web search; leave empty for SearXNG, DeepSeek web search and Tavily.",
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
    input: tuple[Literal["text", "image", "audio", "video"], ...] | None = Field(
        None, description="What this model accepts as input, for example [\"text\", \"image\"]. Omit it to use the "
        "provider's model list (OpenRouter lists each model's input types) or, for other OpenAI-compatible servers, a "
        "one-off test with a tiny generated image. Dom sends a photo, voice note or video to the model itself only when "
        "this says the model takes it; with nothing known the model is treated as text-only. Set it to override what "
        "the list or the test found.",
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


PLAN_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class AssistantConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(
        False,
        description=(
            "Assistant mode, owner DM only: Dom keeps your day in view with a morning plan, an evening wrap-up and "
            "check-ins on reminders and goals. Off, he is the friend he is. It never reaches groups or friends' DMs. "
            "Switch it with `/assistant on`, `/assistant off` and `/assistant status` in your DM, or in Settings → What Dom may do."
        ),
    )
    morning: str = Field(
        "08:00",
        description="Local time (24-hour `HH:MM`, in `timezone`) of the morning plan. Empty turns the morning plan off.",
    )
    evening: str = Field(
        "21:30",
        description="Local time (24-hour `HH:MM`, in `timezone`) of the evening wrap-up. Empty turns the wrap-up off.",
    )

    @field_validator("morning", "evening")
    @classmethod
    def _a_time_or_nothing(cls, value: str) -> str:
        if value and not PLAN_TIME.fullmatch(value):
            raise ValueError("a time of day such as 08:00, or empty to turn it off")
        return value


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


class DispatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    fast_lane: bool = Field(
        True,
        description=(
            "Give the owner's DM a compose lane of its own, so his turns (and the turns his approval cards start) "
            "never wait for a turn in another chat. Off, every chat's turns compose one at a time, owner first."
        ),
    )


class PresenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    seen_reaction: bool = Field(
        True,
        description=(
            "While a DM is online, react 👀 to the newest message Dom read when he answers with nothing (never in groups). "
            "It stands in for a read receipt and is never sent while he is asleep or offline."
        ),
    )


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_similarity: float = Field(
        0.35, ge=0, le=1, allow_inf_nan=False,
        description=(
            "Meaning search drops a hit whose embedding cosine similarity to the query is below this. 0.35 suits "
            "qwen3-vl-embedding-2b, where unrelated short texts score about 0.1 to 0.3 and related ones 0.45 and up; "
            "another embedding model may sit on a different scale. Zero keeps every hit."
        ),
    )
    min_score: float = Field(
        0.15, ge=0, le=1, allow_inf_nan=False,
        description=(
            "Meaning search drops a blended hit (keyword relevance and similarity, weighted by recency) scoring "
            "below this. The best possible score is 1; a hit found by meaning alone peaks at 0.55."
        ),
    )


class WorldConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    place: str = Field(
        "", max_length=60,
        description=(
            "The owner's place, as Dom should name it in the weather line of his turn note (for example `Singapore`). "
            "Empty, the default, turns the line off."
        ),
    )
    latitude: float | None = Field(None, ge=-90, le=90, allow_inf_nan=False, description="Latitude of the place, in degrees.")
    longitude: float | None = Field(None, ge=-180, le=180, allow_inf_nan=False, description="Longitude of the place, in degrees.")

    country: str = Field(
        "", max_length=2,
        description=(
            "Two-letter ISO country code (for example `SG`) for the public holidays in Dom's turn note. Independent of "
            "the place. Empty, the default, turns the holidays off."
        ),
    )

    news: bool = Field(
        True,
        description=(
            "Each evening, before Dom sleeps, he searches the web for the top news and the events in the next few days "
            "where the owner is (the `place`, or else the `country`), keeps a few headlines as `memory/NEWS.md`, and his "
            "turn note carries one or two of them for 36 hours. It needs a place or country and a web search route that "
            "returns result lists, and its calls count toward the search spending limit. Off when no search route is set up."
        ),
    )

    @field_validator("country", mode="before")
    @classmethod
    def _country_code(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            code = value.strip().upper()
            if len(code) != 2 or not code.isascii() or not code.isalpha():
                raise ValueError("[world] country is a two-letter ISO country code such as SG")
            return code
        return "" if isinstance(value, str) else value

    @model_validator(mode="after")
    def _all_or_none(self) -> WorldConfig:
        given = [bool(self.place.strip()), self.latitude is not None, self.longitude is not None]
        if any(given) and not all(given):
            raise ValueError("[world] needs place, latitude and longitude together, or none of them")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.place.strip()) and self.latitude is not None and self.longitude is not None


class TracerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    retention_days: int = Field(
        7, ge=1, le=90,
        description=(
            "Days the debug tracer keeps each turn's full trace (prompts, model requests and responses, tool calls and "
            "deliveries, for every chat). Older traces are deleted once a day."
        ),
    )
    max_mb: int = Field(
        256, ge=16, le=10000,
        description=(
            "Size cap of the trace store (`traces.db` in Dom's home), in megabytes of compressed traces. Past it the "
            "oldest traces are deleted first."
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
    public_url: str = Field("", description="HTTPS dashboard base URL for links in Dom's notices and desktop handoff messages, and the base of the Google OAuth redirect URI (`/api/integrations/google/callback`).")


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


KNOWN_SECRETS = (
    "TELEGRAM_BOT_TOKEN", "JEV_API_KEY", "KLIPY_API_KEY", "DOM_MASTER_KEY", "DOM_MASTER_KEY_PREVIOUS",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
)


OUTFIT_NAME = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"


class StudioTier(BaseModel):
    voice_daily: int = Field(1, ge=0, description="Voice notes per rolling 24 hours.")
    video_weekly: int = Field(1, ge=0, description="Round video notes per rolling week when the daily limit is unset.")
    video_daily: int | None = Field(None, ge=0, description="Round video notes per rolling 24 hours when set. false uses the weekly limit.")
    image_daily: int | None = Field(None, ge=0, description="Images per rolling 24 hours when set. false uses the weekly limit.")
    image_weekly: int = Field(1, ge=0, description="Images per rolling week when the daily limit is unset.")
    special_daily: int = Field(0, ge=0, description="Special video notes per rolling 24 hours. Only the owner DM can use them.")
    gap_minutes: int = Field(360, ge=0, le=10080, description="Minimum minutes between generation deliveries in a chat. It applies to every kind that has no gap of its own below.")
    voice_gap_minutes: int | None = Field(None, ge=0, le=10080, description="Minimum minutes between voice notes in a chat, counted from the last voice note only. Unset: the general gap applies.")
    video_gap_minutes: int | None = Field(None, ge=0, le=10080, description="Minimum minutes between round video notes in a chat, counted from the last one only. Unset: the general gap applies.")
    image_gap_minutes: int | None = Field(None, ge=0, le=10080, description="Minimum minutes between photos in a chat, counted from the last photo only. Unset: the general gap applies.")
    special_gap_minutes: int | None = Field(None, ge=0, le=10080, description="Minimum minutes between special video notes in a chat, counted from the last one only. Unset: the general gap applies.")

    @field_validator("video_daily", "image_daily", "voice_gap_minutes", "video_gap_minutes", "image_gap_minutes", "special_gap_minutes", mode="before")
    @classmethod
    def _weekly(cls, value):
        # TOML has no null, so false unsets a daily limit whose default is set, and the weekly limit applies.
        return None if value is False else value


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


# Gemini TTS's prebuilt voices (https://ai.google.dev/gemini-api/docs/speech-generation#voices). A direct Gemini backup, and
# OpenRouter's Google speech models, take only these; other OpenRouter speech models name their own voices.
GEMINI_VOICES = frozenset((
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
    "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam",
    "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
))
DEFAULT_VOICE = "Achird"
StudioProvider = Literal["openrouter_image", "openrouter_tts", "gemini_tts", "openrouter_video"]
STUDIO_PROVIDERS = get_args(StudioProvider)


class StudioFallback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: StudioProvider = Field(description="Cloud media provider.")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", description="Provider model ID.")
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="Secret variable paired with the provider's fixed HTTPS origin.")
    paid: bool = Field(False, description="Owner confirms this direct Gemini key uses Paid Services. Required for Gemini. Gemini reports tokens, not a price, so its calls are recorded at no cost and do not count toward daily_spend_cap_usd; only OpenRouter-reported costs do.")
    voice: str | None = Field(
        None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
        description="Voice backups only: the provider's voice name, such as a Gemini TTS voice (`Achird` when unset). "
        "A direct Gemini backup, and an OpenRouter Google model, accept only Gemini's prebuilt voice names.",
    )
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
        if self.voice is not None:
            if self.provider not in ("openrouter_tts", "gemini_tts"):
                raise ValueError("Only a voice backup takes a voice")
            if (self.provider == "gemini_tts" or self.model.startswith("google/")) and self.voice not in GEMINI_VOICES:
                raise ValueError(f"{self.voice} is not a Gemini voice; use one of: {', '.join(sorted(GEMINI_VOICES))}")
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
    keep_cap_mb: int = Field(
        2048, ge=0, description="Disk space, in MB, for the photos, voice notes and video notes Dom has sent, kept under "
        "the home's media/ folder (media/<chat id>/<month>/<message id>.<ext>). Past it the oldest files are removed; "
        "Telegram's file_id stays on the message so Telegram can still provide them. Zero keeps nothing.",
    )
    look_owner_dm: bool = Field(
        True, description="In the owner's DM, Dom looks at a generated photo (or a video note's first frame, when ffmpeg "
        "is installed) before sending it, and regenerates it once if it does not match the scene. A cloud video "
        "note is redone only when its first frame is clearly wrong. Needs a chat model that accepts images.",
    )
    look_friend_dm: bool = Field(False, description="The same look before sending, in friends' DMs.")
    look_group: bool = Field(False, description="The same look before sending, in groups.")
    video_scenes: bool = Field(
        True, description="Start each video note from a fresh still of Dom in a scene that fits the moment, made by the "
        "image model with the outfit's approved portrait as the identity reference, instead of animating the portrait "
        "itself. The still counts toward the studio-wide daily total and its cost (when a cloud image backup makes it) "
        "is recorded and capped like any photo; it does not use the chat's photo allowance. If the still can't be made, "
        "the note uses the portrait.",
    )
    event_day_max_multiplier: float = Field(
        2, ge=0, le=10, allow_inf_nan=False, description="The most Dom can raise his own media limits on a special day "
        "in his life (a trip, a concert, his birthday): every per-chat limit and the studio-wide daily total are "
        "multiplied by up to this. Each item beyond the normal limit must also fit the month's unused budget under "
        "`monthly_spend_cap_usd`. Zero turns event days off.",
    )
    event_days_per_week: int = Field(
        1, ge=0, le=7, description="Most event days Dom may declare within any rolling week (a cancelled one doesn't count).",
    )
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
    daily_spend_cap_usd: float = Field(1.0, ge=0, allow_inf_nan=False, description="Daily USD cap, the normal pace: once today's paid calls reach it, friends' and groups' chats and their background work pause until midnight in `timezone`. The owner's DM is exempt unless `cap_owner_dm` is on, and so is background work done for it (reflection, the memory pass, the look before a send, aged media), which obeys only the monthly cap and the area limits. Zero disables the cap.")
    monthly_spend_cap_usd: float = Field(0.0, ge=0, allow_inf_nan=False, description="Monthly USD ceiling for the calendar month in `timezone`, over every paid call: chat, media, search, desktop tasks, embeddings, summaries and studio cloud backups. Once reached, friends' and groups' chats and all background work, the owner's included, pause until the 1st; one-offs such as event days draw only on the month's unused budget and never pass it. The owner's own turns in the DM are exempt unless `cap_owner_dm` is on. Zero disables the cap.")
    cap_owner_dm: bool = Field(False, description="Make the owner's DM obey both spend caps. Off (the default), the owner's own chat is never paused by a cap, though what it spends still counts toward them. A cap is checked before each paid call and the cost counts once it returns, so calls already in flight (one per lane, the owner DM's included, plus background work) can finish past it by their own cost.")
    area_spend_caps_usd: dict[SpendArea, Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(
        default_factory=dict,
        description="Optional monthly USD limit per area (`chat`, `media`, `search`, `desktop`, `memory`), as a `[area_spend_caps_usd]` table. An area at its limit pauses that kind of work for the rest of the month while the others go on. An area left out, or set to zero, has no limit of its own.",
    )
    openers: OpenersConfig = Field(default_factory=OpenersConfig)
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    self_work: SelfWorkConfig = Field(default_factory=SelfWorkConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    world: WorldConfig = Field(default_factory=WorldConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    tracer: TracerConfig = Field(default_factory=TracerConfig)
    t3: T3Config = Field(default_factory=T3Config)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    ephemeral_replies: bool = Field(False, description="Allow owner-only ephemeral replies in groups. Telegram sends these only from a bot that is an admin of the group; Dom knows whether he is one; where he is not, a private reply goes straight to the owner's DM, with a note saying why.")
    memory_self_edit: bool = Field(False, description="Allow Dom to propose edits to his notes in the owner's DM. Each proposal appears on an approval card with a unified diff and a one-line reason; nothing changes until the owner approves.")
    core_memory_cap: int = Field(30, ge=1, description="Most core memories (pinned note lines) across all of Dom's notes, the owner's pins included. Core memories never fade and the memory pass never rewrites them. At the cap Dom must unpin one of his own to pin another.")
    media_input_images: int = Field(
        2, ge=0, le=8, description="Most images (photos, stickers) Dom is shown as the real picture in one prompt, newest "
        "first, when his chat model accepts images. Older media stays as its tag in the window. Zero turns image input off.",
    )
    media_input_audio: int = Field(
        1, ge=0, le=4, description="Most voice notes or audio files Dom hears as the real recording in one prompt. "
        "Zero turns audio input off.",
    )
    media_input_video: int = Field(
        1, ge=0, le=2, description="Most videos Dom is shown as the real clip in one prompt, for chat models that accept "
        "video. Zero turns video input off.",
    )
    media_input_max_mb: float = Field(
        6, gt=0, le=20, allow_inf_nan=False, description="Largest attachment, in MB, that is sent to the chat model itself; "
        "a bigger one is described by the media model instead.",
    )
    leak_check: bool = Field(True, description="Use Jev to screen eligible outbound text and network plugin calls for private information.")
    leak_scan: LeakScan | None = Field(
        None,
        description="How strictly the leak check holds messages back: `strict` (0.7), `standard` (1.2) or `relaxed` (2.0) as the minimum Jev score on a 0–3 scale, or `off`, which skips the Jev check (the local check for private text and credentials in public files and plugin inputs always stays on). Unset, the legacy `leak_threshold` applies.",
    )
    leak_threshold: float = Field(1.2, ge=0, le=3, description="Legacy: minimum Jev score on a 0–3 scale that holds back an outbound reply or online plugin call. Used only while `leak_scan` is unset; a value near a level is shown as that level, any other as custom.")
    menu_card_minutes: float = Field(
        45, ge=0, le=2880, allow_inf_nan=False,
        description="Minutes after which Telegram menu cards (`/status`, `/chats`, `/preset`, `/plugins`, `/proposals`, `/reminders`) delete themselves, checked once a minute and at startup. Telegram cannot delete a message older than 48 hours, so a card that old only loses its buttons. Zero keeps cards until closed.",
    )

    @model_validator(mode="after")
    def _window_marks(self) -> Config:
        if self.window_keep_tokens >= self.window_budget_tokens:
            raise ValueError("window_keep_tokens must be below window_budget_tokens")
        return self

    @model_validator(mode="after")
    def _fallbacks_only_on_roles(self) -> Config:
        for name, endpoint in self.models.items():
            if endpoint.fallbacks and name not in MODEL_ROUTE_ROLES:
                raise ValueError(f"models.{name} cannot have fallbacks; supported roles: {', '.join(MODEL_ROUTE_ROLES)}")
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
            if provider.api_key_env in chat_keys or self.media_key_shared(provider.api_key_env):  # a media key's looser data policy never reaches chat content
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
            if setup.type == "media" and setup.api_key_env and (
                setup.api_key_env in chat_keys or self.media_key_shared(setup.api_key_env)
            ):
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

    def media_key_shared(self, name: str, *, extra_chat_values: Iterable[str] = ()) -> bool:
        """Whether a studio backup's key variable holds the same value as a key a chat, decisions or search route uses
        under another name, or as any running chat client's key value (e.g. after rotating .env without a restart)."""
        try:
            env = _read_dotenv(self.home / ".env")
        except UnicodeDecodeError:  # a malformed .env must not break settings; other I/O errors still surface
            logging.getLogger("dom.config").warning(".env is not valid UTF-8; comparing keys from the environment only")
            env = {}
        value = env.get(name) or os.environ.get(name)
        if not value:
            return False
        if any(other_value and other_value == value for other_value in extra_chat_values):
            return True
        return any(
            (env.get(other) or os.environ.get(other)) == value for other in self.chat_keys() if other != name)

    def media_secret(self, name: str, *, extra_chat_values: Iterable[str] = ()) -> str | None:
        """A studio backup's key value, or None when it is unset or also a chat route's key (E66): a media key may
        take a looser data policy, so it never carries a credential that chat content uses."""
        return None if self.media_key_shared(name, extra_chat_values=extra_chat_values) else self.secret(name)

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
            if name in MODEL_ROUTE_ROLES:
                for index, provider in enumerate(endpoint.fallbacks):
                    yield f"{name}.fallbacks[{index}]", provider

    @property
    def leak_cutoff(self) -> float | None:
        """The Jev score that holds a message back, or None while the Jev leak check is off."""
        if not self.leak_check or self.leak_scan == "off":
            return None
        return LEAK_SCAN_CUTOFFS[self.leak_scan] if self.leak_scan else self.leak_threshold

    @property
    def leak_level(self) -> str:
        """strict, standard, relaxed or off; `custom` for a legacy threshold that matches no level."""
        if not self.leak_check or self.leak_scan == "off":
            return "off"
        if self.leak_scan:
            return self.leak_scan
        level, cutoff = min(LEAK_SCAN_CUTOFFS.items(), key=lambda item: abs(item[1] - self.leak_threshold))
        return level if abs(cutoff - self.leak_threshold) <= LEAK_LEVEL_TOLERANCE else "custom"

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


_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_ENV_VALUE = re.compile(r"[\x21-\x7e]{1,512}")  # printable ASCII, no spaces: a pasted key or token
_DOTENV_LOCK = threading.Lock()


def set_dotenv_many(home: Path, updates: dict[str, str | None]) -> None:
    """Atomically set or drop several secrets in ``home/.env`` in one file replacement (mode 0600 when new).

    Serialised across threads via a process-wide lock and across processes via an exclusive flock on ``home/.env.lock``.
    """
    if not updates:
        return
    for name, value in updates.items():
        if not _ENV_NAME.fullmatch(name):
            raise ValueError("not a variable name")
        if value is not None and not _ENV_VALUE.fullmatch(value):
            raise ValueError("a secret is one run of printable characters without spaces")
    path = home / ".env"
    lock_path = home / ".env.lock"
    with _DOTENV_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                lines = path.read_text().splitlines() if path.is_file() else []
                placed = {name: (value is None) for name, value in updates.items()}
                kept = []
                for line in lines:
                    key = line.strip().partition("=")[0].strip()
                    if line.lstrip().startswith("#") or key not in updates:
                        kept.append(line)
                    elif not placed[key]:
                        kept.append(f"{key}={updates[key]}")
                        placed[key] = True
                for name, value in updates.items():
                    if not placed[name] and value is not None:
                        kept.append(f"{name}={value}")
                        placed[name] = True
                mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o600
                if not path.is_file():
                    path.touch(mode=0o600)
                _replace_file(path, "\n".join(kept) + "\n" if kept else "", mode=mode)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def set_dotenv(home: Path, name: str, value: str | None) -> None:
    """Set one secret in ``home/.env``, or drop it with None, leaving every other line as it was. The file is replaced
    atomically and keeps its mode (0600 when new). The value is only ever written, never returned."""
    set_dotenv_many(home, {name: value})


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
