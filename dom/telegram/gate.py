"""Approval gate: nothing reaches a model until the owner approves the chat."""

from __future__ import annotations

import logging
from typing import Literal

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.models import IncomingMessage
from dom.store import Store
from dom.telegram.executor import retry_telegram

log = logging.getLogger("dom.gate")

Decision = Literal["process", "hold", "drop"]


async def decide(store: Store, msg: IncomingMessage, owner_id: int | None) -> Decision:
    """process: approved. hold: pending, record only. drop: owner unset, or the chat is ignored, left or blocked."""
    return await decide_chat(store, msg.chat_id, owner_id)


async def decide_chat(store: Store, chat_id: int, owner_id: int | None) -> Decision:
    if owner_id is None:
        return "drop"
    status = (await store.get_chat_auth_status(chat_id))["authorization_status"]
    if status == "approved":
        return "process"
    if status == "pending_approval":
        return "hold"
    return "drop"


def first_line(text: str | None, limit: int = 160) -> str:
    """A pending chat's first message as one short line, so the owner can tell who is asking."""
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


async def _send_prompt(
    bot, owner_id: int, chat_id: int, title: str, kind: str, requested_by: str, first_text: str | None = None,
    *, sleep=None,
) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", callback_data=f"approve:{chat_id}"),
        InlineKeyboardButton(text="Ignore", callback_data=f"ignore:{chat_id}"),
    ]])
    text = f'{requested_by or "Someone"} wants Dom in the {kind} "{title}" (Telegram ID {chat_id}).'
    if first := first_line(first_text):
        text += f'\nFirst message: "{first}"'
    text += "\nApprove?"
    kw = {"sleep": sleep} if sleep is not None else {}
    await retry_telegram(lambda: bot.send_message(owner_id, text, reply_markup=keyboard), **kw)


async def request_approval(
    bot, store: Store, owner_id: int, chat_id: int, title: str, kind: str, requested_by: str, first_text: str | None = None,
    *, sleep=None,
) -> None:
    """DM the owner Approve/Ignore buttons, once per pending chat."""
    if not await store.claim_approval_prompt(chat_id):
        return
    await store.note_pending_chat(str(chat_id), title, requested_by)
    try:
        await _send_prompt(bot, owner_id, chat_id, title, kind, requested_by, first_text, sleep=sleep)
    except (TelegramBadRequest, TelegramForbiddenError):  # definitely not delivered: allow a retry
        log.exception("the approval prompt for chat %s was rejected", chat_id)
        await store.release_approval_prompt(chat_id)
    except Exception:  # it may have arrived (timeout, network): stay reserved; the owner can use /pending
        log.exception("could not confirm delivery of the approval prompt for chat %s", chat_id)


async def resend_pending(bot, store: Store, owner_id: int, *, sleep=None) -> None:
    """Send the prompt again for every chat still awaiting a decision (the owner's /pending)."""
    for chat in await store.list_pending_chats():
        chat_id = int(chat["chat_id"])
        title = chat["title"] or str(chat_id)
        kind = "group" if chat_id < 0 else "chat"
        try:
            await _send_prompt(
                bot, owner_id, chat_id, title, kind, chat["invited_by_handle"] or "", chat.get("first_text"), sleep=sleep,
            )
        except Exception:
            log.exception("could not resend the approval prompt for chat %s", chat_id)
