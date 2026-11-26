"""Changes to Dom's life sheet (`memory/WORLD.md`): rare, reasoned, and never over the owner's own lines.

The nightly reflection may propose a change to the sheet in the same request that writes the journal (no extra model
call). A change applies only when it comes with a reason and the last one is at least a week old; the day and reason
are kept so the dashboard can show them. Lines the owner pinned by editing the sheet are core memories, which a model's
edit can never replace or remove, and lines the owner tagged to one chat are not the reflection's to touch.
"""

from __future__ import annotations

import asyncio
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
MARKER = "memory/WORLD.change.json"  # JSON: {"day": "YYYY-MM-DD", "reason": str}, kept in the home beside the sheet
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


def _marker(home: Home) -> tuple[date, str] | None:
    """The last change's day and reason; None only when the sheet has never changed (there is no marker file). A
    marker that can't be read counts as a change on the day the file was last written, so a damaged file never
    reopens the week."""
    path = home.root / MARKER
    try:
        raw = home.scoped(allow_private=False).read(MARKER)
    except (OSError, ValueError, PrivateAccessError):
        raw = ""
    if not raw and not path.is_file():
        return None
    try:
        data = json.loads(raw)
        return date.fromisoformat(data["day"]), str(data.get("reason", ""))
    except (ValueError, TypeError, KeyError, AttributeError):
        log.warning("the world change marker could not be read; counting it as a change on the day it was written")
    try:
        return date.fromtimestamp(path.stat().st_mtime), ""
    except OSError:
        return date.today(), ""


def _is_due(found: tuple[date, str] | None, today: date) -> bool:
    return found is None or today - found[0] >= INTERVAL


async def last_change(app: App) -> tuple[date, str] | None:
    """The day of the sheet's last change and its reason, or None while it has never changed."""
    return await asyncio.to_thread(_marker, app.home)


async def due(app: App, today: date) -> bool:
    """Whether a change may apply today: the sheet has never changed, or its last change is a week old."""
    return _is_due(await last_change(app), today)


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


def _write(home: Home, edit: dict, reason: str, today: date) -> bool:
    """Check the week, claim it, then write the sheet. All of it runs without yielding to the event loop, so two
    proposals can't both pass the check. The week is claimed first (a file moved into place, kept beside the sheet):
    a crash or a failed write after that leaves the week spent with at most part of the change made, never a sheet
    that changed while the week stayed open. Only when no op was applied is the old marker put back."""
    before = _marker(home)
    if not _is_due(before, today):
        log.info("the proposed world change came inside a week of the last one; nothing written")
        return False
    home.write_file(MARKER, json.dumps({"day": today.isoformat(), "reason": reason}))
    scoped = home.scoped(allow_private=False)
    applied = 0
    for op in edit["ops"][:MAX_OPS]:
        if not isinstance(op, dict) or op.get("op") not in ("add", "replace", "remove") or not isinstance(op.get("text", ""), str):
            continue
        old = op.get("old")
        try:
            result = scoped.memory_write(
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
    if applied:
        return True
    if before is None:
        (home.root / MARKER).unlink(missing_ok=True)
    else:
        home.write_file(MARKER, json.dumps({"day": before[0].isoformat(), "reason": before[1]}))
    return False


async def apply(app: App, edit: object, today: date) -> bool:
    """Apply a proposed change if it is allowed. False (and nothing written) when it is malformed, has no reason, comes
    inside a week of the last change, or every op is refused. The gate is checked here, so it holds whoever calls."""
    if not isinstance(edit, dict) or not isinstance(edit.get("ops"), list) or not (reason := _reason(edit.get("reason"))):
        log.info("the proposed world change had no reason or no ops; nothing written")
        return False
    if not _write(app.home, edit, reason, today):
        return False
    try:
        await app.store.record_owner_event(f"Dom changed his world sheet: {reason}")
    except Exception:
        log.exception("could not record the world change for the owner")
    return True
