"""The chat window: it grows until the prompt reaches a token budget, then one trim drops the oldest messages.

The model host caches prompt prefixes, so a window that slides by one message per turn forces everything after the
cached system prefix to be recomputed every turn. Here the window's start is a stored message that only moves at a
trim, and a trim happens only when the whole prompt has reached `window_budget_tokens`; it then drops the oldest
messages until about `window_keep_tokens` are left. The start is kept per chat in the kv store (the first kept
message's row id and time), so it survives restarts and does not move when messages are edited or deleted.

How big the prompt is comes from the model itself: after each turn the first chat call's reported `input_tokens`
minus the estimated size of the window it carried is the prompt's fixed part (system prefix, tools, notes), kept
with the start. The window's current size is that plus an estimate for every message now in it. Messages are
estimated at one token per three UTF-8 bytes (plain text over-counts a little, CJK and emoji come out right).

A trim never drops a message the memory pass has not read yet: it waits (`Window.blocked`) while the caller starts
the pass, so the pass rewrites the notes and the trim moves the window start in the same turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dom.models import StoredMessage

if TYPE_CHECKING:
    from dom.config import Config
    from dom.store import Store

log = logging.getLogger("dom.window")
MIN_KEPT = 4  # a trim never leaves fewer messages than this, however large they are
DEFAULT_FIXED = 6000  # tokens beyond the window until a real prompt has been measured
FRAMING_BYTES = 100  # the <msg ...> markup around one message
FETCH_FACTOR = 2  # rows read per allowed message, so a window far past the cap is still seen as too long


def estimate_tokens(message: StoredMessage) -> int:
    size = FRAMING_BYTES + sum(
        len(part.encode()) for part in (message.text, message.media_json, message.sender_name, message.sender_handle) if part
    )
    return -(-size // 3)


def _key(chat_id: int, thread_id: int | None = None) -> str:
    return f"window:{chat_id}" if thread_id is None else f"window:{chat_id}:{thread_id}"


@dataclass(frozen=True)
class Window:
    rows: list[StoredMessage]
    tokens: int  # estimated prompt size with these rows
    blocked: bool = False  # a trim is due but would drop messages the memory pass has not read
    trim_seq: int = 0  # increments on every trim so the prompt cache knows when to swap notes


async def _state(store: Store, chat_id: int, thread_id: int | None = None) -> tuple[tuple[int, str] | None, int, int]:
    try:
        raw = json.loads(await store.kv_get(_key(chat_id, thread_id)) or "{}")
        start = (int(raw["id"]), str(raw["at"])) if "id" in raw else None
        return start, max(0, int(raw.get("fixed", DEFAULT_FIXED))), max(0, int(raw.get("trims", 0)))
    except (ValueError, TypeError, AttributeError, KeyError):
        return None, DEFAULT_FIXED, 0


async def _save(
    store: Store, chat_id: int, start: tuple[int, str] | None, fixed: int, trims: int = 0,
    thread_id: int | None = None,
) -> None:
    state: dict[str, int | str] = {"fixed": fixed} if start is None else {"id": start[0], "at": start[1], "fixed": fixed}
    if trims:
        state["trims"] = trims
    await store.kv_set(_key(chat_id, thread_id), json.dumps(state))


def _drop_count(rows: list[StoredMessage], tokens: int, config: Config) -> int:
    """How many of the oldest messages bring the window down to the watermark (and to 60% of the message cap)."""
    excess, keep_rows, dropped = tokens - config.window_keep_tokens, config.window_size * 3 // 5, 0
    for message in rows[: max(0, len(rows) - MIN_KEPT)]:
        if excess <= 0 and len(rows) - dropped <= keep_rows:
            break
        excess -= estimate_tokens(message)
        dropped += 1
    return dropped


async def load(
    store: Store, chat_id: int, config: Config, *, through_id: int | None = None, thread_id: int | None = None,
) -> Window:
    """The messages the model reads for this chat (or forum topic), trimming first if the prompt has reached its budget."""
    start, fixed, trims = await _state(store, chat_id, thread_id)
    limit = config.window_size * (FETCH_FACTOR if start else 1)  # with no start yet, begin from the newest `window_size`
    rows = await store.get_window_from(chat_id, start, limit, through_id=through_id, thread_id=thread_id, filter_thread=True)
    tokens = fixed + sum(estimate_tokens(message) for message in rows)
    if tokens < config.window_budget_tokens and len(rows) <= config.window_size:
        return Window(rows, tokens, trim_seq=trims)
    dropped = _drop_count(rows, tokens, config)
    if not dropped:
        return Window(rows, tokens, trim_seq=trims)
    cursor = await store.get_memory_cursor(chat_id)
    if any(m.role != "event" and m.msg_id is not None and m.msg_id > cursor for m in rows[:dropped]):
        return Window(rows, tokens, blocked=True, trim_seq=trims)
    first = rows[dropped]
    trims += 1
    await _save(store, chat_id, (first.id, first.created_at), fixed, trims, thread_id)
    log.info("trimmed chat %s: dropped %d messages (about %d tokens)", chat_id, dropped, tokens)
    return Window(rows[dropped:], tokens - sum(estimate_tokens(m) for m in rows[:dropped]), trim_seq=trims)


async def measure(
    store: Store, chat_id: int, sent: list[StoredMessage], prompt_tokens: int, *, thread_id: int | None = None,
) -> None:
    """Remember how much of a real prompt was not the window: its reported size minus the window's estimate."""
    if prompt_tokens <= 0:
        return
    start, _, trims = await _state(store, chat_id, thread_id)
    if start is None and sent:  # the first turn of a chat: from here the window starts where this one did
        start = (sent[0].id, sent[0].created_at)
    await _save(store, chat_id, start, max(0, prompt_tokens - sum(estimate_tokens(m) for m in sent)), trims, thread_id)
