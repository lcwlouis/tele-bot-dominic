"""The dashboard's Talk page: the owner writes to Dom from the dashboard, in the same conversation as the owner DM.

A message typed there goes to the owner DM as a silent mirror bubble from the bot ("🖥 <text>"), so it has a real
Telegram message id (the window, read marks and restarts all order by it) and the Telegram side reads as one
conversation. It is then recorded as the owner's own message, with `origin = 'dashboard'`, through the same path as
Telegram ingress, and from there the turn is an ordinary owner-DM turn. Bots never receive their own messages as
updates, so the mirror is never ingested twice. Dom's replies already go to Telegram and into `messages`, which the page
polls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram.types import BufferedInputFile

from dom.models import IncomingMessage
from dom.telegram.convert import _media
from dom.telegram.executor import retry_telegram

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.talk")

ORIGIN = "dashboard"
MIRROR = "🖥"  # what the Telegram side shows before a message typed on the dashboard
MAX_TEXT = 4000  # under Telegram's 4096 for a message and 1024 for a caption, once the glyph is added
MAX_CAPTION = 1000
MAX_PHOTO_BYTES = 8_000_000
PHOTO_TYPES = {"image/jpeg": "photo.jpg", "image/png": "photo.png", "image/webp": "photo.webp"}


def _sent_at(app: App, sent) -> datetime:
    date = getattr(sent, "date", None)
    if not isinstance(date, datetime):
        return app.dispatch.now()
    return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


class TalkRefused(ValueError):
    """The message can't be sent as asked; the text says why, for the owner."""


def check(text: str, photo: bytes | None, mime: str | None) -> str:
    text = text.strip()
    if not text and photo is None:
        raise TalkRefused("Write something first.")
    if text.startswith("/"):
        raise TalkRefused("Commands work in Telegram.")
    if photo is not None:
        if mime not in PHOTO_TYPES:
            raise TalkRefused("Send a JPEG, PNG or WebP photo.")
        if len(photo) > MAX_PHOTO_BYTES:
            raise TalkRefused("That photo is over 8 MB.")
        if len(text) > MAX_CAPTION:
            raise TalkRefused(f"A photo's caption can be {MAX_CAPTION} characters at most.")
    elif len(text) > MAX_TEXT:
        raise TalkRefused(f"A message can be {MAX_TEXT} characters at most.")
    return text


async def send_from_dashboard(app: App, text: str, *, photo: bytes | None = None, mime: str | None = None) -> int:
    """Mirror the message into the owner DM, then record it as the owner's and wake the dispatcher. Returns its
    Telegram message id. Holds the owner DM's ingest lock throughout, as Telegram ingress does, so a message typed in
    Telegram at the same moment is recorded strictly before or after this one."""
    from dom.telegram.handlers import record_incoming

    owner_id = app.owner_id
    if owner_id is None:
        raise TalkRefused("Claim Dom in Telegram first.")
    text = check(text, photo, mime)
    mirror = f"{MIRROR} {text}" if text else MIRROR
    async with app.ingest_locks[owner_id]:
        if photo is not None:
            upload = BufferedInputFile(photo, PHOTO_TYPES[mime or "image/jpeg"])
            sent = await retry_telegram(
                lambda: app.bot.send_photo(owner_id, upload, caption=mirror, disable_notification=True), sleep=app.sleep,
            )
        else:
            sent = await retry_telegram(
                lambda: app.bot.send_message(owner_id, mirror, disable_notification=True), sleep=app.sleep,
            )
        person = await app.store.resolve_person(owner_id, None, "")
        msg = IncomingMessage(
            chat_id=owner_id, chat_type="private", chat_title="", msg_id=sent.message_id, sender_id=owner_id,
            sender_handle=person.current_handle or None, sender_name=person.display_name or "",
            is_bot=False, text=text, reply_to=None, media=_media(sent) if photo is not None else None,
            date=_sent_at(app, sent),
        )
        await record_incoming(app, msg, origin=ORIGIN)
    log.info("owner wrote from the dashboard (message %s)", sent.message_id)
    return sent.message_id
