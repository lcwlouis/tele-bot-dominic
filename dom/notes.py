"""The line format of Dom's notes: source tags, core (pinned) marks and dated facts.

A note is a Markdown bullet list. A line may end with metadata groups in braces, which people can read and edit:

    - Works as a nurse (as of 2026-11) {chat -100123}
    - Hates surprises {any}
    - Plans a trip to Japan (until 2027-04-30) {chat -100123} {core dom 2026-11-25: she talks about it all the time}
    - Loves tea {core owner 2026-11-25}

* `{chat <id>}`: told in that chat; shown only there. `{any}`: general and non-sensitive, shown in any chat the
  person is in. No source group (older lines, or lines the owner typed): counts as `{any}`.
* `{core owner DATE}` or `{core dom DATE: why}`: a core memory (pinned). It never fades, is never summarised away,
  and the memory pass never rewrites or drops it. Only Dom can unpin his own; the owner's stay until the owner
  changes them.
* `(as of YYYY-MM)` and `(until YYYY-MM-DD)` are plain text in the line. Code moves a line whose `until` date has
  passed into the `<file>.former.md` sibling.

Tags are metadata: they never count toward a file's size limit, and the prompt builder strips them before the model
sees a note. Filtering by source happens here, in code; no prompt text is trusted to keep a fact in its chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_META = r"\{(?:any|chat -?\d+|core (?:owner|dom) \d{4}-\d{2}-\d{2}(?::[^{}\n]*)?)\}"
_TRAILING_META = re.compile(rf"(?:[ \t]+{_META})+[ \t]*$")
_ONE_META = re.compile(r"\{(any|chat -?\d+|core (owner|dom) (\d{4}-\d{2}-\d{2})(?::([^{}\n]*))?)\}")
_UNTIL = re.compile(r"\(until (\d{4})-(\d{2})-(\d{2})\)")
_BULLET = re.compile(r"^[ \t]*[-*][ \t]+")

ANY = "any"


@dataclass(frozen=True)
class Core:
    by: str  # "owner" or "dom"
    day: str  # YYYY-MM-DD
    why: str = ""


@dataclass(frozen=True)
class Line:
    body: str  # the line without its trailing metadata
    source: str | None = None  # "any", a chat id as text, or None when untagged
    core: Core | None = None


def parse_line(line: str) -> Line:
    match = _TRAILING_META.search(line)
    if match is None:
        return Line(line.rstrip())
    source, core = None, None
    for part in _ONE_META.finditer(match.group(0)):
        text = part.group(1)
        if text.startswith("core"):
            core = Core(part.group(2), part.group(3), (part.group(4) or "").strip())
        else:
            source = ANY if text == ANY else text.split(" ", 1)[1]
    return Line(line[: match.start()].rstrip(), source, core)


def format_line(line: Line) -> str:
    out = line.body
    if line.source is not None:
        out += " {any}" if line.source == ANY else f" {{chat {line.source}}}"
    if line.core is not None:
        why = f": {_clean_why(line.core.why)}" if line.core.why else ""
        out += f" {{core {line.core.by} {line.core.day}{why}}}"
    return out


def _clean_why(why: str) -> str:
    return " ".join(why.replace("{", "(").replace("}", ")").split())[:160]


def is_bullet(line: str) -> bool:
    return bool(_BULLET.match(line))


def strip_tags(text: str) -> str:
    """The text as the model reads it: no source or core marks."""
    return "\n".join(parse_line(line).body for line in text.split("\n"))


def visible_len(text: str) -> int:
    """Length that counts toward a size limit: tags are metadata and are free (D18)."""
    return len(strip_tags(text))


def limit_to_chat(text: str, chat_id: int | str | None) -> str:
    """The lines of a note that belong in one chat, tags kept: untagged, `{any}`, or tagged to that chat.
    `chat_id=None` is the shared context (MEMORY.md), where no chat-tagged line belongs."""
    return "\n".join(
        raw for raw in text.split("\n")
        if (source := parse_line(raw).source) in (None, ANY) or source == str(chat_id)
    )


def for_chat(text: str, chat_id: int | str | None, *, everything: bool = False) -> str:
    """A note as one chat's prompt shows it: lines tagged to another chat dropped, tags stripped. `everything` is
    for the owner's DM, which may read every line of the public notes."""
    return strip_tags(text if everything else limit_to_chat(text, chat_id))


def chat_tagged(text: str) -> bool:
    return any(parse_line(raw).source not in (None, ANY) for raw in text.split("\n"))


def core_lines(text: str) -> list[Line]:
    return [line for raw in text.split("\n") if (line := parse_line(raw)).core is not None]


def expired_until(line: str, today: date) -> bool:
    match = _UNTIL.search(parse_line(line).body)
    if match is None:
        return False
    try:
        return date(*map(int, match.groups())) < today
    except ValueError:
        return False


def former_rel(rel: str) -> str:
    """`memory/people/7.md` -> `memory/people/7.former.md`."""
    return rel.removesuffix(".md") + ".former.md"


def is_former(rel: str) -> bool:
    return rel.endswith(".former.md")


def former_line(raw: str, today: date, reason: str) -> str:
    return f"{raw.rstrip()} [former {today.isoformat()}: {reason}]"


def retag(text: str, old_chat: str, new_chat: str) -> str:
    """Point lines tagged to `old_chat` at `new_chat` (a group that migrated to a supergroup)."""
    return text.replace(f"{{chat {old_chat}}}", f"{{chat {new_chat}}}")
