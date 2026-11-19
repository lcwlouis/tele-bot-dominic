"""Instance configuration: ``DOM_HOME/config.toml`` plus secrets from ``.env``."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str = Field(description="Provider API base URL, including its API version path.")
    model: str = Field(description="Provider model ID.")
    api_key_env: str | None = Field(None, description="Secret variable name. Leave unset for a compatible endpoint without authentication.")


class OpenersConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(True, description="Allow Dom to start conversations in the owner's DM.")
    max_per_day: int = Field(2, ge=0, description="Maximum number of openers per day in the configured timezone.")


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


class DesktopConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    image: str = Field("ghcr.io/agent-infra/sandbox:latest", description="Pre-pulled local sandbox image.")
    url: str = Field("", description="Sandbox API base URL in remote mode.")
    api_key_env: str = Field("SANDBOX_API_KEY", description="Secret variable for the remote sandbox API key. Local mode generates a key.")
    idle_minutes: float = Field(10, gt=0, allow_inf_nan=False, description="Idle minutes before the local desktop stops.")
    profile_dir: str = Field("", description="Local browser profile outside the instance home. Empty uses the sibling desktop-profile directory.")
    network: str = Field("dom-desktop", description="Docker bridge name for local isolation.")
    subnet: str = Field("172.30.0.0/24", description="IPv4 CIDR of the desktop bridge. Choose a subnet separate from your LAN and other bridges.")
    port: int = Field(18080, ge=1, le=65535, description="Loopback host port for the local sandbox API.")
    task_cost_cap_usd: float = Field(0.5, ge=0, allow_inf_nan=False, description="Per-desktop-task USD limit. Zero disables this limit.")
    lan_probes: list[str] = Field([], description="LAN IPv4:port targets local mode must prove it cannot reach. Required in local mode.")
    probe_image: str = Field("alpine:3.22", description="Pre-pulled image used for local isolation probes.")
    probe_control: str = Field("1.1.1.1:443", description="Public IPv4:port target that the local isolation probe must reach.")
    remote_isolation_confirmed: bool = Field(False, description="Set after verifying the remote desktop cannot reach your LAN or Dom host.")
    fallback_usd_per_1k_tokens: float = Field(0.002, ge=0, allow_inf_nan=False, description="USD per 1,000 tokens when a desktop call reports no cost. Used for the task limit only.")

    @field_validator("lan_probes")
    @classmethod
    def _probe_targets(cls, targets: list[str]) -> list[str]:
        for target in targets:
            _ipv4_target(target)
        return targets

    @field_validator("probe_control")
    @classmethod
    def _control_target(cls, target: str) -> str:
        if not _ipv4_target(target).is_global:
            raise ValueError("probe_control must be a public IPv4 address")
        return target


def _ipv4_target(target: str) -> ipaddress.IPv4Address:
    host, _, port = target.rpartition(":")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError("probe targets must look like ipv4:port (an IPv4 literal, not a name)") from None
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("probe targets must look like ipv4:port")
    return address


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    home: Path
    owner_id: int | None = Field(None, description="Telegram user ID recorded after a successful /claim.")
    timezone: str = Field("UTC", description="IANA timezone for presence and daily jobs. Init writes the machine timezone or the supplied --timezone.")
    models: dict[str, ModelEndpoint] = {}
    desktop_mode: Literal["local", "remote", "off"] = Field("off", description="Desktop mode. Use off to disable it, local for Docker, or remote for a separate sandbox host.")
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    window_size: int = Field(80, description="Maximum recent messages in a chat's model window.")
    daily_spend_cap_usd: float = Field(1.0, ge=0, allow_inf_nan=False, description="Daily USD cap for non-owner activity and desktop tasks. Zero disables the cap.")
    openers: OpenersConfig = Field(default_factory=OpenersConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    ephemeral_replies: bool = Field(False, description="Allow owner-only ephemeral replies in groups when supported by Telegram.")
    leak_check: bool = Field(True, description="Use Jev to screen eligible outbound text and network plugin calls for private information.")
    leak_threshold: float = Field(1.2, ge=0, le=3, description="Minimum Jev score on a 0–3 scale that holds back an outbound reply or online plugin call.")

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
    models = {key: defaults[key] for key in ("triage", "media")}
    models.update(raw.get("models", {}))
    if "search" in models:
        models["search"] = {**defaults["search"], **models["search"]}
    return Config.model_validate({
        **raw,
        "home": home,
        "models": models,
        "desktop_mode": raw.get("desktop", {}).get("mode", Config.model_fields["desktop_mode"].default),
    })


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
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(new_text)
    os.replace(temp, path)
    if tomllib.loads(path.read_text()).get("owner_id") != owner_id:
        raise ValueError("owner_id was not saved to config.toml")
