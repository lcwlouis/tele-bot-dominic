"""Keep the bot's global profile photo in sync with the Everywhere preset."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic

from dom.studio.policy import outfits, unserved
from dom.traits import vibe

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
PROFILE_AT = "studio:profile_at"
PROFILE_OUTFIT = "studio:profile_outfit"


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
    else:
        await app.store.kv_set(PROFILE_OUTFIT, outfit)


def unserved_note(app: App) -> str:
    labels = unserved(app.config.studio)
    if not labels:
        return ""
    return (f"On, but nothing can make them: {', '.join(labels)}. Set up the studio or a cloud backup; "
            "Dom sends text instead meanwhile.")


async def status_text(app: App) -> str:
    note = unserved_note(app)
    if app.studio is None or not app.config.studio.enabled:
        backups = app.config.studio.fallbacks
        status = "Local media studio is off." + (f" Cloud backups: {len(backups)}." if backups else "")
        return f"{status}\n{note}" if note else ("Media generation is off." if not backups else status)
    status = f"Media generation: {app.studio.status}"
    if note:
        status += f"\n{note}"
    health = app.studio.health or {}
    workers = health.get("workers_loaded", health.get("loaded_workers", health.get("workers")))
    if isinstance(workers, list) and all(isinstance(worker, str) for worker in workers):
        status += f"\nLoaded workers: {', '.join(workers)[:160] or 'none'}"
    for key in ("available_memory", "available_bytes", "available_memory_bytes", "free_bytes"):
        free = health.get(key)
        if type(free) is int and free >= 0:
            status += f"\nAvailable memory: {free / 1024**3:.1f} GiB"
            break
    count, rows = await app.store.studio_usage(app.owner_id, app.dispatch.now())
    since = app.dispatch.now() - timedelta(days=1)
    owner = sum(stamp > since for _, stamp in rows)
    return f"{status}\nPast 24 hours: {owner} in this chat · {count} across chats"
