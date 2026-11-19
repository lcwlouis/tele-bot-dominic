"""Approval gate: nothing reaches a model until the owner approves the chat."""

from __future__ import annotations

import logging
from typing import Literal

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.models import IncomingMessage
from dom.store import Store

log = logging.getLogger("dom.gate")

Decision = Literal["process", "hold", "drop"]


async def decide(store: Store, msg: IncomingMessage, owner_id: int | None) -> Decision:
    """process: approved. hold: pending, record only. drop: owner unset, ignored or blocked."""
    if owner_id is None:
        return "drop"
    status = (await store.get_chat_auth_status(msg.chat_id))["authorization_status"]
    if status == "approved":
        return "process"
    if status == "pending_approval":
        return "hold"
    return "drop"


async def _send_prompt(bot, owner_id: int, chat_id: int, title: str, kind: str, requested_by: str) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", callback_data=f"approve:{chat_id}"),
        InlineKeyboardButton(text="Ignore", callback_data=f"ignore:{chat_id}"),
    ]])
    text = f'{requested_by or "Someone"} wants Dom in the {kind} "{title}". Approve?'
    await bot.send_message(owner_id, text, reply_markup=keyboard)


async def request_approval(
    bot, store: Store, owner_id: int, chat_id: int, title: str, kind: str, requested_by: str
) -> None:
    """DM the owner Approve/Ignore buttons, once per pending chat."""
    if not await store.claim_approval_prompt(chat_id):
        return
    await store.note_pending_chat(str(chat_id), title, requested_by)
    try:
        await _send_prompt(bot, owner_id, chat_id, title, kind, requested_by)
    except (TelegramBadRequest, TelegramForbiddenError):  # definitely not delivered: allow a retry
        log.exception("the approval prompt for chat %s was rejected", chat_id)
        await store.release_approval_prompt(chat_id)
    except Exception:  # it may have arrived (timeout, network): stay reserved; the owner can use /pending
        log.exception("could not confirm delivery of the approval prompt for chat %s", chat_id)


async def resend_pending(bot, store: Store, owner_id: int) -> None:
    """Send the prompt again for every chat still awaiting a decision (the owner's /pending)."""
    for chat in await store.list_pending_chats():
        chat_id = int(chat["chat_id"])
        title = chat["title"] or str(chat_id)
        kind = "group" if chat_id < 0 else "chat"
        try:
            await _send_prompt(bot, owner_id, chat_id, title, kind, chat["invited_by_handle"] or "")
        except Exception:
            log.exception("could not resend the approval prompt for chat %s", chat_id)
