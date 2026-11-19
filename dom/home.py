"""The instance home (DOM_HOME): layout, bounded memory files, the private/ boundary."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
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
# Writable through the memory tool but not size-bounded (never loaded into chat prompts).
UNBOUNDED = {"memory/JOURNAL.md"}

STARTER_KIT = Path(__file__).resolve().parent.parent / "examples" / "home"


class PrivateAccessError(PermissionError):
    """Raised when private/ is touched through a Home that was not opened for it."""


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    content: str
    error: str | None = None


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
        if pure.parts[0] == "private" and not self.allow_private:
            raise PrivateAccessError(f"{rel} is not available in this scope")
        return self.root.joinpath(*pure.parts)

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
        try:
            path = self._resolve(rel)
        except ValueError as exc:  # traversal; PrivateAccessError still propagates
            return WriteResult(False, "", str(exc))
        limit = limit_for(rel)
        if limit is None and rel not in UNBOUNDED:
            return WriteResult(False, "", f"{rel} is not a writable memory file")
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

        if limit is not None and len(new) > limit:
            return WriteResult(
                False,
                current,
                f"over limit: {len(new)} > {limit} characters; merge or remove entries first",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
        return WriteResult(True, new)

    def soul(self) -> tuple[dict[str, Any], str]:
        return parse_front_matter(self.read("SOUL.md"))


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


def init_home(target: Path) -> None:
    """Copy the starter kit into ``target``. Refuses a non-empty directory."""
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} is not empty")
    shutil.copytree(STARTER_KIT, target, dirs_exist_ok=True)
