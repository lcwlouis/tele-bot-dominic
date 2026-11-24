"""Perform an Actions result in Telegram with humanlike timing."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from errno import ECONNREFUSED
from random import choice
from time import monotonic
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

from aiohttp import ClientConnectorError
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramMigrateToChat, TelegramNetworkError, TelegramRetryAfter,
)
from aiogram.types import (
    BufferedInputFile, EphemeralMessageParameters, LinkPreviewOptions, MessageEntity, ReactionTypeEmoji,
    ReplyParameters,
)
from pydantic import ValidationError

from dom import turn_log
from dom.actions import TELEGRAM_TEXT_LIMIT, Actions, Bubble, SendFile, utf16_len
from dom.gifs import GifSearch
from dom.models import StoredMessage
from dom.store import Store
from dom.studio.cloud import providers as studio_providers
from dom.studio.policy import Kind, enabled
from dom.stickers import resolve_sticker, sticker_fallback
from dom.telegram.format import TextChunk, format_delivery, format_text, with_entities

if TYPE_CHECKING:
    from dom.telegram.handlers import App

T = TypeVar("T")

log = logging.getLogger("dom.executor")
MAX_TYPING_SECONDS = 20  # typing time keeps scaling with length up to this; at 8 characters a second that is 160
MAX_REPLY_TYPING_SECONDS = 30  # all of one reply's typing, the time already shown while thinking included
BUBBLE_GAP_SECONDS = 1.0  # the shortest pause before a later bubble
# Once the reply's typing budget is spent, a later bubble still takes time for its length, typed twice as fast
# (up to 6 s): a flat short pause made a long answer arrive several long bubbles a second apart.
TAIL_SPEEDUP = 2.0
MAX_TAIL_SECONDS = 6.0
TYPING_REFRESH_SECONDS = 4  # Telegram's typing indicator lapses after about 5 seconds
TYPING_EXPIRY_SECONDS = 5
GROUP_QUIP_CHARS = 32
GROUP_QUIP_SECONDS = 0.6
TYPING_CPS = 8.0
ASSISTANT_TYPING_CPS = 16.0
MAX_FLOOD_RETRIES = 3  # bounded retries on TelegramRetryAfter
MAX_FLOOD_WAIT_SECONDS = 10.0  # total flood-wait budget per reply so a chat lock is never held for long
_background: set[asyncio.Task] = set()  # keeps scheduled deletions alive until they run


class FloodBudgetExhausted(Exception):
    """Raised when retrying a Telegram flood wait would exceed the reply's time budget."""


@dataclass
class FloodBudget:
    """Shared elapsed-time budget for Telegram flood retries across one reply."""

    remaining: float = MAX_FLOOD_WAIT_SECONDS
    exhausted: bool = False


class PerformResult(list[int]):
    """Message ids sent by `perform`, plus reaction, media-fallback, and flood-limit status."""

    def __init__(
        self,
        ids: Sequence[int] = (),
        *,
        reacted: bool = False,
        reaction_result: bool | None = None,
        media_fallback: bool = False,
        flood_limited: bool = False,
    ):
        super().__init__(ids)
        self.reacted = reacted
        self.reaction_result = reaction_result
        self.media_fallback = media_fallback
        self.flood_limited = flood_limited


