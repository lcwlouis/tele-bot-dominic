"""Perform an Actions result in Telegram with humanlike timing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from random import choice
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, EphemeralMessageParameters, LinkPreviewOptions, ReactionTypeEmoji, ReplyParameters,
)
from pydantic import ValidationError

from dom.actions import Actions, SendFile
from dom.gifs import GifSearch
from dom.models import StoredMessage
from dom.store import Store

log = logging.getLogger("dom.executor")
MAX_TYPING_SECONDS = 20  # typing time keeps scaling with length up to this; at 8 characters a second that is 160
TYPING_REFRESH_SECONDS = 4  # Telegram's typing indicator lapses after about 5 seconds
_background: set[asyncio.Task] = set()  # keeps scheduled deletions alive until they run


async def _type_for(bot, chat_id: int, seconds: float, thread_id: int | None, sleep) -> None:
    """Show "typing" for `seconds`, sending the indicator again before Telegram lets it lapse."""
    while True:
        await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        if seconds <= TYPING_REFRESH_SECONDS:
            await sleep(seconds)
            return
        await sleep(TYPING_REFRESH_SECONDS)
        seconds -= TYPING_REFRESH_SECONDS


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
    gifs: GifSearch | None = None,
    ephemeral_recipient: int | None = None,
    link_previews: bool = True,
    record: bool = True,
) -> list[int]:
    """Send the bubbles, reaction, sticker and GIF; returns the sent message ids. no_response does nothing at all."""
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
        await react(bot, chat_id, trigger_msg_id, actions.react)

    sent_ids: list[int] = []
    for bubble in actions.bubbles:
        await _type_for(bot, chat_id, min(len(bubble.text) / typing_cps, MAX_TYPING_SECONDS), thread_id, sleep)
        reply = (
            ReplyParameters(message_id=bubble.reply_to, allow_sending_without_reply=True)
            if bubble.reply_to is not None
            else None
        )
        options = {} if link_previews else {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        sent = await bot.send_message(
            chat_id, bubble.text, reply_parameters=reply, message_thread_id=thread_id, **options,
        )
        if record:
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
        elif (sticker_id := await _send_media(
            bot.send_sticker(chat_id, choice(choices), message_thread_id=thread_id),
            store, chat_id, "sticker", {"kind": "sticker", "emoji": actions.sticker}, now, on_sent,
        )) is not None:
            sent_ids.append(sticker_id)
    if actions.gif and gifs is not None:
        try:
            url = await gifs.find(actions.gif)
        except Exception:
            log.exception("gif search crashed in chat %s", chat_id)
            url = None
        if url and (gif_id := await _send_media(
            bot.send_animation(chat_id, url, message_thread_id=thread_id),
            store, chat_id, "gif", {"kind": "gif", "query": actions.gif}, now, on_sent,
        )) is not None:
            sent_ids.append(gif_id)
    return sent_ids


async def _send_media(
    sending: Awaitable, store: Store, chat_id: int, kind: str, media: dict,
    now: Callable[[], datetime] | None, on_sent: Callable[[int], Awaitable[None]] | None,
) -> int | None:
    """Await one media send and record it; a Telegram refusal is logged and means nothing was sent."""
    try:
        sent = await sending
    except TelegramAPIError:
        log.warning("%s send failed in chat %s", kind, chat_id)
        return None
    await store.record_new_message(StoredMessage(
        chat_id=str(chat_id), msg_id=sent.message_id, is_bot=True, role="assistant",
        visibility_scope=store.visibility_for(chat_id), text=f"[{kind}]", media_json=json.dumps(media),
        created_at=(now() if now is not None else datetime.now(timezone.utc)).isoformat(),
    ))
    if on_sent is not None:
        await on_sent(sent.message_id)
    return sent.message_id


async def react(bot, chat_id: int, msg_id: int, emoji: str) -> bool | None:
    """React to a message: True when sent, False when Telegram refuses it (e.g. the chat has reactions
    turned off), None when it failed for another reason, such as the network."""
    try:
        await bot.set_message_reaction(chat_id, msg_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
    except (TelegramBadRequest, ValidationError):
        log.warning("reaction %r rejected in chat %s", emoji, chat_id)
        return False
    except TelegramAPIError as error:
        log.warning("reaction %r failed in chat %s (%s)", emoji, chat_id, type(error).__name__)
        return None
    return True


async def send_files(bot, store: Store, chat_id: int, files: Sequence[SendFile]) -> None:
    if chat_id != store.owner_id:
        return
    for file in files:
        try:
            await bot.send_document(chat_id, BufferedInputFile(file.data, filename=file.filename))
        except TelegramAPIError:
            log.warning("could not send a desktop result document")
            await bot.send_message(chat_id, f"Couldn't send {file.filename}.")
