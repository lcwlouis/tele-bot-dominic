"""Chat reachability checks (E74): verify current chats against Telegram without deleting history."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.reachability")

CHECK_GAP_SECONDS = 0.25
MAX_RETRY_AFTER = 30.0
_UNREACHABLE_PHRASES = (
    "chat not found",
    "user not found",
    "user is deactivated",
    "bot was kicked",
    "bot is not a member",
    "member not found",
    "group chat was deactivated",
    "chat_write_forbidden",
    "channel_private",
    "upgraded to a supergroup",
)
_LOCKS: dict[int, asyncio.Lock] = {}


def _lock_for(app: App) -> asyncio.Lock:
    lock = _LOCKS.get(id(app))
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[id(app)] = lock
    return lock


def _is_group(chat: dict) -> bool:
    chat_type = chat.get("chat_type") or ""
    if chat_type in ("group", "supergroup"):
        return True
    try:
        return int(chat["chat_id"]) < 0
    except (TypeError, ValueError):
        return False


def _unreachable_reason(exc: Exception) -> str | None:
    """Return a short reason string if `exc` means the current bot can no longer reach the chat, else None."""
    if isinstance(exc, TelegramNotFound):
        return "chat not found"
    if isinstance(exc, TelegramForbiddenError):
        detail = str(exc).lower()
        if "blocked" in detail:
            return "blocked by user"
        if "kicked" in detail:
            return "kicked from chat"
        if "deactivated" in detail:
            return "chat deactivated"
        return "forbidden"
    if isinstance(exc, TelegramBadRequest):
        detail = str(exc).lower()
        for phrase in _UNREACHABLE_PHRASES:
            if phrase in detail:
                return phrase
    return None


async def _probe_once(app: App, chat: dict) -> tuple[bool | None, str]:
    """Check one chat against Telegram. Returns (True, '') when reachable, (False, reason) when unreachable,
    or (None, '') on a transient error (leave current state unchanged)."""
    chat_id = int(chat["chat_id"])
    group = _is_group(chat)
    for attempt in range(2):
        try:
            if group and app.bot_id is not None:
                member = await app.bot.get_chat_member(chat_id, app.bot_id)
                status = getattr(member, "status", "")
                if status in ("left", "kicked"):
                    return False, status
                return True, ""
            await app.bot.get_chat(chat_id)
            return True, ""
        except TelegramRetryAfter as exc:
            if attempt == 0 and exc.retry_after <= MAX_RETRY_AFTER:
                await app.sleep(exc.retry_after)
                continue
            log.warning("reachability check rate-limited for chat %s (retry_after=%s)", chat_id, exc.retry_after)
            return None, ""
        except (TelegramForbiddenError, TelegramBadRequest, TelegramNotFound) as exc:
            reason = _unreachable_reason(exc)
            if reason is not None:
                return False, reason
            log.warning("reachability check got unexpected Telegram error for chat %s: %s", chat_id, exc)
            return None, ""
        except Exception as exc:
            log.warning("reachability check failed transiently for chat %s (%s)", chat_id, type(exc).__name__)
            return None, ""
    return None, ""


async def check_reachability(app: App, *, gap: float = CHECK_GAP_SECONDS) -> dict[str, int]:
    """Check every non-hidden current chat (approved, pending_approval, ignored), spread out under Telegram rate limits.
    Unreachable chats keep their prior authorization_status and history."""
    async with _lock_for(app):
        targets = await app.store.list_reachability_targets()
        checked = unreachable = restored = errors = 0
        for index, chat in enumerate(targets):
            if index and gap > 0:
                await app.sleep(gap)
            ok, reason = await _probe_once(app, chat)
            if ok is None:
                errors += 1
                continue
            checked += 1
            if ok:
                if not chat["reachable"]:
                    await app.store.set_chat_reachable(chat["chat_id"], True)
                    restored += 1
            else:
                await app.store.set_chat_reachable(chat["chat_id"], False, reason)
                unreachable += 1
        return {"checked": checked, "unreachable": unreachable, "restored": restored, "errors": errors}
