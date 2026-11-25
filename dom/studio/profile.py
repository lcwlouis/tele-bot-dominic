"""Keep the bot's global profile photo in sync with the Everywhere preset."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic

from dom.studio.policy import SWITCH_KINDS, backups, local, outfits, unserved
from dom.traits import vibe

if TYPE_CHECKING:
    from dom.config import Config
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
PROFILE_AT = "studio:profile_at"
PROFILE_OUTFIT = "studio:profile_outfit"
PROFILE_RETRY = timedelta(hours=1)


async def maintain(app: App) -> None:
    """One background loop owns health refresh and profile updates."""
    while True:
        try:
            if app.studio is not None and app.config.studio.enabled:
                await app.studio.refresh()
                await sync_profile(app)
        except Exception as error:
            log.warning("studio maintenance failed (%s)", type(error).__name__)
        await asyncio.sleep(10)


async def sync_profile(app: App) -> None:
    config = app.config.studio
    client = app.studio
    if client is None or not client.ready or not config.enabled or not config.profile_photo_follows_preset:
        return
    now = app.dispatch.now()
    stamp = await app.store.kv_get(PROFILE_AT)
    # The first sync after the switch is turned on may happen at once (the stamp is cleared then); after that the
    # photo changes at most once per the chosen interval, and only when the outfit has changed.
    if stamp and now - datetime.fromisoformat(stamp) < timedelta(days=config.profile_photo_every_days):
        return
    wardrobe = outfits(config, (await vibe(app.home, app.store, now)).preset)
    if not wardrobe or wardrobe[0] == await app.store.kv_get(PROFILE_OUTFIT):
        return
    outfit = wardrobe[0]
    data = await client.portrait(outfit)
    if not data or not app.config.studio.enabled or not app.config.studio.profile_photo_follows_preset:
        return
    current = outfits(app.config.studio, (await vibe(app.home, app.store, app.dispatch.now())).preset)
    if not current or current[0] != outfit:
        return
    # Persist the attempt first. Telegram may apply a photo even if its response is lost.
    await app.store.kv_set(PROFILE_AT, app.dispatch.now().isoformat())
    try:
        await app.bot.set_my_profile_photo(photo=InputProfilePhotoStatic(
            photo=BufferedInputFile(data, filename="portrait.png"),
        ))
    except TelegramAPIError:
        log.warning("studio profile photo update failed")
        # A refused change is tried again after an hour instead of a whole interval.
        retry = app.dispatch.now() - timedelta(days=config.profile_photo_every_days) + PROFILE_RETRY
        await app.store.kv_set(PROFILE_AT, retry.isoformat())
    else:
        await app.store.kv_set(PROFILE_OUTFIT, outfit)


SWITCH_NAMES = {
    "voice": "Voice notes", "video_notes": "Round video notes", "images": "Photos", "special": "Special video notes",
    "profile_photo_follows_preset": "Profile photo",
}


def _has_key(config: Config) -> Callable[[str], bool]:
    def has_key(name: str) -> bool:
        try:
            return bool(config.media_secret(name))  # a backup whose key is also chat's never serves
        except (OSError, ValueError):
            return False
    return has_key


def availability(config: Config, ready: bool) -> list[str]:
    """One line per studio switch: off, available, unavailable for now, or on with nothing set up to serve it. A
    backup counts once its key has a value. Reads .env, so run it off the event loop."""
    studio, has_key = config.studio, _has_key(config)
    lines = []
    for switch, name in SWITCH_NAMES.items():
        if not getattr(studio, switch):
            state = "off"
        elif (ready and local(studio)) or any(
                has_key(backup.api_key_env) for kind in SWITCH_KINDS[switch] for backup in backups(studio, kind)):
            state = "available"
        elif local(studio):
            state = "unavailable for now"
        else:
            state = "on, but nothing can make them yet"
        lines.append(f"{name}: {state}")
    return lines


async def unserved_note(app: App) -> str:
    labels = await asyncio.to_thread(unserved, app.config.studio, _has_key(app.config))
    if not labels:
        return ""
    return (f"On, but nothing can make them: {', '.join(labels)}. Set up the studio or a cloud backup; "
            "Dom sends text instead meanwhile.")


async def status_text(app: App) -> str:
    """The owner's /studio reply: availability per kind and today's counts. Machinery (workers, memory, outage times,
    providers) stays on the dashboard."""
    studio = app.config.studio
    if not any(getattr(studio, switch) for switch in SWITCH_NAMES) and not local(studio) and not studio.fallbacks:
        return "Media generation is off."
    ready = app.studio is not None and app.studio.ready
    lines = await asyncio.to_thread(availability, app.config, ready)
    count, rows = await app.store.studio_usage(app.owner_id, app.dispatch.now())
    since = app.dispatch.now() - timedelta(days=1)
    owner = sum(stamp > since for _, stamp in rows)
    return "\n".join([*lines, f"Past 24 hours: {owner} in this chat · {count} across chats"])
