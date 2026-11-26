"""The instance home (DOM_HOME): layout, bounded memory files, the private/ boundary."""

from __future__ import annotations

import json
import re
import shutil
from hashlib import sha256
from tempfile import NamedTemporaryFile
from threading import Lock
from importlib import resources
from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, time
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from dom import notes

# Character limits, keyed by path pattern relative to the home.
LIMITS: dict[str, int] = {
    "memory/MEMORY.md": 2200,
    "memory/HABITS.md": 1000,
    "memory/people/*.md": 1000,
    "memory/chats/*.md": 1000,
    "memory/places/*.md": 1400,
    "memory/projects/*.md": 1400,
    "memory/goals/*.md": 1400,
    "private/USER.md": 1375,
    "private/NOTES.md": 2200,
}
MAX_SOUL = 6000  # characters in SOUL.md after an approved edit
MAX_SOUL_EDIT = 2000  # characters in the old or the new text of one soul edit
# Writable through memory_write but not size-bounded (never loaded into chat prompts).
UNBOUNDED = {"memory/JOURNAL.md", "private/JOURNAL.md"}
JOURNAL_LIVE = 30  # entries kept in JOURNAL.md; older ones move to the monthly archive
ARCHIVE_DIRS = {False: "memory/journal", True: "private/journal"}
_ARCHIVE_FILE = re.compile(r"^(?:memory|private)/journal/(?:\d{4}-\d{2}|\d{4}|RECENT)\.md$")
# Note files whose lines carry source tags (a source chat, or `any`).
_TAGGED = ("memory/people/*.md", "memory/chats/*.md", "memory/places/*.md", "memory/projects/*.md", "memory/goals/*.md")
# Note files the owner's hand edits pin.
_NOTE_FILES = (
    "memory/MEMORY.md", "memory/people/*.md", "memory/chats/*.md", "memory/places/*.md",
    "memory/projects/*.md", "memory/goals/*.md", "private/USER.md", "private/NOTES.md",
)
_MEMORY_WRITE_LOCK = Lock()
_JOURNAL_HEADING = re.compile(r"^## \d{4}-\d{2}-\d{2}[ \t\r]*$", re.MULTILINE)



class PrivateAccessError(PermissionError):
    """Raised when private/ is touched through a Home that was not opened for it."""


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    content: str
    error: str | None = None
    over_limit: bool = False


def limit_for(rel: str) -> int | None:
    if notes.is_former(rel):
        return None  # a former file is the record of what changed: written by code, never by a model's edit
    for pattern, limit in LIMITS.items():
        if fnmatchcase(rel, pattern):
            return limit
    return None


