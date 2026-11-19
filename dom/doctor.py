"""Read-only checks for a Dom instance. Remote probes only run on request."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from dom.backup import ARCHIVE_NAME
from dom.config import Config, ModelEndpoint, load_config
from dom.desktop.sandbox import desktop_profile_path, valid_remote_url
from dom.home import LIMITS, MAX_SOUL, Home
from dom.jev import JevClient, JevUnavailable
from dom.plugins.runner import PluginRunner
from dom.store.store import TARGET_VERSION
from dom.web.auth import Auth

Status = Literal["ok", "warn", "fail"]
BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


def bwrap_available(runner: PluginRunner) -> bool:
    return sys.platform == "linux" and runner.bwrap.is_file() and os.access(runner.bwrap, os.X_OK)


def _soul(home: Home) -> Check:
    try:
        length = len(home.read_memory_file("SOUL.md"))
    except (ValueError, OSError):
        return Check("fail", "SOUL.md", "Make SOUL.md a readable regular UTF-8 file.")
    if not 0 < length <= MAX_SOUL:
        return Check("fail", "SOUL.md", f"Provide a nonempty SOUL.md of at most {MAX_SOUL} characters.")
    return Check("ok", "SOUL.md", f"{length}/{MAX_SOUL} characters.")


def _memory(home: Home) -> Check:
    too_long = unreadable = 0
    for pattern, limit in LIMITS.items():
        for path in home.root.glob(pattern):
            try:
                if not path.is_file() or len(home.read_memory_file(path.relative_to(home.root).as_posix())) > limit:
                    too_long += 1
            except (ValueError, OSError):
                unreadable += 1
    if unreadable:
        return Check(
            "fail", "memory",
            f"{unreadable} unreadable notes and {too_long} non-file or oversized entries. "
            "Check memory/ and private/ for file types, UTF-8 and character limits.",
        )
    if too_long:  # imported notes often start over their limit; Dom's notes pass trims them
        return Check(
            "warn", "memory",
            f"{too_long} non-file or oversized entries. Dom trims oversized notes over time; "
            "edit them in the dashboard to fix it now.",
        )
    return Check("ok", "memory", "Notes are within their character limits.")


def _database(home: Path) -> Check:
    path = home / "state.db"
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return Check("fail", "state.db", "Use a regular SQLite database in the instance home.")
        if not path.exists():
            return Check("warn", "state.db", "Missing. Start Dom to initialize it on first run.")
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
    except (OSError, sqlite3.Error):
        return Check("fail", "state.db", "Could not read the SQLite database. Check permissions or restore a backup.")
    if version != TARGET_VERSION:
        hint = "Back up the home and start this release to apply migrations." if version < TARGET_VERSION else (
            "Use a Dom release that supports this database version."
        )
        return Check("fail", "state.db", f"Schema version {version}, expected {TARGET_VERSION}. {hint}")
    return Check("ok", "state.db", f"Schema version {version}.")


def _backups(home: Path) -> Check:
    cutoff = time.time() - 36 * 3600
    try:
        recent = any(
            archive.stat().st_mtime > cutoff
            for archive in (home.parent / "backups").iterdir()
            if ARCHIVE_NAME.fullmatch(archive.name) and not archive.is_symlink() and archive.is_file()
        )
    except OSError:
        recent = False
    if recent:
        return Check("ok", "backups", "An archive is newer than 36 hours.")
    return Check("warn", "backups", "No recent archive in the sibling backups folder. Run dom backup and check the timer.")


def _disk(home: Path) -> Check:
    try:
        free = shutil.disk_usage(home).free
    except OSError:
        return Check("fail", "disk", "Cannot measure free space. Check that the home exists and is accessible.")
    if free < 500_000_000:
        return Check("fail", "disk", "Less than 500 MB free. Free space on the instance filesystem.")
    if free < 2_000_000_000:
        return Check("warn", "disk", "Less than 2 GB free. Free space before taking more backups.")
    return Check("ok", "disk", f"{free / 1_000_000_000:.2f} GB free.")


def _desktop(config: Config) -> Check:
    settings = config.desktop
    if config.desktop_mode == "off":
        return Check("ok", "desktop", "Disabled.")
    if config.desktop_mode == "remote":
        if not valid_remote_url(settings.url):
            return Check("fail", "desktop", "Set [desktop] url to the remote HTTP(S) URL.")
        if not settings.remote_isolation_confirmed:
            return Check("fail", "desktop", "Verify LAN isolation, then set [desktop] remote_isolation_confirmed = true.")
        return Check("ok", "desktop", "Remote URL and isolation confirmation are set.")

    problems = []
    if not shutil.which("docker"):
        problems.append("Install Docker or turn off local desktop mode.")
    if not settings.lan_probes:
        problems.append("Set [desktop] lan_probes to LAN IPv4 addresses and ports.")
    try:
        ipaddress.IPv4Network(settings.subnet)
    except ValueError:
        problems.append("Set [desktop] subnet to a valid IPv4 network in CIDR notation.")
    try:
        desktop_profile_path(config)
    except (OSError, RuntimeError):
        problems.append("Set [desktop] profile_dir to a directory outside and separate from DOM_HOME.")
    if not all(value.strip() for value in (settings.network, settings.image, settings.probe_image)):
        problems.append("Set [desktop] network, image and probe_image to nonempty names.")
    if problems:
        return Check("fail", "desktop", " ".join(problems))
    return Check("ok", "desktop", "Local settings and Docker executable are present.")


def _config_checks(config: Config) -> list[Check]:
    names = {BOT_TOKEN_ENV, *(endpoint.api_key_env for endpoint in config.models.values() if endpoint.api_key_env)}
    try:
        missing = sorted(name for name in names if not (config.secret(name) or "").strip())
    except (OSError, ValueError):
        secrets = Check("fail", "secrets", "Make .env readable as UTF-8.")
    else:
        secrets = (
            Check("fail", "secrets", "Set in .env or the environment: " + ", ".join(
                name if ENV_NAME.fullmatch(name) else "<invalid environment variable name>" for name in missing
            ) + ".")
            if missing else Check("ok", "secrets", "Required secrets are set.")
        )
    owner = (
        Check("ok", "owner", "Claimed.") if config.owner_id
        else Check("warn", "owner", "Start Dom, then send /claim <code> in the bot DM. Use the code in .claim.")
    )
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = Check("fail", "timezone", "Set timezone in config.toml to an IANA timezone such as UTC.")
    else:
        zone = Check("ok", "timezone", "Valid IANA timezone.")
    if config.web.enabled:
        try:
            Auth(config)
        except ValueError as exc:
            # Auth's own validation messages are fixed. Decoding errors can include input data.
            detail = str(exc) if str(exc).startswith("Dashboard auth") else "Check [web] and make .env readable."
            web = Check("fail", "web", detail)
        except OSError:
            web = Check("fail", "web", "Check [web] and make .env readable.")
        else:
            web = Check("ok", "web", "Dashboard authentication settings are valid.")
    else:
        web = Check("ok", "web", "Disabled.")
    return [secrets, owner, zone, web, _desktop(config)]


async def _telegram(http: httpx.AsyncClient, token: str, owner: int | None) -> list[Check]:
    async def call(method: str, data: dict):
        # HTTPX logs request URLs at INFO. Telegram embeds the token in its URL.
        log = logging.getLogger("httpx")
        def hide_token(record: logging.LogRecord) -> bool:
            return "https://api.telegram.org/bot" not in record.getMessage()

        log.addFilter(hide_token)
        try:
            response = await http.post(f"https://api.telegram.org/bot{token}/{method}", json=data)
        finally:
            log.removeFilter(hide_token)
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True:
            raise ValueError
        return payload["result"]

    checks = []
    try:
        me = await call("getMe", {})
        if me.get("is_bot") is not True:
            raise ValueError
    except (httpx.HTTPError, httpx.InvalidURL, KeyError, AttributeError, ValueError):
        return [
            Check("fail", "Telegram getMe", "Check TELEGRAM_BOT_TOKEN and connectivity to Telegram."),
            Check("warn", "Telegram getMyCommands", "Retry after getMe succeeds."),
            Check("warn", "Telegram privacy", "Retry after getMe succeeds."),
        ]
    checks.append(Check("ok", "Telegram getMe", "Bot token accepted."))
    checks.append(
        Check("ok", "Telegram privacy", "Privacy mode is disabled.")
        if me.get("can_read_all_group_messages") is True else
        Check("warn", "Telegram privacy", "For ordinary group messages, disable privacy mode in BotFather or make Dom a group admin.")
    )
    data = {"scope": {"type": "chat", "chat_id": owner}} if owner else {}
    try:
        commands = await call("getMyCommands", data)
        if not isinstance(commands, list):
            raise ValueError
    except (httpx.HTTPError, httpx.InvalidURL, KeyError, AttributeError, ValueError):
        checks.append(Check("fail", "Telegram getMyCommands", "Check the bot token, owner claim and Telegram connectivity."))
    else:
        checks.append(
            Check("warn", "Telegram getMyCommands", "Owner command menu is empty. Restart Dom to publish it.")
            if owner and not commands else Check("ok", "Telegram getMyCommands", "Command menu request succeeded.")
        )
    return checks


def _probe_hint(response: httpx.Response) -> str:
    """The provider's own reason, which names problems like data-policy blocks. Never echoes secrets:
    it is the response body of a request that carried only a fixed synthetic prompt."""
    try:
        message = str(response.json().get("error", {}).get("message", ""))
    except ValueError:
        message = ""
    if "ZDR" in message or "data policy" in message:
        return (f"HTTP {response.status_code}: the provider refused it under your account's data policy "
                "(zero data retention). Pick a model with a ZDR-compliant provider.")
    detail = " ".join(message.split())[:160]
    return f"HTTP {response.status_code}: {detail or 'no reason given'}. Check the endpoint URL, model ID and key."


async def _model(http: httpx.AsyncClient, name: str, endpoint: ModelEndpoint, key: str | None) -> Check:
    label = "models." + (name if ENV_NAME.fullmatch(name) else "<custom>")
    if endpoint.api_key_env and not key:
        return Check("warn", label, "Set the model endpoint's secret to run its online probe.")
    try:
        if name == "triage":
            if not key:
                return Check("warn", label, "Set api_key_env and its secret to enable the Jev client.")
            answers = await JevClient(http, key, endpoint.model, endpoint.base_url).decide(
                "Health check.", {"check": {
                    "type": "choice", "instructions": "Answer yes.",
                    "criteria": {"yes": "The check reached Jev.", "no": "The check did not reach Jev."},
                }},
            )
            if (answers["check"] or {}).get("choice") not in ("yes", "no"):
                raise ValueError
        else:
            response = await http.post(
                endpoint.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"} if key else {},
                json={"model": endpoint.model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 1},
            )
            response.raise_for_status()
            if not isinstance(response.json()["choices"][0]["message"], dict):
                raise ValueError
    except httpx.HTTPStatusError as error:
        return Check("fail", label, _probe_hint(error.response))
    except (httpx.HTTPError, httpx.InvalidURL, JevUnavailable, KeyError, IndexError, TypeError, ValueError):
        return Check("fail", label, "Probe failed. Check the endpoint URL, model ID, key and connectivity.")
    return Check("ok", label, "Endpoint answered a small synthetic request.")


async def _spend(http: httpx.AsyncClient, name: str, key: str) -> Check:
    label = f"OpenRouter spend ({name})"
    try:
        response = await http.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        remaining = response.json()["data"]["limit_remaining"]
        if remaining is None:
            return Check("ok", label, "OpenRouter reports no key spend limit.")
        if type(remaining) not in (float, int) or not math.isfinite(remaining):
            raise ValueError
    except (httpx.HTTPError, KeyError, TypeError, ValueError, OverflowError):
        return Check("warn", label, "Could not read remaining spend. Check the key in OpenRouter.")
    if remaining <= 0:
        return Check("warn", label, "No spend remaining under the key limit. Check the key in OpenRouter.")
    return Check("ok", label, f"${remaining:.2f} remaining under the key limit.")


async def _online(config: Config) -> list[Check]:
    checks = []
    async with httpx.AsyncClient(timeout=8) as http:
        token = config.secret(BOT_TOKEN_ENV)
        if token:
            checks.extend(await _telegram(http, token, config.owner_id))
        else:
            checks.append(Check("warn", "Telegram", "Set TELEGRAM_BOT_TOKEN to run the online probes."))
        openrouter_keys = {}
        for name, endpoint in config.models.items():
            variable = endpoint.api_key_env
            key = config.secret(variable) if variable else None
            checks.append(await _model(http, name, endpoint, key))
            try:
                url = httpx.URL(endpoint.base_url)
            except httpx.InvalidURL:
                continue
            if key and url.scheme == "https" and url.host == "openrouter.ai":
                label = variable if ENV_NAME.fullmatch(variable) else "key"
                openrouter_keys.setdefault(key, label)
        for key, name in openrouter_keys.items():
            checks.append(await _spend(http, name, key))
    return checks


def run(home: Path, *, online: bool = False) -> int:
    home = home.resolve()
    try:
        config = load_config(home)
    except (OSError, ValueError, TypeError, AttributeError):
        config = None
        config_check = Check("fail", "config", "Check that config.toml exists and has valid TOML and field values.")
    else:
        config_check = (
            Check("ok", "config", "config.toml parsed.") if "chat" in config.models
            else Check("fail", "config", "Add the required [models.chat] section to config.toml.")
        )
    checks = [config_check]
    if config is not None:
        checks.extend(_config_checks(config))
    notes = Home(home, allow_private=True)
    checks.extend((_soul(notes), _memory(notes), _database(home), _backups(home), _disk(home)))
    runner = PluginRunner(home=home)
    if bwrap_available(runner):
        checks.append(Check("ok", "bwrap", "Bubblewrap is available."))
        checks.append(
            Check("ok", "plugin sandbox", "Isolation self-test passed.") if runner.self_test()
            else Check("warn", "plugin sandbox", "Isolation self-test failed. Check bubblewrap and Linux namespace support.")
        )
    else:
        checks.append(Check("warn", "bwrap", "Install bubblewrap on Linux to enable plugins."))
        checks.append(Check("warn", "plugin sandbox", "Install bubblewrap before running the isolation self-test."))
    if online and config is not None:
        try:
            checks.extend(asyncio.run(_online(config)))
        except (OSError, ValueError):
            checks.append(Check("fail", "online checks", "Make .env readable as UTF-8 and retry."))
    for check in checks:
        print(f"{check.status} {check.name}: {check.detail}")
    return int(any(check.status == "fail" for check in checks))
