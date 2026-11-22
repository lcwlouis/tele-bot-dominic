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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ModelRole = Literal["chat", "fallback", "media", "drafting"]
MODEL_ROLES = get_args(ModelRole)


class ProviderEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str = Field(description="Provider API base URL, including its API version path.")
    model: str = Field(description="Provider model ID.")
    api_key_env: str | None = Field(None, description="Secret variable name. Leave unset for a compatible endpoint without authentication.")
    reasoning_effort: Literal["low", "medium"] | None = Field(
        None, description="Thinking effort sent to the provider. Use low or medium, or omit it to use the provider default.",
    )


class ModelEndpoint(ProviderEndpoint):
    fallbacks: tuple[ProviderEndpoint, ...] = Field((), description="Additional providers to try in order when this role's primary fails.")


class OpenersConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(False, description="Allow Dom to start conversations in the owner's DM.")
    max_per_day: int = Field(2, ge=0, description="Maximum number of openers per day in the configured timezone.")


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


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    home: Path
    owner_id: int | None = Field(None, description="Telegram user ID recorded after a successful /claim.")
    timezone: str = Field("UTC", description="IANA timezone for presence and daily jobs. Init writes the machine timezone or the supplied --timezone.")
    models: dict[str, ModelEndpoint] = {}
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
    if "search" in models:
        models["search"] = {**defaults["search"], **models["search"]}
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


def _replace_file(path: Path, text: str, *, mode: int | None = None) -> None:
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), mode)
            file.write(text.encode("utf-8"))
            file.flush()
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
