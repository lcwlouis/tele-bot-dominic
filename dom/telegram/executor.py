"""Perform an Actions result in Telegram with humanlike timing."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReactionTypeEmoji, ReplyParameters
from pydantic import ValidationError

from dom.actions import Actions
from dom.models import StoredMessage
from dom.store import Store

log = logging.getLogger("dom.executor")
MAX_TYPING_SECONDS = 8
_background: set[asyncio.Task] = set()  # keeps scheduled deletions alive until they run


async def _delete_later(bot, store: Store, chat_id: int, message_id: int, delay: float, sleep) -> None:
    await sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        log.exception("could not delete message %s in chat %s", message_id, chat_id)
        return
    await store.record_message_delete(chat_id, [message_id])


async def cancel_pending_deletions() -> None:
    """Stop scheduled send-then-delete tasks (shutdown): they need the bot and store, which are closing."""
    tasks = list(_background)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def perform(
    bot,
    store: Store,
    chat_id: int,
    trigger_msg_id: int,
    actions: Actions,
    *,
    typing_cps: float = 8.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    thread_id: int | None = None,
) -> list[int]:
    """Send the bubbles and reaction; returns the sent message ids. no_response does nothing at all."""
    if actions.no_response:
        return []
    if actions.react:
        try:
            await bot.set_message_reaction(chat_id, trigger_msg_id, reaction=[ReactionTypeEmoji(emoji=actions.react)])
        except (TelegramAPIError, ValidationError):  # the model may pick an emoji Telegram does not allow
            log.warning("reaction %r rejected in chat %s", actions.react, chat_id)

    sent_ids: list[int] = []
    for bubble in actions.bubbles:
        await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        await sleep(min(len(bubble.text) / typing_cps, MAX_TYPING_SECONDS))
        reply = (
            ReplyParameters(message_id=bubble.reply_to, allow_sending_without_reply=True)
            if bubble.reply_to is not None
            else None
        )
        sent = await bot.send_message(chat_id, bubble.text, reply_parameters=reply, message_thread_id=thread_id)
        await store.record_new_message(
            StoredMessage(
                chat_id=str(chat_id),
                msg_id=sent.message_id,
                reply_to_msg_id=bubble.reply_to,
                is_bot=True,
                role="assistant",
                visibility_scope=store.visibility_for(chat_id),
                text=bubble.text,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        sent_ids.append(sent.message_id)
        if bubble.delete_after is not None:
            task = asyncio.create_task(_delete_later(bot, store, chat_id, sent.message_id, bubble.delete_after, sleep))
            _background.add(task)
            task.add_done_callback(_background.discard)
    return sent_ids
