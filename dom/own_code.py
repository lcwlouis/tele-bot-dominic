"""Read-only access to Dom's own installed code, docs and changelog.

Paths are relative to the folder that holds the `dom` package: `dom/...`, `docs/...`, `README.md` and
`CHANGELOG.md`. Nothing outside those files is readable: no hidden files, no symlinks, no binary files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import dom

READ_LIMIT = 12_000
CHANGELOG_LIMIT = 6_000
MAX_HITS = 30
MAX_FILE_BYTES = 1_000_000
SUFFIXES = {".py", ".md", ".txt", ".toml", ".sql", ".html", ".css", ".yml", ".yaml", ".json"}
ROOT_FOLDERS = {"dom", "docs"}
ROOT_FILES = {"README.md", "CHANGELOG.md"}


def installed_base() -> Path:
    """The folder holding the installed `dom` package, and its README, CHANGELOG and docs if present."""
    return Path(dom.__file__).resolve().parent.parent


def _allowed(parts: tuple[str, ...]) -> bool:
    if not parts or any(not part or part.startswith(".") or part == "__pycache__" for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    return parts[0] in ROOT_FOLDERS and Path(parts[-1]).suffix in SUFFIXES


def _files(base: Path, prefix: str = "") -> list[tuple[str, Path]]:
    prefix = prefix.strip("/")
    candidates = [base / name for name in ROOT_FILES]
    for folder in ROOT_FOLDERS:
        for directory, folders, names in os.walk(base / folder, followlinks=False):
            folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
            candidates += [Path(directory) / name for name in names]
    found = []
    for path in candidates:
        rel = path.relative_to(base).as_posix()
        if path.is_symlink() or not path.is_file() or not _allowed(Path(rel).parts):
            continue
        if not prefix or rel == prefix or rel.startswith(prefix + "/"):
            found.append((rel, path))
    return sorted(found)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated: showing {limit} of {len(text)} characters]"


def _pieces(text: str, limit: int) -> list[str]:
    """The text cut into pieces of at most `limit` characters, at line ends where a line allows it."""
    pieces, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a line longer than a piece is cut where the piece ends
            room = limit - len(current)
            pieces.append(current + line[:room])
            current, line = "", line[room:]
        if len(current) + len(line) > limit:
            pieces.append(current)
            current = ""
        current += line
    return [*pieces, current] if current or not pieces else pieces


def _page(text: str, limit: int, part: object, how: str) -> str:
    """One part (from 1) of a text too long to return whole, with where it stands and how to ask for the next."""
    pieces = _pieces(text, limit)
    if len(pieces) == 1:
        return text
    if part is None:
        part = 1
    elif isinstance(part, str) and re.fullmatch(r"[0-9]{1,6}", part.strip()):
        part = int(part)
    if not isinstance(part, int) or isinstance(part, bool) or not 1 <= part <= len(pieces):
        return f"error: part must be a number from 1 to {len(pieces)}"
    number = part
    start = sum(len(piece) for piece in pieces[:number - 1])
    end = start + len(pieces[number - 1])
    footer = f"[truncated: part {number} of {len(pieces)}, characters {start + 1}-{end} of {len(text)}"
    footer += f"; {how} with part {number + 1} for the rest]" if number < len(pieces) else "]"
    piece = pieces[number - 1]  # kept whole, blank lines included, so the character range is exact
    return f"{piece}{'' if piece.endswith(chr(10)) else chr(10)}{footer}"


def _resolve(base: Path, rel: object) -> Path | str:
    """The file for a relative path, or an error message."""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        return "error: path must be a file path such as dom/tools.py"
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or not _allowed(parts):
        return f"error: {rel} is not a readable source, doc or changelog file"
    path = base
    for part in parts:  # a link anywhere in the path could lead outside the allowed files
        path = path / part
        if path.is_symlink():
            return f"error: {rel} is a symbolic link"
    if not path.is_file():
        return f"error: no such file: {rel}"
    return path


def _text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file is too large")
    return path.read_bytes().decode("utf-8")


def list_files(base: Path, prefix: str = "") -> str:
    lines = [f"{rel} ({path.stat().st_size} bytes)" for rel, path in _files(base, prefix)]
    return _truncate("\n".join(lines), READ_LIMIT) if lines else "no files"


def read_file(base: Path, rel: object, part: object = 1) -> str:
    path = _resolve(base, rel)
    if isinstance(path, str):
        return path
    try:
        return _page(_text(path), READ_LIMIT, part, "read again")
    except (OSError, ValueError):
        return f"error: {rel} is not a readable text file"


def search(base: Path, query: object, prefix: str = "") -> str:
    if not isinstance(query, str) or not query.strip():
        return "error: query must be non-empty text"
    needle = query.strip().casefold()
    hits = []
    for rel, path in _files(base, prefix):
        try:
            text = _text(path)
        except (OSError, ValueError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle in line.casefold():
                if len(hits) == MAX_HITS:
                    return "\n".join(hits) + f"\n[more matches not shown: limited to {MAX_HITS}]"
                hits.append(f"{rel}:{number}: {line.strip()[:200]}")
    return "\n".join(hits) or "no matches"


def _sections(base: Path) -> list[tuple[str, str]]:
    """(title, text) for each `## ` section of CHANGELOG.md, newest first."""
    path = base / "CHANGELOG.md"
    try:
        text = _text(path) if path.is_file() and not path.is_symlink() else ""
    except (OSError, ValueError):
        return []
    return changelog_sections(text)


def changelog_sections(text: str) -> list[tuple[str, str]]:
    """(title, text) for each `## ` section of a changelog's text, in file order."""
    chunks = re.split(r"(?m)^(?=## )", text)
    return [(chunk.splitlines()[0][3:].strip(), chunk.rstrip() + "\n") for chunk in chunks if chunk.startswith("## ")]


def changelog(base: Path, version: object = None, part: object = 1) -> str:
    """The newest sections that fit, or with `version` (such as 0.8.4) that release's whole section, in parts when
    it is long."""
    sections = _sections(base)
    if not sections:
        return "no changelog"
    if isinstance(version, str) and version.strip():
        wanted = version.strip().removeprefix("v")
        for title, section in sections:
            if title.startswith(f"[{wanted}]"):
                return _page(section, READ_LIMIT, part, "ask again")
        return f"error: no changelog section for {wanted}"
    text = ""
    for _, section in sections:
        if len(text) + len(section) > CHANGELOG_LIMIT:
            break
        text += section + "\n"
    return text.rstrip() + "\n" if text else _truncate(sections[0][1], CHANGELOG_LIMIT)


def latest_release(base: Path) -> dict | None:
    """The newest released section's title and its first three bullets."""
    for title, section in _sections(base):
        if title.casefold().startswith("[unreleased"):
            continue
        bullets: list[str] = []
        for line in section.splitlines():
            if line.startswith("- "):
                bullets.append(line[2:].strip())
            elif line.startswith("  ") and bullets:
                bullets[-1] += " " + line.strip()
        return {"title": title, "highlights": [bullet[:300] for bullet in bullets[:3]]}
    return None
