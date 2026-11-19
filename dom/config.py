"""Instance configuration: ``DOM_HOME/config.toml`` plus secrets from ``.env``."""

from __future__ import annotations

import os
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    model: str
    api_key_env: str | None = None


class OpenersConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    max_per_day: int = Field(default=2, ge=0)


class WebConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    access_team: str = ""
    access_aud: str = ""
    dev_token_env: str = ""


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    home: Path
    owner_id: int | None = None
    timezone: str = "Asia/Singapore"
    models: dict[str, ModelEndpoint] = {}
    desktop_mode: Literal["local", "remote", "off"] = "off"
    window_size: int = 80
    daily_spend_cap_usd: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    openers: OpenersConfig = Field(default_factory=OpenersConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    ephemeral_replies: bool = False
    leak_check: bool = True
    leak_threshold: float = Field(default=1.2, ge=0, le=3)

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


def load_config(home: Path) -> Config:
    raw = tomllib.loads((home / "config.toml").read_text())
    starter = resources.files("dom").joinpath("starter_home", "config.toml")
    defaults = tomllib.loads(starter.read_text(encoding="utf-8"))["models"]
    models = {key: defaults[key] for key in ("triage", "media")}
    models.update(raw.get("models", {}))
    if "search" in models:
        models["search"] = {**defaults["search"], **models["search"]}
    return Config(
        home=home,
        owner_id=raw.get("owner_id"),
        timezone=raw.get("timezone", "Asia/Singapore"),
        models=models,
        desktop_mode=raw.get("desktop", {}).get("mode", "off"),
        window_size=raw.get("window_size", 80),
        daily_spend_cap_usd=raw.get("daily_spend_cap_usd", 1.0),
        openers=raw.get("openers", {}),
        web=raw.get("web", {}),
        **{key: raw[key] for key in ("leak_check", "leak_threshold", "ephemeral_replies") if key in raw},
    )


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
