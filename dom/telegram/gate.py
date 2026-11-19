"""Approval gate: nothing reaches a model until the owner approves the chat."""

from __future__ import annotations

import logging
from typing import Literal

from aiogram.exceptions import TelegramAPIError
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


async def request_approval(
    bot, store: Store, owner_id: int, chat_id: int, title: str, kind: str, requested_by: str
) -> None:
    """DM the owner Approve/Ignore buttons, once per pending chat."""
    if not await store.claim_approval_prompt(chat_id):
        return
    await store.set_chat_auth_status(str(chat_id), "pending_approval", title=title, invited_by_handle=requested_by)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Approve", callback_data=f"approve:{chat_id}"),
        InlineKeyboardButton(text="Ignore", callback_data=f"ignore:{chat_id}"),
    ]])
    text = f'{requested_by or "Someone"} wants Dom in the {kind} "{title}". Approve?'
    try:
        await bot.send_message(owner_id, text, reply_markup=keyboard)
    except TelegramAPIError:
        log.exception("could not send the approval prompt for chat %s", chat_id)
        await store.set_chat_auth_status(str(chat_id), "pending_approval", approval_prompt_sent=False)
