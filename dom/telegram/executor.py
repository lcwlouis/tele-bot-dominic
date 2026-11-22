"""Perform an Actions result in Telegram with humanlike timing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from random import choice
from time import monotonic
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, EphemeralMessageParameters, LinkPreviewOptions, ReactionTypeEmoji, ReplyParameters,
)
from pydantic import ValidationError

from dom import turn_log
from dom.actions import Actions, SendFile
from dom.gifs import GifSearch
from dom.models import StoredMessage
from dom.store import Store

log = logging.getLogger("dom.executor")
MAX_TYPING_SECONDS = 20  # typing time keeps scaling with length up to this; at 8 characters a second that is 160
MAX_REPLY_TYPING_SECONDS = 30  # all of one reply's typing, the time already shown while thinking included
BUBBLE_GAP_SECONDS = 1.0  # once that is spent, each later bubble still follows a short pause
TYPING_REFRESH_SECONDS = 4  # Telegram's typing indicator lapses after about 5 seconds
TYPING_EXPIRY_SECONDS = 5
GROUP_QUIP_CHARS = 32
GROUP_QUIP_SECONDS = 0.6
TYPING_CPS = 8.0
ASSISTANT_TYPING_CPS = 16.0
_background: set[asyncio.Task] = set()  # keeps scheduled deletions alive until they run


async def _type_for(bot, chat_id: int, seconds: float, thread_id: int | None, sleep) -> None:
    """Show "typing" for `seconds`, sending the indicator again before Telegram lets it lapse."""
    while True:
        await _show_typing(bot, chat_id, thread_id)
        if seconds <= TYPING_REFRESH_SECONDS:
            await sleep(seconds)
            return
        await sleep(TYPING_REFRESH_SECONDS)
        seconds -= TYPING_REFRESH_SECONDS


async def _show_typing(bot, chat_id: int, thread_id: int | None) -> bool:
    try:
        await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
    except Exception:
        log.debug("typing indicator failed in chat %s", chat_id, exc_info=True)
        return False
    return True


class TypingIndicator:
    """Refresh Telegram's cosmetic indicator and count time it was actually visible."""

    def __init__(self, bot, chat_id: int, thread_id: int | None, clock=monotonic):
        self.clock = clock
        self._shown_at: float | None = None
        self._previous = 0.0
        self._task = asyncio.create_task(self._keep(bot, chat_id, thread_id))

    async def _keep(self, bot, chat_id: int, thread_id: int | None) -> None:
        while True:
            if await _show_typing(bot, chat_id, thread_id):
                self._previous = self.seconds
                self._shown_at = self.clock()
            await asyncio.sleep(TYPING_REFRESH_SECONDS)

    @property
    def seconds(self) -> float:
        if self._shown_at is None:
            return self._previous
        return self._previous + min(self.clock() - self._shown_at, TYPING_EXPIRY_SECONDS)

    def stop(self) -> float:
        self._previous = self.seconds
        self._shown_at = None
        self._task.cancel()
        return self._previous


@asynccontextmanager
async def typing_while(bot, chat_id: int, thread_id: int | None = None):
    indicator = TypingIndicator(bot, chat_id, thread_id)
    try:
        yield indicator
    finally:
        indicator.stop()


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


TELEGRAM_TEXT_LIMIT = 4096


async def _tell_owner_held_back(store: Store, chat_id: int, why: str = "it wasn't eligible") -> None:
    """Leave a note in the owner's DM so they are not left wondering why a private reply never arrived."""
    try:
        title = (await store.get_chat_auth_status(chat_id))["title"] or str(chat_id)
        await store.record_owner_event(f"Held back a private reply in {title}: {why}")
    except Exception:
        log.exception("could not record the held-back private reply")


