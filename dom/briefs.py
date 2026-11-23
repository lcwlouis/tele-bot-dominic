"""Core-change briefs that the owner can pass to a coding agent."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from threading import Lock

from dom.home import Home
from dom.skills import _visible

LIMITS = {"title": 120, "problem": 2000, "proposal": 4000, "acceptance": 2000}
_FILENAME = re.compile(r"\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_WRITE_LOCK = Lock()


@dataclass(frozen=True)
class Brief:
    filename: str
    title: str
    markdown: str


def _field(name: str, value: object) -> str:
    limit = LIMITS[name]
    if isinstance(value, str):
        text = value.strip().replace("\r\n", "\n")
        if text and len(value) <= limit and _visible(text) and (name != "title" or text.isprintable()):
            return text
    raise ValueError(f"{name} must contain 1 to {limit} characters of visible text.")


def write_brief(home: Home, day: date, *, title: str, problem: str, proposal: str, acceptance: str) -> Brief:
    title = _field("title", title)
    problem = _field("problem", problem)
    proposal = _field("proposal", proposal)
    acceptance = _field("acceptance", acceptance)
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower())[:64].strip("-") or "brief"
    content = (
        f"# {title}\n\n"
        f"## Problem\n\n{problem}\n\n"
        f"## Proposed change\n\n{proposal}\n\n"
        f"## Acceptance criteria\n\n{acceptance}\n\n"
        f"written by Dom on {day.isoformat()}\n"
    )
    home = home.scoped(allow_private=False)
    base = f"{day.isoformat()}-{slug}"
    with _WRITE_LOCK:
        filename = f"{base}.md"
        number = 2
        while home._regular_path(f"briefs/{filename}").exists():
            filename = f"{base}-{number}.md"
            number += 1
        home._write_regular(f"briefs/{filename}", content)
    return Brief(filename, title, content)


def list_briefs(home: Home) -> list[Brief]:
    home = home.scoped(allow_private=False)
    folder = home._regular_path("briefs")
    if not folder.is_dir():
        return []
    listed = []
    maximum = sum(LIMITS.values()) + 1024
    for path in sorted(folder.iterdir(), reverse=True):
        if not _FILENAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        try:
            source = home._regular_path(f"briefs/{path.name}")
            if source.stat().st_nlink != 1:
                continue
            with source.open(encoding="utf-8") as file:
                markdown = file.read(maximum + 1)
        except (OSError, ValueError):
            continue
        title = markdown.partition("\n")[0].removeprefix("# ")
        if title and markdown.startswith("# ") and len(markdown) <= maximum:
            listed.append(Brief(path.name, title, markdown))
    return listed


def markdown_blocks(markdown: str) -> list[tuple[str, list[str], str]]:
    """Render brief Markdown as headings, paragraphs, lists and fenced code, with escaped text. Each block is
    (kind, lines, language); language is a code fence's info word, for the dashboard's highlighting, else ""."""
    blocks: list[tuple[str, list[str], str]] = []
    kind = ""
    lines: list[str] = []
    fence = ""
    language = ""

    def flush() -> None:
        if lines:
            blocks.append((kind, lines.copy(), language if kind == "code" else ""))
            lines.clear()

    for line in markdown.splitlines()[1:]:  # the card already displays the title
        if fence:
            if line.strip() == fence:
                flush()
                kind = fence = ""
            else:
                lines.append(line)
            continue
        if match := re.match(r"^(```+|~~~+)", line):
            flush()
            fence = match[1]
            kind = "code"
            language = (line[len(fence):].split() or [""])[0][:20].lower()
            continue
        if line.startswith("## "):
            flush()
            blocks.append(("heading", [line[3:]], ""))
            kind = ""
            continue
        if not line.strip():
            flush()
            continue
        match = re.match(r"^\s*(?P<bullet>[-*+] |\d+\. )(?P<text>.+)", line)
        next_kind = ("ol" if match["bullet"][0].isdigit() else "ul") if match else "p"
        if next_kind != kind:
            flush()
            kind = next_kind
        lines.append(match["text"] if match else line)
    flush()
    return blocks
