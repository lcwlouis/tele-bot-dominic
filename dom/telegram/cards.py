"""Menu cards that delete themselves.

A card is any owner message with a Close button (`/status`, `/chats`, `/preset`, `/plugins`, `/proposals`,
`/reminders`, ...). When one is sent it is recorded with an expiry (`menu_card_minutes` after sending); a sweep once a
minute, and one at startup, deletes the cards whose time is up, so a restart forgets none. Telegram refuses to delete a
message older than 48 hours, so such a card only loses its buttons.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.telegram.executor import retry_telegram

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
CLOSE_DATA = "card:close"
CLOSE_BUTTON = InlineKeyboardButton(text="✕ Close", callback_data=CLOSE_DATA)
DELETE_LIMIT = timedelta(hours=48)  # Telegram deletes no message older than this
SWEEP_BATCH = 20  # cards handled per sweep, so a long outage never floods Telegram; the rest wait for the next one


def is_card(markup: object) -> bool:
    """True when the keyboard has a Close button."""
    return isinstance(markup, InlineKeyboardMarkup) and any(
        button.callback_data == CLOSE_DATA for row in markup.inline_keyboard for button in row
    )


async def track(app: App, chat_id: int, sent: object) -> None:
    """Record a card just sent so it expires. A failure is logged: the card still works, it just stays."""
    minutes = app.config.menu_card_minutes
    message_id = getattr(sent, "message_id", None)
    if not minutes or not isinstance(message_id, int):
        return
    now = app.dispatch.now().astimezone(timezone.utc)
    try:
        await app.store.add_menu_card(chat_id, message_id, now.isoformat(), (now + timedelta(minutes=minutes)).isoformat())
    except Exception:
        log.exception("could not record a menu card to expire")


async def sweep(app: App) -> int:
    """Delete the cards that have expired; returns how many left the table. A card Telegram would not delete, or
    one past 48 hours, loses its buttons instead; a flood wait or a network failure leaves it for the next sweep."""
    now = app.dispatch.now().astimezone(timezone.utc)
    removed = 0
    for row in await app.store.due_menu_cards(now.isoformat(), SWEEP_BATCH):
        chat_id, message_id = row["chat_id"], row["message_id"]
        too_old = now - datetime.fromisoformat(row["sent_at"]) >= DELETE_LIMIT
        try:
            if not too_old:
                try:
                    await retry_telegram(lambda: app.bot.delete_message(chat_id, message_id), sleep=app.sleep)
                except TelegramBadRequest:  # already gone, or Telegram will not delete it any more
                    too_old = True
            if too_old:
                try:
                    await retry_telegram(
                        lambda: app.bot.edit_message_reply_markup(
                            chat_id=chat_id, message_id=message_id, reply_markup=None,
                        ),
                        sleep=app.sleep,
                    )
                except TelegramBadRequest:  # gone, or no buttons left to clear
                    log.info("a menu card in chat %s was already gone or had no buttons", chat_id)
        except (TelegramRetryAfter, TelegramAPIError, OSError):
            log.warning("could not expire a menu card in chat %s; will retry", chat_id)
            continue
        await app.store.forget_menu_card(chat_id, message_id)
        removed += 1
    return removed