async def _private_reply_to_dm(
    bot, store: Store, chat_id: int, texts: list[str], reason: str,
    on_sent: Callable[[int], Awaitable[None]] | None,
) -> list[int]:
    """Telegram refused an owner-only bubble in the group (it allows them only from a group admin). Sending it there
    in public would leak what was meant for the owner alone, so it goes to the owner's DM instead, with why. It stays
    out of the DM's history, like a desktop report, and an event tells Dom it was moved."""
    owner = store.owner_id
    title = str(chat_id)
    with suppress(Exception):
        title = (await store.get_chat_auth_status(chat_id))["title"] or title
    why = "Telegram lets me send those only when I'm a group admin" if "ADMIN" in reason.upper() else "Telegram refused it"
    sent_ids = []
    try:
        for text in _pack([f"My private reply in {title} (it couldn't go there: {why}):", *texts]):
            with turn_log.timed("send"):
                sent = await bot.send_message(owner, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
            turn_log.bubble_sent()
            sent_ids.append(sent.message_id)
            if on_sent is not None:
                await on_sent(sent.message_id)
    except Exception as exc:
        log.exception("could not move a refused private reply to the owner's DM")
        if not sent_ids:
            await _tell_owner_held_back(store, chat_id, f"{why}, and the owner's DM refused it too ({type(exc).__name__})")
            return []
    await store.record_owner_event(f"Sent a private reply for {title} to the owner's DM: {why}")
    return sent_ids


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2  # Telegram counts message length in UTF-16 code units


def _pack(texts: list[str]) -> list[str]:
    """Join texts with blank lines into as few messages as fit Telegram's limit; a text never splits across two."""
    messages: list[str] = []
    for text in texts:
        if messages and _utf16(messages[-1]) + 2 + _utf16(text) <= TELEGRAM_TEXT_LIMIT:
            messages[-1] += "\n\n" + text
        else:
            messages.append(text)
    return messages


async def perform(
    bot,
    store: Store,
    chat_id: int,
    trigger_msg_id: int | None,
    actions: Actions,
    *,
    typing_cps: float = TYPING_CPS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    thread_id: int | None = None,
    now: Callable[[], datetime] | None = None,
    on_sent: Callable[[int], Awaitable[None]] | None = None,
    stickers: Mapping[str, Sequence[str]] | None = None,
    gifs: GifSearch | None = None,
    ephemeral_recipient: int | None = None,
    ephemeral_in_chat: bool = True,
    link_previews: bool = True,
    record: bool = True,
    typed_already: float | None = None,
    before_send: Callable[[], None] | None = None,
) -> list[int]:
    """Send the bubbles, reaction, sticker and GIF; returns the sent message ids. no_response does nothing at all."""
    if actions.no_response:
        return []
    if actions.private and chat_id < 0:
        if ephemeral_recipient is None or ephemeral_recipient != store.owner_id:
            log.warning("ignored a private action without an eligible receiver in chat %s", chat_id)
            await _tell_owner_held_back(store, chat_id)
            return []
        if not ephemeral_in_chat:  # Dom is not an admin there, so Telegram would refuse: straight to the DM
            if actions.bubbles and before_send is not None:
                before_send()
            return await _private_reply_to_dm(
                bot, store, chat_id, [b.text for b in actions.bubbles], "BOT_NOT_ADMIN", on_sent,
            )
        parameters = EphemeralMessageParameters(receiver_user_id=ephemeral_recipient)
        # Keep these bubbles out of the group's history and skip public side effects.
        sent_ids = []
        for bubble in actions.bubbles:
            if before_send is not None:
                before_send()
            try:
                with turn_log.timed("send"):
                    sent = await bot.send_message(
                        chat_id, bubble.text, message_thread_id=thread_id, ephemeral_message_parameters=parameters,
                    )
            except TelegramAPIError as error:
                log.warning("ephemeral send failed in chat %s: %s", chat_id, error.message)
                rest = actions.bubbles[len(sent_ids):]
                return sent_ids + await _private_reply_to_dm(
                    bot, store, chat_id, [b.text for b in rest], error.message, on_sent,
                )
            turn_log.bubble_sent()
            sent_ids.append(sent.message_id)
            if on_sent is not None:
                await on_sent(sent.message_id)
        return sent_ids
    if actions.react and trigger_msg_id is not None:
        await react(bot, chat_id, trigger_msg_id, actions.react)

    sent_ids: list[int] = []
    # "typing" already showed while Dom thought, so that time counts against the reply's budget; a long reply in
    # many bubbles would otherwise keep the friend waiting minutes after it was written.
    budget = max(0.0, MAX_REPLY_TYPING_SECONDS - (typed_already or 0.0))
    for index, bubble in enumerate(actions.bubbles):
        human_time = len(bubble.text) / typing_cps
        if typed_already is not None and index == 0:
            if chat_id < 0 and len(bubble.text) <= GROUP_QUIP_CHARS:
                human_time = min(human_time, GROUP_QUIP_SECONDS)
            human_time = max(0.0, human_time - typed_already)
        if typed_already is not None:
            if index:  # the caller's indicator stopped at the first send; the gap shows its own
                with turn_log.timed("typing"):
                    await _type_for(bot, chat_id, BUBBLE_GAP_SECONDS, thread_id, sleep)
            elif (seconds := min(human_time, MAX_TYPING_SECONDS, budget)) > 0:
                with turn_log.timed("typing"):
                    await sleep(seconds)  # the caller's indicator keeps refreshing in parallel until the send
        else:
            seconds = min(human_time, MAX_TYPING_SECONDS, budget)
            budget -= seconds
            if index > 0:
                seconds = max(seconds, BUBBLE_GAP_SECONDS)
            if seconds > 0:
                with turn_log.timed("typing"):
                    await _type_for(bot, chat_id, seconds, thread_id, sleep)
        reply = (
            ReplyParameters(message_id=bubble.reply_to, allow_sending_without_reply=True)
            if bubble.reply_to is not None
            else None
        )
        options = {} if link_previews else {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        if before_send is not None:
            before_send()
        with turn_log.timed("send"):
            sent = await bot.send_message(
                chat_id, bubble.text, reply_parameters=reply, message_thread_id=thread_id, **options,
            )
        turn_log.bubble_sent()
        if record:
            await store.record_new_message(
                StoredMessage(
                    chat_id=str(chat_id),
                    msg_id=sent.message_id,
                    reply_to_msg_id=bubble.reply_to if bubble.reply_to is not None else trigger_msg_id,
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
        else:
            if before_send is not None:
                before_send()
            sticker_id = await _send_media(
                bot.send_sticker(chat_id, choice(choices), message_thread_id=thread_id),
                store, chat_id, "sticker", {"kind": "sticker", "emoji": actions.sticker}, now, on_sent, trigger_msg_id,
            )
            if sticker_id is not None:
                sent_ids.append(sticker_id)
    if actions.gif and gifs is not None:
        try:
            with turn_log.timed("gif_search"):
                url = await gifs.find(actions.gif)
        except Exception:
            log.exception("gif search crashed in chat %s", chat_id)
            url = None
        if url:
            if before_send is not None:
                before_send()
            gif_id = await _send_media(
                bot.send_animation(chat_id, url, message_thread_id=thread_id),
                store, chat_id, "gif", {"kind": "gif", "query": actions.gif}, now, on_sent, trigger_msg_id,
            )
            if gif_id is not None:
                sent_ids.append(gif_id)
    return sent_ids


async def _send_media(
    sending: Awaitable, store: Store, chat_id: int, kind: str, media: dict,
    now: Callable[[], datetime] | None, on_sent: Callable[[int], Awaitable[None]] | None, trigger_msg_id: int | None,
) -> int | None:
    """Await one media send and record it; a Telegram refusal is logged and means nothing was sent."""
    try:
        with turn_log.timed("send"):
            sent = await sending
    except TelegramAPIError:
        log.warning("%s send failed in chat %s", kind, chat_id)
        return None
    turn_log.bubble_sent()
    await store.record_new_message(StoredMessage(
        chat_id=str(chat_id), msg_id=sent.message_id, is_bot=True, role="assistant",
        reply_to_msg_id=trigger_msg_id,
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
        with turn_log.timed("send"):
            await bot.set_message_reaction(chat_id, msg_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
    except (TelegramBadRequest, ValidationError):
        log.warning("reaction %r rejected in chat %s", emoji, chat_id)
        return False
    except TelegramAPIError as error:
        log.warning("reaction %r failed in chat %s (%s)", emoji, chat_id, type(error).__name__)
        return None
    return True


PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
PHOTO_MAX_BYTES = 10_000_000  # Telegram's limit for a photo; it also refuses one whose width + height passes 10000


async def send_files(bot, store: Store, chat_id: int, files: Sequence[SendFile]) -> None:
    if chat_id != store.owner_id:
        return
    for file in files:
        try:
            if file.filename.lower().endswith(PHOTO_SUFFIXES) and len(file.data) <= PHOTO_MAX_BYTES:
                try:  # a photo opens inline, with no download tap
                    await bot.send_photo(chat_id, BufferedInputFile(file.data, filename=file.filename))
                    continue
                except TelegramBadRequest:  # too large in pixels or too long a page for a photo: send it as a file
                    pass
            await bot.send_document(chat_id, BufferedInputFile(file.data, filename=file.filename))
        except TelegramAPIError:
            log.warning("could not send a desktop result document")
            await bot.send_message(chat_id, f"Couldn't send {file.filename}.")