async def retry_telegram(
    call: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_retries: int = MAX_FLOOD_RETRIES,
    budget: FloodBudget | None = None,
    retry_refused: bool = False,
) -> T:
    """Retry bounded flood waits and, when requested, a refused connection before any request was sent."""
    for attempt in range(max_retries + 1):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            wait = max(0.0, float(exc.retry_after))
            if budget is not None:
                if attempt >= max_retries or wait > budget.remaining:
                    budget.exhausted = True
                    log.warning(
                        "Telegram flood control exceeded budget (need %.1fs, %.1fs left; attempt %d/%d)",
                        wait, budget.remaining, attempt + 1, max_retries,
                    )
                    raise FloodBudgetExhausted(str(exc)) from exc
                started = monotonic()
                log.warning("Telegram flood control: waiting %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
                await sleep(wait)
                budget.remaining = max(0.0, budget.remaining - max(wait, monotonic() - started))
            else:
                if attempt >= max_retries:
                    raise
                log.warning("Telegram flood control: waiting %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
                await sleep(wait)
        except TelegramNetworkError as exc:
            cause = exc.__cause__
            if (
                not retry_refused or attempt >= max_retries
                or not isinstance(cause, ClientConnectorError) or cause.os_error.errno != ECONNREFUSED
            ):
                raise
            await sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


async def _send_typing(bot, chat_id: int, thread_id: int | None) -> bool:
    """Send one typing indicator; cosmetic only, so failures (including flood waits) are skipped, never retried."""
    try:
        await bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        return True
    except TelegramRetryAfter as exc:
        log.debug("skipping typing indicator in chat %s due to flood control (retry_after=%s)", chat_id, exc.retry_after)
        return False
    except Exception:
        log.debug("typing indicator failed in chat %s", chat_id, exc_info=True)
        return False


async def _type_for(bot, chat_id: int, seconds: float, thread_id: int | None, sleep) -> None:
    """Show "typing" for `seconds`, sending the indicator again before Telegram lets it lapse.
    A flooded or failed send_chat_action is skipped and never aborts the reply."""
    while True:
        if not await _send_typing(bot, chat_id, thread_id):
            await sleep(seconds)
            return
        if seconds <= TYPING_REFRESH_SECONDS:
            await sleep(seconds)
            return
        await sleep(TYPING_REFRESH_SECONDS)
        seconds -= TYPING_REFRESH_SECONDS


class TypingIndicator:
    """Refresh Telegram's cosmetic indicator and count the time it was actually visible. A flooded or failed send
    stops the refreshing; the indicator lapses by itself a few seconds later."""

    def __init__(
        self, bot, chat_id: int, thread_id: int | None, clock=monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.clock = clock
        self._shown_at: float | None = None
        self._previous = 0.0
        self._task = asyncio.create_task(self._keep(bot, chat_id, thread_id, sleep))

    async def _keep(self, bot, chat_id: int, thread_id: int | None, sleep) -> None:
        while True:
            if not await _send_typing(bot, chat_id, thread_id):
                return
            self._previous = self.seconds
            self._shown_at = self.clock()
            await sleep(TYPING_REFRESH_SECONDS)

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
async def typing_while(
    bot,
    chat_id: int,
    thread_id: int | None = None,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
):
    """Show "typing" while Dom is still working out his reply (the model calls), so a slow answer does not look
    like a crash. The indicator is cosmetic: a flooded or failed send stops refreshing, and it lapses by itself a few
    seconds after the block ends if Dom decides to say nothing."""
    indicator = TypingIndicator(bot, chat_id, thread_id, sleep=sleep)
    try:
        yield indicator
    finally:
        indicator.stop()


async def _delete_later(bot, store: Store, chat_id: int, message_id: int, delay: float, sleep) -> None:
    await sleep(delay)
    try:
        await retry_telegram(lambda: bot.delete_message(chat_id, message_id), sleep=sleep, budget=FloodBudget())
    except (TelegramAPIError, FloodBudgetExhausted):
        log.exception("could not delete message %s in chat %s", message_id, chat_id)
        return
    await store.record_message_delete(chat_id, [message_id])


async def cancel_pending_deletions() -> None:
    """Stop scheduled send-then-delete tasks (shutdown): they need the bot and store, which are closing."""
    tasks = list(_background)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _tell_owner_held_back(store: Store, chat_id: int, why: str) -> None:
    """Leave a note in the owner's DM so they are not left wondering why a private reply never arrived."""
    try:
        title = (await store.get_chat_auth_status(chat_id))["title"] or str(chat_id)
        await store.record_owner_event(f"Held back a private reply in {title}: {why}")
    except Exception:
        log.exception("could not record the held-back private reply")


async def send_chunk(
    bot, chat_id: int, chunk: TextChunk, *, sleep=asyncio.sleep, budget: FloodBudget | None = None,
    retry_refused: bool = False, authorize_message: Callable[[], Awaitable[None]] | None = None, **options,
):
    async def send(text, **kwargs):
        async def attempt():
            if authorize_message is not None:
                await authorize_message()
            return await bot.send_message(chat_id, text, **kwargs)

        return await retry_telegram(attempt, sleep=sleep, budget=budget, retry_refused=retry_refused)

    return await with_entities(send, *chunk, **options)


async def send_text(
    bot, chat_id: int, text: str, *, sleep=asyncio.sleep, budget: FloodBudget | None = None,
    literal: bool = False, pre: bool = False, reply_markup=None, **options,
):
    """Send a notice or card. Its keyboard belongs to the final message."""
    chunks = format_text(text, literal=literal, pre=pre)
    sent = None
    for index, chunk in enumerate(chunks):
        if reply_markup is not None and index == len(chunks) - 1:
            options["reply_markup"] = reply_markup
        sent = await send_chunk(bot, chat_id, chunk, sleep=sleep, budget=budget, **options)
    return sent


async def edit_text(bot, chat_id: int, message_id: int, text: str, *, sleep=asyncio.sleep, literal: bool = False, **options):
    """Replace a single status card in place, within Telegram's message length."""
    chunks = format_text(text, literal=literal)
    if not chunks:
        return None

    async def edit(text, **kwargs):
        return await retry_telegram(
            lambda: bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs), sleep=sleep,
        )

    return await with_entities(edit, *chunks[0], **options)


def _delivery_bubbles(bubbles: Sequence[Bubble]) -> list[Bubble]:
    delivery = []
    for bubble in bubbles:
        if bubble._telegram_chunk is not None:
            delivery.append(bubble)
            continue
        for index, (chunk, history) in enumerate(format_delivery(bubble.text)):
            piece = bubble.model_copy(update={"text": history, "reply_to": None if index else bubble.reply_to})
            piece._telegram_chunk = chunk
            delivery.append(piece)
    return delivery


async def _private_reply_to_dm(
    bot, store: Store, chat_id: int, chunks: list[TextChunk], reason: str,
    *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, budget: FloodBudget | None = None,
    on_sent: Callable[[int], Awaitable[None]] | None = None,
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
        header = format_text(f"My private reply in {title} (it couldn't go there: {why}):", literal=True)
        for chunk in _pack([*header, *chunks]):
            with turn_log.timed("send"):
                sent = await send_chunk(
                    bot, owner, chunk, sleep=sleep, budget=budget,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            turn_log.bubble_sent()
            sent_ids.append(sent.message_id)
            if on_sent is not None:
                await on_sent(sent.message_id)
    except FloodBudgetExhausted:
        log.warning("private reply fallback to owner's DM hit flood budget for chat %s", chat_id)
        return sent_ids
    except Exception as exc:
        log.exception("could not move a refused private reply to the owner's DM")
        if not sent_ids:
            await _tell_owner_held_back(store, chat_id, f"{why}, and the owner's DM refused it too ({type(exc).__name__})")
            return []
    await store.record_owner_event(f"Sent a private reply for {title} to the owner's DM: {why}")
    return sent_ids


def _pack(chunks: list[TextChunk]) -> list[TextChunk]:
    """Pack adjacent rendered chunks while keeping entity offsets relative to the new message."""
    messages: list[TextChunk] = []
    for text, entities in chunks:
        if messages and (offset := utf16_len(messages[-1][0]) + 2) + utf16_len(text) <= TELEGRAM_TEXT_LIMIT:
            prev, previous_entities = messages[-1]
            messages[-1] = (
                prev + "\n\n" + text,
                previous_entities + [entity.model_copy(update={"offset": entity.offset + offset}) for entity in entities],
            )
        else:
            messages.append((text, entities))
    return messages


def _strip_reply_to(bubbles: Sequence[Bubble]) -> list[Bubble]:
    return [b.model_copy(update={"reply_to": None}) for b in bubbles]


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
    ephemeral_ineligible_reason: str | None = None,
    link_previews: bool = True,
    record: bool = True,
    disable_notification: bool = False,
    retry_refused: bool = False,
    authorize_message: Callable[[], Awaitable[None]] | None = None,
    typed_already: float | None = None,
    reply_elapsed: float = 0.0,
    before_send: Callable[[], None] | None = None,
    on_migrate: Callable[[Actions], None] | None = None,
    flood_budget: float = MAX_FLOOD_WAIT_SECONDS,
    queue_studio: Callable[[], None] | None = None,
) -> PerformResult:
    """Send the bubbles, reaction, sticker and GIF; returns the sent message ids. no_response does nothing at all."""
    if actions.no_response:
        return PerformResult()
    flood = FloodBudget(remaining=flood_budget)
    bubbles = _delivery_bubbles(actions.bubbles)
    if actions.private and chat_id < 0:
        if ephemeral_recipient is None or ephemeral_recipient != store.owner_id:
            why = ephemeral_ineligible_reason or (
                "ephemeral replies are off or it was a friend's turn"
                if ephemeral_recipient is None
                else "the receiver was not the owner"
            )
            log.warning("ignored a private action without an eligible receiver in chat %s: %s", chat_id, why)
            await _tell_owner_held_back(store, chat_id, why)
            return PerformResult()
        if not ephemeral_in_chat:  # Dom is not an admin there, so Telegram would refuse: straight to the DM
            if bubbles and before_send is not None:
                before_send()
            moved = await _private_reply_to_dm(
                bot, store, chat_id, [b._telegram_chunk for b in bubbles], "BOT_NOT_ADMIN",
                sleep=sleep, budget=flood, on_sent=on_sent,
            )
            return PerformResult(moved, flood_limited=flood.exhausted)
        parameters = EphemeralMessageParameters(receiver_user_id=ephemeral_recipient)
        # Keep these bubbles out of the group's history and skip public side effects.
        sent_ids: list[int] = []
        for index, bubble in enumerate(bubbles):
            if before_send is not None:
                before_send()
            try:
                with turn_log.timed("send"):
                    sent = await send_chunk(
                        bot, chat_id, bubble._telegram_chunk, sleep=sleep, budget=flood,
                        message_thread_id=thread_id, ephemeral_message_parameters=parameters,
                    )
            except TelegramMigrateToChat:
                if on_migrate is not None:
                    on_migrate(actions.model_copy(update={"bubbles": _strip_reply_to(bubbles[index:])}))
                raise
            except FloodBudgetExhausted:
                return PerformResult(sent_ids, flood_limited=True)
            except TelegramAPIError as error:
                log.warning("ephemeral send failed in chat %s: %s", chat_id, error.message)
                rest = bubbles[index:]
                moved = await _private_reply_to_dm(
                    bot, store, chat_id, [b._telegram_chunk for b in rest], error.message,
                    sleep=sleep, budget=flood, on_sent=on_sent,
                )
                return PerformResult(sent_ids + moved, flood_limited=flood.exhausted)
            turn_log.bubble_sent()
            sent_ids.append(sent.message_id)
            if on_sent is not None:
                await on_sent(sent.message_id)
        return PerformResult(sent_ids)

    committed = False
    reaction_done = False
    reaction_result: bool | None = None

    async def enter_send_boundary() -> bool:
        """Cross the send boundary once, then send any chosen reaction so a restart before sending cannot react twice."""
        nonlocal committed, reaction_done, reaction_result
        if not committed:
            committed = True
            if before_send is not None:
                before_send()
        if not reaction_done:
            reaction_done = True
            if actions.react and trigger_msg_id is not None:
                reaction_result = await react(bot, chat_id, trigger_msg_id, actions.react, sleep=sleep, budget=flood)
                if flood.exhausted:
                    return False
                if not reaction_result:
                    log.warning("reaction %r not sent in chat %s (result=%s)", actions.react, chat_id, reaction_result)
        return True

    sent_ids = []
    # "typing" already showed while Dom thought, so that time counts against the reply's budget; a long reply in
    # many bubbles would otherwise keep the friend waiting minutes after it was written.
    budget = max(0.0, MAX_REPLY_TYPING_SECONDS - (typed_already or 0.0))
    for index, bubble in enumerate(bubbles):
        chunk = bubble._telegram_chunk
        human_time = len(chunk[0]) / typing_cps
        if index == 0 and typed_already is not None:
            if chat_id < 0 and len(chunk[0]) <= GROUP_QUIP_CHARS:
                human_time = min(human_time, GROUP_QUIP_SECONDS)
            # Reply time counts even when the indicator was late or failed. It overlaps visible typing.
            human_time = max(0.0, human_time - max(typed_already, reply_elapsed))
        seconds = min(human_time, MAX_TYPING_SECONDS, budget)
        budget -= seconds
        if index > 0:
            seconds = max(seconds, min(human_time / TAIL_SPEEDUP, MAX_TAIL_SECONDS), BUBBLE_GAP_SECONDS)
        if seconds > 0:
            with turn_log.timed("typing"):
                if index == 0 and typed_already is not None:
                    await sleep(seconds)  # the caller's indicator keeps refreshing in parallel until the send
                else:
                    await _type_for(bot, chat_id, seconds, thread_id, sleep)
        reply = (
            ReplyParameters(message_id=bubble.reply_to, allow_sending_without_reply=True)
            if bubble.reply_to is not None
            else None
        )
        options = {} if link_previews else {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        if disable_notification:
            options["disable_notification"] = True
        try:
            if not await enter_send_boundary():
                return PerformResult(sent_ids, reacted=False, reaction_result=None, flood_limited=True)
            with turn_log.timed("send"):
                sent = await send_chunk(
                    bot, chat_id, chunk, reply_parameters=reply, message_thread_id=thread_id,
                    sleep=sleep, budget=flood, retry_refused=retry_refused, authorize_message=authorize_message, **options,
                )
        except TelegramMigrateToChat:
            if on_migrate is not None:
                on_migrate(actions.model_copy(update={"bubbles": _strip_reply_to(bubbles[index:]), "react": None}))
            raise
        except FloodBudgetExhausted:
            return PerformResult(
                sent_ids,
                reacted=reaction_result is True,
                reaction_result=reaction_result,
                flood_limited=True,
            )
        turn_log.bubble_sent()
        if record:
            await store.record_new_message(
                StoredMessage(
                    chat_id=str(chat_id),
                    msg_id=sent.message_id,
                    reply_to_msg_id=bubble.reply_to if bubble.reply_to is not None else trigger_msg_id,
                    thread_id=thread_id,
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

    sticker_failed = False
    if actions.sticker:
        resolved = resolve_sticker(stickers, actions.sticker)
        choices, emoji = resolved if resolved else ((), actions.sticker)
        if not choices:
            log.warning("unknown sticker emoji %r in chat %s", actions.sticker, chat_id)
            sticker_failed = True
        else:
            picked = choice(choices)
            try:
                if not await enter_send_boundary():
                    return PerformResult(sent_ids, reacted=False, reaction_result=None, flood_limited=True)
                sticker_id = await _send_media(
                    lambda p=picked: bot.send_sticker(chat_id, p, message_thread_id=thread_id),
                    store, chat_id, "sticker", {"kind": "sticker", "emoji": emoji}, now, on_sent,
                    thread_id=thread_id, sleep=sleep, record=record, budget=flood, trigger_msg_id=trigger_msg_id,
                    authorize_message=authorize_message,
                )
            except TelegramMigrateToChat:
                if on_migrate is not None:
                    on_migrate(Actions(sticker=actions.sticker, gif=actions.gif))
                raise
            except FloodBudgetExhausted:
                return PerformResult(
                    sent_ids,
                    reacted=reaction_result is True,
                    reaction_result=reaction_result,
                    flood_limited=True,
                )
            if sticker_id is not None:
                sent_ids.append(sticker_id)
            else:
                sticker_failed = True

    gif_failed = False
    if actions.gif:
        url = None
        if gifs is not None:
            try:
                with turn_log.timed("gif_search"):
                    url = await gifs.find(actions.gif)
            except Exception:
                log.exception("gif search crashed in chat %s", chat_id)
        if url:
            try:
                if not await enter_send_boundary():
                    return PerformResult(sent_ids, reacted=False, reaction_result=None, flood_limited=True)
                gif_id = await _send_media(
                    lambda u=url: bot.send_animation(chat_id, u, message_thread_id=thread_id),
                    store, chat_id, "gif", {"kind": "gif", "query": actions.gif}, now, on_sent,
                    thread_id=thread_id, sleep=sleep, record=record, budget=flood, trigger_msg_id=trigger_msg_id,
                    authorize_message=authorize_message,
                )
            except TelegramMigrateToChat:
                if on_migrate is not None:
                    on_migrate(Actions(gif=actions.gif))
                raise
            except FloodBudgetExhausted:
                return PerformResult(
                    sent_ids,
                    reacted=reaction_result is True,
                    reaction_result=reaction_result,
                    flood_limited=True,
                )
        else:
            gif_id = None
        if gif_id is not None:
            sent_ids.append(gif_id)
        else:
            gif_failed = True

    media_fallback = False
    fallback_text = None
    if not sent_ids and not actions.bubbles and (sticker_failed or gif_failed):
        # A failed sticker falls back to its bare emoji only (never "📚2" or words); a failed GIF to its query.
        fallback_text = sticker_fallback(actions.sticker) if sticker_failed and actions.sticker else f"*{actions.gif}*"
    if fallback_text:
        options = {} if link_previews else {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        try:
            if not await enter_send_boundary():
                return PerformResult(sent_ids, reacted=False, reaction_result=None, flood_limited=True)
            with turn_log.timed("send"):
                if fallback_text == f"*{actions.gif}*":  # the GIF's query in italics, set directly, not via Markdown
                    italic = MessageEntity(type="italic", offset=0, length=utf16_len(actions.gif))
                    sent = await send_chunk(bot, chat_id, (actions.gif, [italic]), sleep=sleep, budget=flood,
                                            message_thread_id=thread_id, authorize_message=authorize_message, **options)
                else:
                    sent = await send_text(bot, chat_id, fallback_text, sleep=sleep, budget=flood,
                                           message_thread_id=thread_id, authorize_message=authorize_message, **options)
        except TelegramMigrateToChat:
            if on_migrate is not None:
                on_migrate(Actions(bubbles=[Bubble(text=fallback_text)]))
            raise
        except FloodBudgetExhausted:
            return PerformResult(
                sent_ids,
                reacted=reaction_result is True,
                reaction_result=reaction_result,
                flood_limited=True,
            )
        turn_log.bubble_sent()
        if record:
            await store.record_new_message(StoredMessage(
                chat_id=str(chat_id), msg_id=sent.message_id, reply_to_msg_id=trigger_msg_id, thread_id=thread_id,
                is_bot=True, role="assistant", visibility_scope=store.visibility_for(chat_id), text=fallback_text,
                created_at=(now() if now is not None else datetime.now(timezone.utc)).isoformat(),
            ))
        sent_ids.append(sent.message_id)
        media_fallback = True
        if on_sent is not None:
            await on_sent(sent.message_id)

    if not reaction_done and actions.react and trigger_msg_id is not None:
        if not await enter_send_boundary():
            return PerformResult(sent_ids, reacted=False, reaction_result=None, flood_limited=True)

    if queue_studio is not None and not actions.private and not flood.exhausted:
        if await enter_send_boundary():
            queue_studio()

    return PerformResult(
        sent_ids,
        reacted=reaction_result is True,
        reaction_result=reaction_result,
        media_fallback=media_fallback,
        flood_limited=flood.exhausted,
    )


async def _send_media(
    sending: Callable[[], Awaitable], store: Store, chat_id: int, kind: str, media: dict | None,
    now: Callable[[], datetime] | None, on_sent: Callable[[int], Awaitable[None]] | None,
    *, thread_id: int | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, record: bool = True,
    budget: FloodBudget | None = None, trigger_msg_id: int | None = None,
    history_text: str | None = None, authorize_message: Callable[[], Awaitable[None]] | None = None,
) -> int | None:
    """Await one media send and record it; a Telegram refusal is logged and means nothing was sent."""
    async def attempt():
        if authorize_message is not None:
            await authorize_message()
        return await sending()

    try:
        with turn_log.timed("send"):
            sent = await retry_telegram(attempt, sleep=sleep, budget=budget)
    except TelegramMigrateToChat:
        raise
    except TelegramAPIError:
        log.warning("%s send failed in chat %s", kind, chat_id)
        return None
    turn_log.bubble_sent()
    if record:
        await store.record_new_message(StoredMessage(
            chat_id=str(chat_id), msg_id=sent.message_id, reply_to_msg_id=trigger_msg_id, thread_id=thread_id,
            is_bot=True, role="assistant", visibility_scope=store.visibility_for(chat_id),
            text=history_text if history_text is not None else f"[{kind}]",
            media_json=json.dumps(media) if media is not None else None,
            created_at=(now() if now is not None else datetime.now(timezone.utc)).isoformat(),
        ))
    if on_sent is not None:
        await on_sent(sent.message_id)
    return sent.message_id


async def _studio_indicator(bot, chat_id: int, thread_id: int | None, action: str) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id, action, message_thread_id=thread_id)
        except TelegramAPIError:
            return
        await asyncio.sleep(TYPING_REFRESH_SECONDS)


async def deliver_studio(
    app: App, chat_id: int, scope: str, thread_id: int | None, trigger_msg_id: int,
    actions: Actions, on_sent: Callable[[int], Awaitable[None]],
) -> None:
    """Generate and send screened media after the text, without occupying a turn."""
    if actions.private or actions.no_response:
        return
    client = app.studio
    now = app.dispatch.now
    budget = FloodBudget()
    video_voice: bytes | None = None
    video_voice_attempted = False

    async def allowed(kind: Kind) -> bool:
        return (
            (scope != "owner_dm" or chat_id == app.owner_id)
            and enabled(app.config.studio, scope, kind)
            and await app.dispatch._approved(chat_id)
        )

    async def generate(kind: Kind, text: str, *, reuse_video_voice: bool = False) -> bool | None:
        # None is a refused job. A failed video job may still fall back to an allowed voice note.
        if not await allowed(kind):
            return None
        local_ready = client is not None and client.ready
        cloud = app.cloud_studio
        cloud_ready = cloud is not None and studio_providers(app.config.studio, kind)
        voice_backup = cloud is not None and studio_providers(app.config.studio, "voice")
        if not reuse_video_voice and not local_ready and not cloud_ready and not (
            kind in {"video_note", "special"} and voice_backup
        ):
            return None
        async with app.studio_quota.reserve(app.config.studio, scope, chat_id, kind, now()) as reserved:
            if not reserved or not await allowed(kind):
                return None
            if kind == "voice":
                action, filename, send = "record_voice", "voice.ogg", app.bot.send_voice
                request = client.voice(text) if local_ready and not reuse_video_voice else None
            elif kind == "photo":
                action, filename, send = "upload_photo", "photo.png", app.bot.send_photo
                request = client.image(text, actions.outfit) if local_ready else None
            else:
                action, filename, send = "record_video_note", "video.mp4", app.bot.send_video_note
                request = client.video(text, kind == "special", actions.outfit) if local_ready else None
            async def cloud_allowed() -> bool:
                if not await allowed(kind):
                    return False
                return not await app.dispatch.usage.over_cap(app.config, app.bot, sleep=app.sleep)

            async def make_video_voice() -> bytes | None:
                nonlocal video_voice, video_voice_attempted
                video_voice_attempted = True
                if client is not None and client.ready:
                    video_voice = await client.voice(text)
                    if video_voice is not None:
                        await asyncio.shield(app.dispatch.usage.record(
                            "studio_voice", "local-studio", "local", {}, chat_id,
                        ))
                if video_voice is None and cloud is not None:
                    video_voice = await cloud.generate(app.config, "voice", text, None, chat_id, cloud_allowed)
                return video_voice

            if request is None and not cloud_ready and not reuse_video_voice:
                return False
            indicator = asyncio.create_task(_studio_indicator(app.bot, chat_id, thread_id, action))
            try:
                data = video_voice if reuse_video_voice else (await request if request is not None else None)
                if data is None and cloud_ready and not reuse_video_voice:
                    if kind in {"video_note", "special"}:
                        data = await cloud.generate_video(
                            app.config, kind, actions.outfit, chat_id, cloud_allowed, make_video_voice,
                        )
                    else:
                        data = await cloud.generate(app.config, kind, text, actions.outfit, chat_id, cloud_allowed)
            finally:
                indicator.cancel()
                await asyncio.gather(indicator, return_exceptions=True)
            if data is None:
                return False
            if not await allowed(kind):
                return None
            if kind == "photo" and data.startswith(b"\xff\xd8\xff"):
                filename = "photo.jpg"
            message_kind = "video_note" if kind == "special" else kind
            key = "scene" if kind == "photo" else "text"
            media = {"kind": message_kind, key: text}
            if kind == "special":
                media["special"] = True
            with app.dispatch.protect_send() as commit:
                commit()
                delivered = await _send_media(
                    lambda: send(chat_id, BufferedInputFile(data, filename=filename), message_thread_id=thread_id),
                    app.store, chat_id, message_kind, media, now, on_sent, thread_id=thread_id,
                    sleep=app.sleep, budget=budget, trigger_msg_id=trigger_msg_id, history_text=f"[{message_kind}] {text}",
                )
            return delivered is not None

    async def fallback(kind: Kind, text: str) -> None:
        if not await allowed(kind):
            return
        with app.dispatch.protect_send() as commit:
            commit()
            await _send_media(
                lambda: send_text(app.bot, chat_id, text, literal=True, sleep=app.sleep, budget=budget,
                                  message_thread_id=thread_id,
                                  link_preview_options=LinkPreviewOptions(is_disabled=True)),
                app.store, chat_id, "text", None, now, on_sent, thread_id=thread_id,
                sleep=app.sleep, budget=budget, trigger_msg_id=trigger_msg_id, history_text=text,
            )

    try:
        if actions.voice and not await generate("voice", actions.voice):
            await fallback("voice", actions.voice)
        if actions.video_note:
            video = actions.video_note
            kind = "special" if video.special else "video_note"
            result = await generate(kind, video.text)
            if result is False and await allowed(kind) and (not video_voice_attempted or video_voice is not None):
                result = await generate("voice", video.text, reuse_video_voice=video_voice_attempted)
            if not result:
                await fallback(kind, video.text)
        if actions.image:
            await generate("photo", actions.image)
    except (TelegramMigrateToChat, FloodBudgetExhausted):
        log.warning("studio delivery stopped in chat %s", chat_id)
    except Exception as error:
        log.warning("studio delivery failed (%s)", type(error).__name__)


async def react(
    bot,
    chat_id: int,
    msg_id: int,
    emoji: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    budget: FloodBudget | None = None,
) -> bool | None:
    """React to a message: True when sent, False when Telegram refuses it (e.g. the chat has reactions
    turned off), None when it failed for another reason, such as the network or flood budget."""
    try:
        with turn_log.timed("send"):
            await retry_telegram(
                lambda: bot.set_message_reaction(chat_id, msg_id, reaction=[ReactionTypeEmoji(emoji=emoji)]),
                sleep=sleep, budget=budget,
            )
    except TelegramMigrateToChat:
        raise
    except (TelegramBadRequest, ValidationError):
        log.warning("reaction %r rejected in chat %s", emoji, chat_id)
        return False
    except (TelegramAPIError, FloodBudgetExhausted) as error:
        log.warning("reaction %r failed in chat %s (%s)", emoji, chat_id, type(error).__name__)
        return None
    return True


PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
PHOTO_MAX_BYTES = 10_000_000  # Telegram's limit for a photo; it also refuses one whose width + height passes 10000


async def send_files(
    bot,
    store: Store,
    chat_id: int,
    files: Sequence[SendFile],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if chat_id != store.owner_id:
        return
    flood = FloodBudget()
    for file in files:
        try:
            if file.filename.lower().endswith(PHOTO_SUFFIXES) and len(file.data) <= PHOTO_MAX_BYTES:
                try:  # a photo opens inline, with no download tap
                    await retry_telegram(
                        lambda f=file: bot.send_photo(chat_id, BufferedInputFile(f.data, filename=f.filename)),
                        sleep=sleep, budget=flood,
                    )
                    continue
                except TelegramBadRequest:  # too large in pixels or too long a page for a photo: send it as a file
                    pass
            await retry_telegram(
                lambda f=file: bot.send_document(chat_id, BufferedInputFile(f.data, filename=f.filename)),
                sleep=sleep, budget=flood,
            )
        except FloodBudgetExhausted:
            log.warning("sending desktop result documents hit Telegram flood budget")
            return
        except TelegramAPIError:
            log.warning("could not send a desktop result document")
            try:
                await send_text(bot, chat_id, f"Couldn't send {file.filename}.", sleep=sleep, budget=flood, literal=True)
            except FloodBudgetExhausted:
                return
