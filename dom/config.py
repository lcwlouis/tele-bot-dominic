"""Instance configuration: ``DOM_HOME/config.toml`` plus secrets from ``.env``."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    model: str
    api_key_env: str | None = None


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    home: Path
    owner_id: int | None = None
    timezone: str = "Asia/Singapore"
    models: dict[str, ModelEndpoint] = {}
    desktop_mode: Literal["local", "remote", "off"] = "off"
    window_size: int = 35

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
    return Config(
        home=home,
        owner_id=raw.get("owner_id"),
        timezone=raw.get("timezone", "Asia/Singapore"),
        models=raw.get("models", {}),
        desktop_mode=raw.get("desktop", {}).get("mode", "off"),
        window_size=raw.get("window_size", 35),
    )


_OWNER_LINE = re.compile(r"^owner_id\s*=.*$", re.MULTILINE)
_FIRST_TABLE = re.compile(r"^\[", re.MULTILINE)


def save_owner(home: Path, owner_id: int) -> None:
    """Set ``owner_id`` in config.toml, leaving every other line as it was."""
    path = home / "config.toml"
    text = path.read_text()
    line = f"owner_id = {owner_id}"
    if _OWNER_LINE.search(text):
        text = _OWNER_LINE.sub(line, text, count=1)
    else:
        # top-level keys must come before the first table header
        match = _FIRST_TABLE.search(text)
        at = match.start() if match else len(text)
        prefix = text[:at]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        text = f"{prefix}{line}\n\n{text[at:]}" if match else f"{prefix}{line}\n"
    path.write_text(text)
