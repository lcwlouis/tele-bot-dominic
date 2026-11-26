"""Changes to Dom's life sheet (`memory/WORLD.md`): rare, reasoned, and never over the owner's own lines.

The nightly reflection may propose a change to the sheet in the same request that writes the journal (no extra model
call). A change applies only when it comes with a reason and the last one is at least a week old; the day and reason
are kept so the dashboard can show them. Lines the owner pinned by editing the sheet are core memories, which a model's
edit can never replace or remove, and lines the owner tagged to one chat are not the reflection's to touch.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from dom import notes
from dom.home import WORLD, Home, PrivateAccessError, limit_for

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.world_change")
KEY = "world:last_change"  # JSON: {"day": "YYYY-MM-DD", "reason": str}
INTERVAL = timedelta(days=7)
MAX_OPS = 4
MAX_REASON = 160
SOURCE = "world"  # the memory_write source: a write that is not from a chat, so it can't pin or touch another chat's line

FORMAT = (
    ' You may also include "world_edit": {"reason": "why Dom\'s life changes, in a sentence", "ops": [{"op": "add" | '
    '"replace" | "remove", "text": "the bullet to add, or the replacement", "old": "for replace and remove, the '
    'unique existing text", "entities": ["place:short-slug"]}]} to change <world>, Dom\'s own life sheet (home and work, '
    "routine by weekday and part of the day such as `- Thursday evening: usually gym`, regular spots, projects). "
    "It should change rarely and for a reason, such as a new job, a move or a new habit; most days leave it alone, and "
    "keep small things the way they are. Write what Dom does, not what the chats said: never another person's private "
    "matters, and no tags. List a choice of options after a colon (`Lunch: cai fan, laksa, kopi and toast`) so he can "
    "vary them. Lines ending in {core ...} are the owner's and stay as they are."
)


async def last_change(app: App) -> tuple[date, str] | None:
    """The day of the sheet's last change and its reason, or None while it has never changed."""
    raw = await app.store.kv_get(KEY)
    try:
        data = json.loads(raw) if raw else None
        return date.fromisoformat(data["day"]), str(data.get("reason", ""))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


async def due(app: App, today: date) -> bool:
    """Whether a change may apply today: the sheet has never changed, or its last change is a week old."""
    found = await last_change(app)
    return found is None or today - found[0] >= INTERVAL


def context(home: Home) -> str:
    """The sheet as the reflection reads it, in a `<world>` element: the lines no chat owns (so a line the owner tagged to
    one chat stays out), with their marks, so the model can see which lines are pinned."""
    try:
        text = home.scoped(allow_private=False).read(WORLD)
    except (OSError, PrivateAccessError, ValueError):
        text = ""
    shown = notes.limit_to_chat(text, None).strip()
    return f'<world limit="{limit_for(WORLD)}">\n{escape(shown)}\n</world>'


def _reason(value: object) -> str:
    return " ".join(value.split())[:MAX_REASON] if isinstance(value, str) else ""


async def apply(app: App, edit: object, today: date) -> bool:
    """Apply a proposed change if it is allowed. False (and nothing written) when it is malformed, has no reason, comes
    inside a week of the last change, or every op is refused. The gate is checked here, so it holds whoever calls."""
    if not isinstance(edit, dict) or not isinstance(edit.get("ops"), list) or not (reason := _reason(edit.get("reason"))):
        log.info("the proposed world change had no reason or no ops; nothing written")
        return False
    if not await due(app, today):
        log.info("the proposed world change came inside a week of the last one; nothing written")
        return False
    home = app.home.scoped(allow_private=False)
    applied = 0
    for op in edit["ops"][:MAX_OPS]:
        if not isinstance(op, dict) or op.get("op") not in ("add", "replace", "remove") or not isinstance(op.get("text", ""), str):
            continue
        old = op.get("old")
        try:
            result = home.memory_write(
                WORLD, op["op"], op.get("text", ""), old if isinstance(old, str) else None, source=SOURCE,
                entities=notes.normalize_entities(op.get("entities", ())), today=today,
            )
        except (PermissionError, ValueError, OSError) as error:
            log.info("a world sheet op was refused (%s)", type(error).__name__)
            continue
        if result.ok:
            applied += 1
        else:
            log.info("a world sheet op was refused: %s", result.error)
    if not applied:
        return False
    await app.store.kv_set(KEY, json.dumps({"day": today.isoformat(), "reason": reason}))
    try:
        await app.store.record_owner_event(f"Dom changed his world sheet: {reason}")
    except Exception:
        log.exception("could not record the world change for the owner")
    return True