def content_version(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


class Home:
    def __init__(self, root: Path, allow_private: bool = False):
        self.root = Path(root)
        self.allow_private = allow_private

    def scoped(self, allow_private: bool) -> Home:
        return Home(self.root, allow_private)

    def _resolve(self, rel: str) -> Path:
        pure = PurePosixPath(rel)
        if not rel or pure.is_absolute() or ".." in pure.parts or "\\" in rel:
            raise ValueError(f"invalid path: {rel!r}")
        # Judge the real location: a symlink must not smuggle private/ content (or anything
        # outside the home) through a public-looking name.
        root = self.root.resolve()
        real = root.joinpath(*pure.parts).resolve()
        try:
            inside = real.relative_to(root)
        except ValueError:
            raise ValueError(f"{rel} points outside the home") from None
        if (pure.parts[0] == "private" or inside.parts[:1] == ("private",)) and not self.allow_private:
            raise PrivateAccessError(f"{rel} is not available in this scope")
        return real

    def read(self, rel: str) -> str:
        path = self._resolve(rel)
        return path.read_text() if path.is_file() else ""

    def memory_write(
        self,
        rel: str,
        op: Literal["add", "replace", "remove"],
        text: str,
        old: str | None = None,
        *,
        source: str | None = None,
        entities: tuple[str, ...] = (),
        today: date | None = None,
    ) -> WriteResult:
        """`source` marks a write that came from the memory pass: the chat the facts came from. Such a write cannot
        pin a line (core marks in its text are dropped) and its lines get that chat's source tag unless it says `{any}`."""
        # Proposal decisions run in the store worker. Serialize their edits with memory passes.
        with _MEMORY_WRITE_LOCK:
            return self._memory_write(rel, op, text, old, source, entities, today or date.today())

    def _memory_write(
        self,
        rel: str,
        op: Literal["add", "replace", "remove"],
        text: str,
        old: str | None,
        source: str | None,
        entities: tuple[str, ...],
        today: date,
    ) -> WriteResult:
        try:
            path = self._resolve(rel)
        except ValueError as exc:  # traversal; PrivateAccessError still propagates
            return WriteResult(False, "", str(exc))
        limit = limit_for(rel)
        if limit is None and rel not in UNBOUNDED:
            return WriteResult(False, "", f"{rel} is not a writable memory file")
        if path != self.root.resolve().joinpath(*PurePosixPath(rel).parts):
            return WriteResult(False, "", f"{rel} is a link; memory files must be regular files")
        current = path.read_text() if path.is_file() else ""
        former: list[tuple[str, str]] = []

        if op == "add":
            line = self._shaped(rel, f"- {text}", source, entities)
            if not text.strip() or any(notes.blank_body(part) for part in line.splitlines()):
                return WriteResult(False, current, "add needs `text`")
            new = f"{current.rstrip(chr(10))}\n{line}" if current.strip() else line
        elif op in ("replace", "remove"):
            if not old or not old.strip():
                return WriteResult(False, current, f"{op} needs `old`")
            count = current.count(old)
            if count != 1:
                return WriteResult(False, current, f"`old` must match exactly once, found {count}")
            start = current.index(old)
            end = start + len(old.rstrip("\n"))  # a trailing line break in `old` belongs to no line's text
            lines = current.split("\n")
            first, last = current.count("\n", 0, start), current.count("\n", 0, end)
            touched = lines[first:last + 1]
            if any(notes.parse_line(line).core is not None for line in touched):
                return WriteResult(False, current, "that line is a core memory (pinned); leave it as it is")
            if source is not None and any(_foreign(line, source) for line in touched):
                return WriteResult(False, current, "that line belongs to another chat's notes")
            if op == "replace":
                line_start = current.rfind("\n", 0, start) + 1
                line_end = current.find("\n", end)
                before, after = current[line_start:start], current[end:len(current) if line_end == -1 else line_end]
                if source is not None:  # the new line's tag comes from `text` alone, never from what the old line carried
                    before, after = notes.strip_meta(before), notes.strip_meta(after)
                replaced = [self._shaped(rel, line, source, entities) for line in (before + text + after).split("\n")]
                if any(line.strip() and notes.blank_body(line) for line in replaced):
                    return WriteResult(False, current, "replace needs `text`")
                lines[first:last + 1] = replaced
                if replaced != touched:
                    former = [(line, "replaced") for line in touched if line.strip()]
            else:
                del lines[first:last + 1]
                former = [(line, "removed") for line in touched if line.strip()]
            new = "\n".join(lines)
        else:
            return WriteResult(False, current, f"unknown op {op!r}")

        return self._save_memory_text(rel, current, new, limit, former, today)

    @staticmethod
    def _shaped(rel: str, text: str, source: str | None, entities: tuple[str, ...] = ()) -> str:
        """Text the memory pass wrote, as the file keeps it: no core marks, and no source tag except in the people and
        chat notes, where each line is tagged with the chat it came from (or `{any}` when the pass says it is general)."""
        if source is None:
            return text
        tagged = any(fnmatchcase(rel, pattern) for pattern in _TAGGED)
        out = []
        for raw in text.split("\n"):
            if not raw.strip():
                out.append(raw)
                continue
            line = notes.parse_line(raw)
            tag = (notes.ANY if line.source == notes.ANY else source) if tagged else None
            refs = notes.normalize_entities((*line.entities, *entities))
            out.append(notes.format_line(notes.Line(line.body, tag, entities=refs)))
        return "\n".join(out)

    def _save_memory_text(
        self, rel: str, current: str, new: str, limit: int | None,
        former: list[tuple[str, str]] = (), today: date | None = None,
    ) -> WriteResult:
        if rel == "memory/MEMORY.md" and notes.chat_tagged(new):
            return WriteResult(False, current, "MEMORY.md is read in every chat, so its lines can't be tagged to one chat")
        # Shrinking is always allowed so a file that is already over its limit can be pruned. Tags are free (D18).
        size, before = notes.visible_len(new), notes.visible_len(current)
        if limit is not None and size > limit and size >= before:
            return WriteResult(
                False,
                current,
                f"over limit: {size} > {limit} characters; merge or remove entries first",
                over_limit=True,
            )
        if former:
            self._append_former(rel, former, today or date.today())
        self._write_regular(rel, new)
        return WriteResult(True, new)

    def _append_former(self, rel: str, items: list[tuple[str, str]], today: date) -> None:
        """Superseded lines go to `<file>.former.md`, newest last, so nothing in a note vanishes silently."""
        former_rel = notes.former_rel(rel)
        path = self._regular_path(former_rel)
        text = path.read_text() if path.is_file() else ""
        added = [notes.former_line(line, today, reason) for line, reason in items]
        self._write_regular(former_rel, "\n".join(filter(None, [text.rstrip("\n"), *added])) + "\n")

    def save_memory(self, rel: str, text: str, expected: str, *, pin: bool = True, today: date | None = None) -> WriteResult:
        """Save an owner's full-file edit if it is based on the current text. With `pin`, a note line the owner
        added or changed becomes a core memory (D18)."""
        with _MEMORY_WRITE_LOCK:
            if rel == "memory/HABITS.md":
                return WriteResult(False, "", "HABITS.md is read-only.")
            limit = MAX_SOUL if rel == "SOUL.md" else limit_for(rel)
            if limit is None and rel not in UNBOUNDED:
                return WriteResult(False, "", f"{rel} is not a writable memory file")
            path = self._regular_path(rel)
            current = path.read_text() if path.is_file() else ""
            if content_version(current) != expected:
                return WriteResult(False, current, "This file changed since you opened it. Discard to reload it.")
            if rel == "SOUL.md" and len(text) > MAX_SOUL:
                return WriteResult(False, current, f"over limit: {len(text)} > {MAX_SOUL} characters", over_limit=True)
            if pin and any(fnmatchcase(rel, pattern) for pattern in _NOTE_FILES):
                text = _pin_hand_edits(current, text, today or date.today())
            return self._save_memory_text(rel, current, text, limit)

    def _note_rels(self) -> list[str]:
        """Every note file this Home can read, MEMORY.md first."""
        people, chats = self.list_memory_notes()
        rels = ["memory/MEMORY.md", *people, *chats, *self.list_entity_notes()]
        return rels + (["private/USER.md", "private/NOTES.md"] if self.allow_private else [])

    def core_memories(self) -> list[dict[str, str]]:
        """All core memories in the notes this Home can read, one entry per line."""
        found = []
        for rel in self._note_rels():
            for raw in self.read(rel).split("\n"):
                line = notes.parse_line(raw)
                if (core := line.core) is None:
                    continue
                found.append({
                    "file": rel, "text": line.body, "source": line.source or notes.ANY,
                    "by": core.by, "day": core.day, "why": core.why,
                    "raw": raw,  # the exact line, so the dashboard changes this line and not a twin with the same text
                })
        return found

    def pin_line(self, rel: str, old: str, why: str, cap: int, today: date, source: str | None = None) -> WriteResult:
        """Dom marks one existing line a core memory. At `cap` core memories (the owner's count too) he must unpin first."""
        with _MEMORY_WRITE_LOCK:
            found = self._unique_line(rel, old)
            if isinstance(found, WriteResult):
                return found
            current, lines, index = found
            line = notes.parse_line(lines[index])
            if source is not None and _foreign(lines[index], source):
                return WriteResult(False, current, "that line belongs to another chat's notes")
            if line.core is not None:
                return WriteResult(False, current, "that line is already a core memory")
            if len(self.scoped(True).core_memories()) >= cap:
                return WriteResult(False, current, f"core memories are full ({cap}); unpin one of your own first")
            lines[index] = notes.format_line(notes.Line(line.body, line.source, notes.Core("dom", today.isoformat(), why), line.entities))
            return self._save_memory_text(rel, current, "\n".join(lines), limit_for(rel))

    def unpin_line(self, rel: str, old: str, source: str | None = None) -> WriteResult:
        """Dom lets go of one of his own core memories. The owner's pins are not his to remove."""
        with _MEMORY_WRITE_LOCK:
            found = self._unique_line(rel, old)
            if isinstance(found, WriteResult):
                return found
            current, lines, index = found
            line = notes.parse_line(lines[index])
            if source is not None and _foreign(lines[index], source):
                return WriteResult(False, current, "that line belongs to another chat's notes")
            if line.core is None:
                return WriteResult(False, current, "that line is not a core memory")
            if line.core.by != "dom":
                return WriteResult(False, current, "the owner pinned that line; only the owner can unpin it")
            lines[index] = notes.format_line(notes.Line(line.body, line.source, entities=line.entities))
            return self._save_memory_text(rel, current, "\n".join(lines), limit_for(rel))

    def _unique_line(self, rel: str, old: str) -> tuple[str, list[str], int] | WriteResult:
        try:
            path = self._regular_path(rel)
        except ValueError as exc:
            return WriteResult(False, "", str(exc))
        if notes.is_former(rel) or not any(fnmatchcase(rel, pattern) for pattern in _NOTE_FILES):
            return WriteResult(False, "", f"{rel} cannot hold core memories")
        current = path.read_text() if path.is_file() else ""
        if not old or not old.strip() or current.count(old) != 1:
            return WriteResult(False, current, f"`old` must match exactly once, found {current.count(old) if old and old.strip() else 0}")
        lines = current.split("\n")
        return current, lines, current.count("\n", 0, current.index(old))

    def sweep_expired(self, today: date) -> int:
        """Move lines whose `(until DATE)` has passed to the former file. Core memories never expire."""
        moved = 0
        with _MEMORY_WRITE_LOCK:
            for rel in self._note_rels():
                try:
                    path = self._regular_path(rel)
                except ValueError:
                    continue
                current = path.read_text() if path.is_file() else ""
                lines = current.split("\n")
                gone = [line for line in lines if notes.expired_until(line, today) and notes.parse_line(line).core is None]
                if not gone:
                    continue
                kept = [line for line in lines if line not in gone]
                self._append_former(rel, [(line, "expired") for line in gone], today)
                self._write_regular(rel, "\n".join(kept))
                moved += len(gone)
        return moved

    def journal_archive_files(self, private: bool) -> list[str]:
        """The month and year files of one journal's archive, oldest first (not RECENT.md)."""
        try:
            folder = self._resolve(ARCHIVE_DIRS[private])
        except ValueError:
            return []
        if not folder.is_dir() or folder.is_symlink():
            return []
        names = sorted(p.name for p in folder.glob("*.md") if p.is_file() and not p.is_symlink() and p.stem[:1].isdigit())
        return [f"{ARCHIVE_DIRS[private]}/{name}" for name in names]

    def core_days(self) -> frozenset[str]:
        """The days core memories were pinned: search never fades what happened then."""
        return frozenset(item["day"] for item in self.core_memories())

    def update_archive(self, rel: str, change: Callable[[str], str]) -> None:
        """Rewrite one journal archive file (`memory|private/journal/YYYY-MM.md` or `YYYY.md`) under the memory lock."""
        if not _ARCHIVE_FILE.match(rel):
            raise ValueError(f"{rel} is not a journal archive file")
        with _MEMORY_WRITE_LOCK:
            current = self.read(rel)
            updated = change(current)
            if updated != current:
                self._write_regular(rel, updated)

    def private_text_files(self) -> list[str]:
        """Every file holding the owner's private text: the private notes, the live journal and its archive
        (month and year files, summaries included). Guards compare public text against all of them."""
        if not self.allow_private:
            raise PrivateAccessError("private files need a private-scoped home")
        base = self.root / ARCHIVE_DIRS[True]
        archive = sorted(
            f"{ARCHIVE_DIRS[True]}/{p.name}" for p in base.glob("*.md")
            if p.is_file() and not p.is_symlink() and not p.name.endswith(".former.md")
        ) if base.is_dir() and not base.is_symlink() else []
        return ["private/USER.md", "private/NOTES.md", "private/JOURNAL.md", *archive]

    def list_memory_notes(self) -> tuple[list[str], list[str]]:
        """List regular per-person and per-chat Markdown notes in the public Home scope."""
        home = self.scoped(allow_private=False)

        def names(directory: str) -> list[str]:
            try:
                folder = home._regular_path(directory)
                return sorted(f"{directory}/{p.name}" for p in folder.glob("*.md") if not p.is_symlink() and p.is_file() and not notes.is_former(p.name))
            except (OSError, ValueError):
                return []

        return names("memory/people"), names("memory/chats")

    def former_files(self) -> list[str]:
        """Every `.former.md` file beside the notes, private ones too when this Home is private-scoped."""
        found = []
        folders = ("memory", "memory/people", "memory/chats", "memory/places", "memory/projects", "memory/goals")
        for folder in (*folders, *(("private",) if self.allow_private else ())):
            try:
                base = self._regular_path(folder)
            except ValueError:
                continue
            if base.is_dir():
                found.extend(
                    f"{folder}/{p.name}" for p in sorted(base.glob("*.former.md")) if p.is_file() and not p.is_symlink()
                )
        return found

    def list_entity_notes(self) -> list[str]:
        """Place, project and goal notes, excluding their read-only former siblings."""
        home = self.scoped(allow_private=False)
        found = []
        for directory in ("memory/places", "memory/projects", "memory/goals"):
            try:
                folder = home._regular_path(directory)
                found.extend(
                    f"{directory}/{p.name}" for p in sorted(folder.glob("*.md"))
                    if p.is_file() and not p.is_symlink() and not notes.is_former(p.name)
                )
            except (OSError, ValueError):
                continue
        return found

    def read_memory_file(self, rel: str) -> str:
        path = self._regular_path(rel)
        return path.read_text() if path.is_file() else ""

    def move_chat_notes(self, old_chat_id: int | str, new_chat_id: int | str) -> bool:
        """Copy a migrated group's chat notes file to the new chat id when the destination is empty.
        Leaves the old file intact so any in-flight pass on the old id never loses its file."""
        old_id, new_id = str(old_chat_id), str(new_chat_id)
        if old_id == new_id:
            return False
        moved = False
        with _MEMORY_WRITE_LOCK:
            home = self.scoped(allow_private=False)
            old_path = home._regular_path(f"memory/chats/{old_id}.md")
            new_path = home._regular_path(f"memory/chats/{new_id}.md")
            if old_path.is_file() and not (new_path.exists() and new_path.read_text(encoding="utf-8").strip()):
                home._write_regular(f"memory/chats/{new_id}.md", notes.retag(old_path.read_text(encoding="utf-8"), old_id, new_id))
                moved = True
            # Lines other notes tagged to the old chat id now belong to the new one, whether or not it had chat notes.
            for rel in home.list_memory_notes()[0]:
                text = home.read_memory_file(rel)
                if (retagged := notes.retag(text, old_id, new_id)) != text:
                    home._write_regular(rel, retagged)
                    moved = True
        return moved

    def _regular_path(self, rel: str) -> Path:
        path = self._resolve(rel)
        if path != self.root.resolve().joinpath(*PurePosixPath(rel).parts):
            raise ValueError(f"{rel} is a link; expected a regular file")
        return path

    def _write_regular(self, rel: str, content: str) -> None:
        path = self._regular_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
            pending = Path(temp.name)
            try:
                temp.write(content)
                temp.close()
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)

    def write_sticker_sets(self, names: list[str]) -> None:
        self.scoped(allow_private=False)._write_regular("stickers.toml", f"sets = {json.dumps(names)}\n")

    def write_sticker_descriptions(self, descriptions: dict[str, str]) -> None:
        content = json.dumps(descriptions, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        self.scoped(allow_private=False)._write_regular("sticker_descriptions.json", content)

    def edited_soul(self, old: str, new: str) -> str | None:
        """SOUL.md with `old` replaced by `new`, or None unless `old` occurs in it exactly once."""
        current = self.scoped(allow_private=False).read("SOUL.md")
        first = current.find(old) if old else -1
        if first == -1 or current.find(old, first + 1) != -1:
            return None
        return current[:first] + new + current[first + len(old):]

    def replace_soul(self, old: str, new: str) -> None:
        with _MEMORY_WRITE_LOCK:
            edited = self.edited_soul(old, new)
            if edited is None:
                raise ValueError("SOUL.md must contain the old text exactly once")
            if len(edited) > MAX_SOUL:
                raise ValueError(f"SOUL.md cannot exceed {MAX_SOUL} characters")
            self.scoped(allow_private=False)._write_regular("SOUL.md", edited)

    def journal_entries(self, *, private: bool = False) -> list[str]:
        return _split_journal(self.read(_journal_path(private)))[1]

    def append_journal(self, day: date, body: str, *, private: bool = False) -> None:
        """Add a day's entry. The live file keeps the last JOURNAL_LIVE entries; older ones move, oldest first and
        one at a time, to the monthly archive. Each move writes the archive before it trims the live file, so an
        interruption leaves the entry in one place or both, never neither, and a later call finishes the move."""
        with _MEMORY_WRITE_LOCK:
            rel = _journal_path(private)
            heading = f"## {day.isoformat()}"
            preamble, entries = _split_journal(self.read(rel))
            entries = [entry for entry in entries if entry.splitlines()[0].rstrip() != heading]
            entries.append(f"{heading}\n{body}")
            self._write_regular(rel, _join_journal(preamble, entries))
            while len(entries) > JOURNAL_LIVE:
                self._archive_entry(entries[0], private)
                entries = entries[1:]
                self._write_regular(rel, _join_journal(preamble, entries))

    def _archive_entry(self, entry: str, private: bool) -> None:
        heading = entry.splitlines()[0].rstrip()
        rel = f"{ARCHIVE_DIRS[private]}/{heading[3:10]}.md"
        preamble, entries = _split_journal(self.read(rel))
        if any(item.splitlines()[0].rstrip() == heading for item in entries):
            return  # already archived by an interrupted earlier move
        entries = sorted([*entries, entry], key=lambda item: item.splitlines()[0])
        self._write_regular(rel, _join_journal(preamble or f"# Journal {heading[3:10]}", entries))

    def write_habits_line(self, start: time, end: time) -> None:
        home = self.scoped(allow_private=False)
        rel = "memory/HABITS.md"
        path = home._resolve(rel)
        content = path.read_bytes().decode("utf-8") if path.is_file() else ""
        window = f'"{start:%H:%M}-{end:%H:%M}"'
        matches = list(re.finditer(r"^([ \t]*(?:[-*][ \t]*)?sleep[ \t]*:[ \t]*)[^\r\n]*", content, re.MULTILINE))
        if matches:
            line = matches[-1]  # the parser uses the last value of a repeated key
            content = content[:line.end(1)] + window + content[line.end():]
        else:
            newline = "\r\n" if "\r\n" in content else "\n"
            separator = newline if content and not content.endswith("\n") else ""
            content += f"{separator}sleep: {window}{newline}"
        home._write_regular(rel, content)

    def preset_names(self) -> list[str]:
        try:
            return sorted(path.stem for path in self.scoped(allow_private=False)._resolve("presets").glob("*.md"))
        except (PrivateAccessError, ValueError):
            return []

    def preset(self, name: str) -> tuple[dict[str, Any], str] | None:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        path = self.scoped(allow_private=False)._resolve(f"presets/{name}.md")
        return parse_front_matter(path.read_text(encoding="utf-8")) if path.is_file() else None

    def write_preset(self, name: str, content: str) -> None:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError("expected a preset filename without its extension")
        self.scoped(allow_private=False)._write_regular(f"presets/{name}.md", content)

    def soul(self) -> tuple[dict[str, Any], str]:
        return parse_front_matter(self.read("SOUL.md"))


def _journal_path(private: bool) -> str:
    return "private/JOURNAL.md" if private else "memory/JOURNAL.md"


def _join_journal(preamble: str, entries: list[str]) -> str:
    return "\n\n".join(filter(None, [preamble, *entries])) + "\n"


def _foreign(line: str, source: str) -> bool:
    """A line tagged to a chat other than the one writing."""
    return notes.parse_line(line).source not in (None, notes.ANY, source)


def _pin_hand_edits(current: str, text: str, today: date) -> str:
    """Mark each bullet the owner added or reworded as their core memory. Lines that only had a tag edited stay as typed."""
    before = set(current.split("\n"))
    bodies = {notes.parse_line(raw).body for raw in before}
    out = []
    for raw in text.split("\n"):
        if raw in before or not notes.is_bullet(raw) or notes.parse_line(raw).body in bodies:
            out.append(raw)
            continue
        line = notes.parse_line(raw)
        out.append(notes.format_line(notes.Line(line.body, line.source, notes.Core("owner", today.isoformat()), line.entities)))
    return "\n".join(out)


def _split_journal(text: str) -> tuple[str, list[str]]:
    starts = [match.start() for match in _JOURNAL_HEADING.finditer(text)]
    bounds = [*starts, len(text)]
    return text[:bounds[0]].rstrip(), [text[start:end].rstrip() for start, end in zip(starts, bounds[1:])]


def _scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML: flat ``key: value`` lines plus one-level nested maps."""
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---")
    if not sep:
        return {}, text
    body = body.lstrip("\n") if body.startswith("\n") else body
    meta: dict[str, Any] = {}
    nested: dict[str, Any] | None = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.strip().partition(":")
        if line[0] in " \t" and nested is not None:
            nested[key.strip()] = _scalar(value)
        elif value.strip():
            meta[key.strip()] = _scalar(value)
            nested = None
        else:
            nested = meta[key.strip()] = {}
    return meta, body


def local_timezone() -> str:
    """The machine's IANA timezone name (TZ, then /etc/localtime), or "UTC" if it can't be told."""
    import os
    from zoneinfo import ZoneInfo

    candidates = [os.environ.get("TZ", "")]
    try:
        link = os.readlink("/etc/localtime")
        candidates.append(link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else "")
    except OSError:
        pass
    for name in candidates:
        name = name.lstrip(":")
        if name:
            try:
                ZoneInfo(name)
                return name
            except Exception:
                continue
    return "UTC"


def init_home(target: Path, timezone: str | None = None) -> None:
    """Copy the starter kit into ``target``. Refuses a non-empty directory. Sets ``timezone`` in
    config.toml (default: this machine's timezone)."""
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} is not empty")
    with resources.as_file(resources.files("dom") / "starter_home") as starter:
        shutil.copytree(starter, target, dirs_exist_ok=True)
    config = target / "config.toml"
    tz = timezone or local_timezone()
    config.write_text(re.sub(r'(?m)^timezone = ".*"$', f'timezone = "{tz}"', config.read_text(), count=1))
