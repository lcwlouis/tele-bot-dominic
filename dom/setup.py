"""`dom setup`: where the media studio runs (owner decision E82), and the notes that belong at setup (E80).

A studio can run on the same machine as Dom, on a rented cloud GPU, or not at all, with external APIs doing the
media. Each path writes its `[studio]` settings to config.toml and the token to .env; the dashboard's Services
section edits the same values later.
"""

from __future__ import annotations

import getpass
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args

import tomlkit

from dom.config import StudioConfig, _replace_file, set_dotenv, validate_config

# Shown once, as text, when `dom setup` runs. The dashboard afterwards marks it with an info glyph, not a warning.
ZDR_RECOMMENDATION = (
    "Zero data retention is recommended for every cloud model that sees your chats. Dom asks OpenRouter for it on "
    "chat routes by itself; for any other provider, check its own data terms."
)
STUDIO_NOTE_TAILSCALE = (
    "A studio on a rented cloud GPU or another network is best reached over Tailscale: join the GPU and Dom's host to "
    "one tailnet and use the GPU's MagicDNS name or Tailscale address as the address. Never open the studio's port to "
    "the internet. The bearer token is still required."
)

StudioPath = Literal["local", "cloud", "apis"]
STUDIO_PATHS = get_args(StudioPath)
LOCAL_URL = "http://127.0.0.1:8080"
MEDIA_KEY = "OPENROUTER_MEDIA_API_KEY"
# What the "external APIs only" path sets as cloud backups, from docs/studio.md. Video is left out: no OpenRouter
# video endpoint offers zero data retention, so a video backup is an explicit choice in Settings.
API_BACKUPS = {
    "voice_fallbacks": {"provider": "openrouter_tts", "model": "google/gemini-3.8-flash-lite-tts", "api_key_env": MEDIA_KEY},
    "image_fallbacks": {"provider": "openrouter_image", "model": "google/gemini-2.5-flash-image", "api_key_env": MEDIA_KEY},
}


def apply_studio_path(
    home: Path, path: StudioPath, *, base_url: str = "", token: str | None = None,
) -> list[str]:
    """Write the `[studio]` settings for a path and the token to .env; returns what is still left to do.

    local: the studio at `base_url` (default on this machine). cloud: a remote `base_url` and its token, both
    required. apis: the studio off, the voice and photo switches on, and cloud backups added for any chain without one.
    A config that would not validate is left untouched."""
    if path not in STUDIO_PATHS:
        raise ValueError("choose local, cloud or apis")
    if path == "cloud" and (not base_url or not token):
        raise ValueError("a studio on a cloud GPU needs its address and its token")
    config_path = home / "config.toml"
    old = config_path.read_text()
    document = tomlkit.parse(old)
    studio = document.setdefault("studio", tomlkit.table())
    todo: list[str] = []
    if path == "apis":
        studio["enabled"], studio["base_url"] = False, ""
        studio["voice"], studio["images"] = True, True
        for chain, backup in API_BACKUPS.items():
            if chain not in studio:
                backups = tomlkit.aot()
                backups.append(tomlkit.item(backup))
                studio[chain] = backups
        todo.append(f"Put your OpenRouter media key in .env as {MEDIA_KEY} (a separate key from the chat one).")
    else:
        address = StudioConfig(base_url=base_url or LOCAL_URL).base_url  # the config's own origin check
        studio["enabled"], studio["base_url"] = True, address
        if token is None:
            todo.append(f"Put the studio's token in .env as {StudioConfig().token_env}, then restart Dom.")
    new = tomlkit.dumps(document)
    config = validate_config(home, tomllib.loads(new))
    if token is not None:
        set_dotenv(home, config.studio.token_env, token)
    _replace_file(home / "config.toml.bak", old, mode=0o600, durable=False)
    _replace_file(config_path, new)
    return todo


def run_setup(
    home: Path, *, ask: Callable[[str], str] | None = None, ask_secret: Callable[[str], str] | None = None,
    say: Callable[[str], None] = print, path: StudioPath | None = None, base_url: str = "",
) -> int:
    """Ask where the studio runs and write it. `path` and `base_url` skip their questions."""
    ask, ask_secret = ask or input, ask_secret or getpass.getpass
    say(ZDR_RECOMMENDATION + "\n")
    if path is None:
        say(
            "Where does the media studio (voice notes, round videos, photos) run?\n"
            "  1) On this machine\n  2) On a rented cloud GPU\n  3) Nowhere: external APIs only\n"
        )
        path = {"1": "local", "2": "cloud", "3": "apis"}.get(ask("Choose 1, 2 or 3 [3]: ").strip() or "3")
        if path is None:
            say("Choose 1, 2 or 3.")
            return 1
    token = None
    if path == "cloud":
        say(STUDIO_NOTE_TAILSCALE)
    if path != "apis":
        base_url = base_url or ask(f"Studio address [{LOCAL_URL if path == 'local' else 'required'}]: ").strip()
        token = ask_secret("Studio token (hidden; leave empty to add it to .env yourself): ").strip() or None
    try:
        todo = apply_studio_path(home, path, base_url=base_url, token=token)
    except (OSError, ValueError) as error:
        say(f"Setup stopped: {error}")
        return 1
    say(f"Saved the {path} studio path to {home / 'config.toml'} (the previous file is config.toml.bak).")
    for line in todo:
        say(f"Next: {line}")
    return 0
