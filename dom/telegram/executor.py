"""Perform an Actions result in Telegram with humanlike timing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from random import choice
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, EphemeralMessageParameters, LinkPreviewOptions, ReactionTypeEmoji, ReplyParameters,
)
from pydantic import ValidationError

from dom.actions import Actions, SendFile
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


async def _tell_owner_held_back(store: Store, chat_id: int) -> None:
    """Leave a note in the owner's DM so they are not left wondering why a private reply never arrived."""
    try:
        title = (await store.get_chat_auth_status(chat_id))["title"] or str(chat_id)
        await store.record_owner_event(f"Held back a private reply in {title}: it wasn't eligible")
    except Exception:
        log.exception("could not record the held-back private reply")


async def perform(
    bot,
    store: Store,
    chat_id: int,
    trigger_msg_id: int | None,
    actions: Actions,
    *,
    typing_cps: float = 8.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    thread_id: int | None = None,
    now: Callable[[], datetime] | None = None,
    on_sent: Callable[[int], Awaitable[None]] | None = None,
    stickers: Mapping[str, Sequence[str]] | None = None,
    ephemeral_recipient: int | None = None,
    link_previews: bool = True,
) -> list[int]:
    """Send the bubbles, reaction and sticker; returns the sent message ids. no_response does nothing at all."""
    if actions.no_response:
        return []
    if actions.private and chat_id < 0:
        if ephemeral_recipient is None or ephemeral_recipient != store.owner_id:
            log.warning("ignored a private action without an eligible receiver in chat %s", chat_id)
            await _tell_owner_held_back(store, chat_id)
            return []
        parameters = EphemeralMessageParameters(receiver_user_id=ephemeral_recipient)
        # Keep these bubbles out of the group's history and skip public side effects.
        sent_ids = []
        for bubble in actions.bubbles:
            try:
                sent = await bot.send_message(
                    chat_id, bubble.text, message_thread_id=thread_id, ephemeral_message_parameters=parameters,
                )
            except TelegramAPIError:
                log.warning("ephemeral send failed in chat %s", chat_id)
                break
            sent_ids.append(sent.message_id)
        return sent_ids
    if actions.react and trigger_msg_id is not None:
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
        options = {} if link_previews else {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        sent = await bot.send_message(
            chat_id, bubble.text, reply_parameters=reply, message_thread_id=thread_id, **options,
        )
        await store.record_new_message(
            StoredMessage(
                chat_id=str(chat_id),
                msg_id=sent.message_id,
                reply_to_msg_id=bubble.reply_to,
                is_bot=True,
                role="assistant",
                visibility_scope=store.visibility_for(chat_id),
                text=bubble.text,
                created_at=(now() if now is not None else datetime.now(timezone.utc)).isoformat(),
            )
        )
        sent_ids.append(sent.message_id)
        if on_sent is not None:
            await on_sent(sent.message_id)
        if bubble.delete_after is not None:
            task = asyncio.create_task(_delete_later(bot, store, chat_id, sent.message_id, bubble.delete_after, sleep))
            _background.add(task)
            task.add_done_callback(_background.discard)
    if actions.sticker:
        choices = (stickers or {}).get(actions.sticker)
        if not choices:
            log.warning("unknown sticker emoji %r in chat %s", actions.sticker, chat_id)
            return sent_ids
        try:
            sent = await bot.send_sticker(chat_id, choice(choices), message_thread_id=thread_id)
        except TelegramAPIError:
            log.warning("sticker send failed in chat %s", chat_id)
            return sent_ids
        await store.record_new_message(StoredMessage(
            chat_id=str(chat_id), msg_id=sent.message_id, is_bot=True, role="assistant",
            visibility_scope=store.visibility_for(chat_id), text="[sticker]",
            media_json=json.dumps({"kind": "sticker", "emoji": actions.sticker}),
            created_at=(now() if now is not None else datetime.now(timezone.utc)).isoformat(),
        ))
        sent_ids.append(sent.message_id)
        if on_sent is not None:
            await on_sent(sent.message_id)
    return sent_ids


async def send_files(bot, store: Store, chat_id: int, files: Sequence[SendFile]) -> None:
    if chat_id != store.owner_id:
        return
    for file in files:
        try:
            await bot.send_document(chat_id, BufferedInputFile(file.data, filename=file.filename))
        except TelegramAPIError:
            log.warning("could not send a desktop result document")
            await bot.send_message(chat_id, f"Couldn't send {file.filename}.")
