"""The instance home (DOM_HOME): layout, bounded memory files, the private/ boundary."""

from __future__ import annotations

import json
import re
import shutil
from tempfile import NamedTemporaryFile
from threading import Lock
from importlib import resources
from dataclasses import dataclass
from datetime import date, time
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Literal

# Character limits, keyed by path pattern relative to the home.
LIMITS: dict[str, int] = {
    "memory/MEMORY.md": 2200,
    "memory/HABITS.md": 1000,
    "memory/people/*.md": 1000,
    "memory/chats/*.md": 1000,
    "private/USER.md": 1375,
    "private/NOTES.md": 2200,
}
# Writable through memory_write but not size-bounded (never loaded into chat prompts).
UNBOUNDED = {"memory/JOURNAL.md"}
_MEMORY_WRITE_LOCK = Lock()
_JOURNAL_HEADING = re.compile(r"^## \d{4}-\d{2}-\d{2}[ \t]*$", re.MULTILINE)



class PrivateAccessError(PermissionError):
    """Raised when private/ is touched through a Home that was not opened for it."""


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    content: str
    error: str | None = None
    over_limit: bool = False


def limit_for(rel: str) -> int | None:
    for pattern, limit in LIMITS.items():
        if fnmatchcase(rel, pattern):
            return limit
    return None


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
    ) -> WriteResult:
        # Proposal decisions run in the store worker. Serialize their edits with memory passes.
        with _MEMORY_WRITE_LOCK:
            return self._memory_write(rel, op, text, old)

    def _memory_write(
        self,
        rel: str,
        op: Literal["add", "replace", "remove"],
        text: str,
        old: str | None = None,
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

        if op == "add":
            new = f"{current.rstrip(chr(10))}\n- {text}" if current.strip() else f"- {text}"
        elif op in ("replace", "remove"):
            if not old:
                return WriteResult(False, current, f"{op} needs `old`")
            count = current.count(old)
            if count != 1:
                return WriteResult(False, current, f"`old` must match exactly once, found {count}")
            if op == "replace":
                new = current.replace(old, text)
            else:
                lines = current.split("\n")
                new = "\n".join(line for line in lines if old not in line)
        else:
            return WriteResult(False, current, f"unknown op {op!r}")

        # Shrinking is always allowed so a file that is already over its limit can be pruned.
        if limit is not None and len(new) > limit and len(new) >= len(current):
            return WriteResult(
                False,
                current,
                f"over limit: {len(new)} > {limit} characters; merge or remove entries first",
                over_limit=True,
            )
        self._write_regular(rel, new)
        return WriteResult(True, new)

    def _write_regular(self, rel: str, content: str) -> None:
        path = self._resolve(rel)
        if path != self.root.resolve().joinpath(*PurePosixPath(rel).parts):
            raise ValueError(f"{rel} is a link; expected a regular file")
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

    def replace_soul(self, old: str, new: str) -> bool:
        home = self.scoped(allow_private=False)
        current = home.read("SOUL.md")
        first = current.find(old) if old else -1
        if first == -1 or current.find(old, first + 1) != -1:
            return False
        home._write_regular("SOUL.md", current[:first] + new + current[first + len(old):])
        return True

    def journal_entries(self, *, private: bool = False) -> list[str]:
        return _split_journal(self.read(_journal_path(private)))[1]

    def append_journal(self, day: date, body: str, *, private: bool = False) -> None:
        rel = _journal_path(private)
        heading = f"## {day.isoformat()}"
        preamble, entries = _split_journal(self.read(rel))
        entries = [entry for entry in entries if entry.splitlines()[0].rstrip() != heading]
        entries.append(f"{heading}\n{body}")
        text = "\n\n".join(filter(None, [preamble, *entries[-30:]])) + "\n"
        self._write_regular(rel, text)

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
