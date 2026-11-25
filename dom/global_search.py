"""One search over everything the owner can open: chats (FTS5), memory files, briefs, reminders, desktop tasks, skills
and plugins. The dashboard's search box and Dom's `search` tool (owner DM only) both call it.

Privacy: the owner's DM shows in full. A message from any other chat is cut to a one-line preview here, so no caller
can pass more than the page shows. Dom's per-chat `search_history` never comes through this module.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from datetime import date, datetime, timezone

from dom import briefs, skills
from dom.home import Home, _split_journal
from dom.store import Store
from dom.store.store import recency_weight

KINDS = ("chat", "memory", "brief", "reminder", "task", "skill", "plugin")
PER_KIND = 8
PREVIEW = 120
SNIPPET = 160
MAX_FILE = 256 * 1024
MAX_QUERY = 200


@dataclass(frozen=True)
class Hit:
    kind: str
    title: str  # what the hit is: a chat's title, a file, a skill name
    text: str  # the matching line, with a little around it
    ref: str  # chat id, file path, filename, name or id: what the page opens
    at: str = ""
    full: bool = True  # False: a one-line preview of another chat's message


def one_line(text: str, limit: int) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _tokens(query: str) -> list[str]:
    return [token.lower() for token in query.split()[:8] if token]


def _matches(tokens: list[str], *fields: str) -> bool:
    haystack = " ".join(fields).lower()
    return bool(tokens) and all(token in haystack for token in tokens)


def snippet(text: str, tokens: list[str]) -> str:
    """The text around the first matching token, as one line."""
    flat = " ".join((text or "").split())
    lower = flat.lower()
    start = min((i for token in tokens if (i := lower.find(token)) >= 0), default=0)
    start = max(0, start - SNIPPET // 3)
    piece = flat[start:start + SNIPPET]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(flat) else "")


def _memory_paths(home: Path) -> list[str]:
    found = ["SOUL.md"]
    for folder in ("memory", "memory/people", "memory/chats", "private"):
        base = home / folder
        if base.is_dir() and not base.is_symlink():
            found.extend(f"{folder}/{p.name}" for p in sorted(base.glob("*.md")) if p.is_file() and not p.is_symlink())
    return [rel for rel in found if (home / rel).is_file()]


def _search_memory(home: Home, tokens: list[str]) -> list[Hit]:
    private = home.scoped(allow_private=True)
    hits = []
    for rel in _memory_paths(private.root):
        try:
            text = private.read_memory_file(rel)[:MAX_FILE]
        except (OSError, ValueError):
            continue
        if _matches(tokens, rel, text):
            hits.append(Hit("memory", rel, snippet(text, tokens), rel))
    return hits


def _search_archive(home: Home, tokens: list[str], keep_days: frozenset[str]) -> list[Hit]:
    """Journal archive hits, one per matching entry (and per summary block), newest first. Both archives are
    owner-only: this module is, and the private one needs the private scope. Entries from a day a core memory was
    pinned do not fade in the order."""
    today = datetime.now(timezone.utc).date()
    scored: list[tuple[float, str, Hit]] = []
    for private in (False, True):
        for rel in home.journal_archive_files(private):
            try:
                preamble, entries = _split_journal(home.read(rel)[:MAX_FILE])
            except (OSError, ValueError):
                continue
            if _matches(tokens, preamble):
                scored.append((2.0, rel, Hit("memory", rel, snippet(preamble, tokens), rel)))  # summaries first
            for entry in entries:
                if not _matches(tokens, entry):
                    continue
                day = entry.splitlines()[0][3:13]
                try:
                    weight = 1.0 if day in keep_days else recency_weight((today - date.fromisoformat(day)).days)
                except ValueError:
                    weight = 0.5
                scored.append((weight, day, Hit("memory", f"{rel} · {day}", snippet(entry, tokens), rel, day)))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _, _, hit in scored]


def _search_files(home: Home, tokens: list[str]) -> tuple[list[Hit], list[Hit], list[Hit]]:
    found_briefs = [
        Hit("brief", item.title, snippet(item.markdown, tokens), item.filename)
        for item in briefs.list_briefs(home) if _matches(tokens, item.markdown)
    ]
    found_skills = [
        Hit("skill", str(item["name"]), snippet(f"{item['description']} {item['body']}", tokens), str(item["name"]))
        for item in skills.skills_inventory(home) if _matches(tokens, str(item["name"]), str(item["description"]), str(item["body"]))
    ]
    keep_days = home.scoped(allow_private=True).core_days()
    return [*_search_memory(home, tokens), *_search_archive(home.scoped(allow_private=True), tokens, keep_days)], found_briefs, found_skills


async def search_all(store: Store, home: Home, query: str, chat_titles: dict[str, str] | None = None) -> dict[str, list[Hit]]:
    """Hits by kind (every kind present, maybe empty), at most PER_KIND each. Owner-only: callers check that first."""
    query = " ".join(query.split())[:MAX_QUERY]
    tokens = _tokens(query)
    result: dict[str, list[Hit]] = {kind: [] for kind in KINDS}
    owner = store.owner_id
    if not tokens or owner is None:
        return result
    titles = chat_titles or {}

    keep_days = await asyncio.to_thread(home.scoped(allow_private=True).core_days)
    for row in await store.search_chat_history(query, str(owner), PER_KIND, keep_days):
        chat_id = str(row["chat_id"])
        mine = chat_id == str(owner)
        text = row["text"] or ""
        result["chat"].append(Hit(
            "chat", "Your DM" if mine else titles.get(chat_id, chat_id),
            text if mine else one_line(text, PREVIEW), chat_id, row["created_at"], full=mine,
        ))

    memory, found_briefs, found_skills = await asyncio.to_thread(_search_files, home, tokens)
    result["memory"], result["brief"], result["skill"] = memory, found_briefs, found_skills

    result["reminder"] = [
        Hit("reminder", one_line(row["text"], 80), row["text"], str(row["id"]), row["due_at"])
        for row in await store.list_reminders(owner, limit=200) if _matches(tokens, row["text"])
    ]
    result["task"] = [
        Hit("task", one_line(row["brief"], 80), snippet(f"{row['brief']} {row.get('result') or ''}", tokens), str(row["id"]), row["created_at"])
        for row in await store.desktop_tasks(owner, limit=200) if _matches(tokens, row["brief"], row.get("result") or "")
    ]
    for row in await store.list_plugins():
        description = ""
        try:
            description = str(json.loads(row["manifest_json"]).get("description") or "")
        except (ValueError, AttributeError):
            pass
        if _matches(tokens, row["name"], description):
            result["plugin"].append(Hit("plugin", row["name"], one_line(description, SNIPPET), row["name"]))

    return {kind: hits[:PER_KIND] for kind, hits in result.items()}


LABELS = {
    "chat": "Chats", "memory": "Memory", "brief": "Briefs", "reminder": "Reminders", "task": "Desktop tasks",
    "skill": "Skills", "plugin": "Plugins",
}
