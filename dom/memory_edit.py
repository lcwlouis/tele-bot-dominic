"""Owner-DM-only memory self-edit proposals (opt-in via `memory_self_edit = true`).

Dom proposes a note edit as a unified diff with a one-line reason on an approval card.
Nothing is written until the owner approves, and approval refuses if the file changed
since the proposal was created.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher, unified_diff

from dom.config import Config
from dom.home import UNBOUNDED, Home, PrivateAccessError, content_version, limit_for
from dom.tools import Tool, TurnCtx

REASON_LIMIT = 200
MAX_JOURNAL_CHARS = 65536
PENDING_LIMIT = 10

_OPS = ("add", "replace", "remove", "set")
_SHORT_NAMES = {
    "USER.md": "private/USER.md",
    "NOTES.md": "private/NOTES.md",
    "MEMORY.md": "memory/MEMORY.md",
}
_EDITABLE_EXACT = {
    "private/USER.md",
    "private/NOTES.md",
    "private/JOURNAL.md",
    "memory/JOURNAL.md",
    "memory/MEMORY.md",
}
_PERSON_NOTE = re.compile(r"\A(?:memory/)?people/(\d+|handle_[a-z0-9_]+)\.md\Z")
_CHAT_NOTE = re.compile(r"\A(?:memory/)?chats/(-?\d+)\.md\Z")
_DATE_HEADING = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_CLAUSE_SPLIT = re.compile(r"(?:[.;!?]+|:)\s+")
_SENTENCE_SPLIT = re.compile(r"[.;!?]+\s+")
_PRIVATE_FILES = ("private/USER.md", "private/NOTES.md", "private/JOURNAL.md")


def resolve_note_path(file: object) -> str:
    """Validate and normalize a note path to its canonical DOM_HOME-relative form."""
    if not isinstance(file, str):
        raise ValueError("file must be a note path")
    if "\n" in file or "\r" in file or "\x00" in file:
        raise ValueError("invalid note path")
    name = file.strip()
    if not name or name.startswith(("/", "~")) or "\\" in name or ".." in name:
        raise ValueError(f"invalid note path: {file!r}")
    if name in ("HABITS.md", "memory/HABITS.md"):
        raise ValueError("HABITS.md is read-only.")
    if name == "JOURNAL.md":
        raise ValueError("Specify private/JOURNAL.md or memory/JOURNAL.md.")
    if name in _SHORT_NAMES:
        return _SHORT_NAMES[name]
    if name in _EDITABLE_EXACT:
        return name
    if match := _PERSON_NOTE.fullmatch(name):
        return f"memory/people/{match.group(1)}.md"
    if match := _CHAT_NOTE.fullmatch(name):
        return f"memory/chats/{match.group(1)}.md"
    raise ValueError(f"{file!r} is not an editable note")


def clean_reason(reason: object) -> str:
    """Normalize and validate a one-line reason for the approval card."""
    if not isinstance(reason, str):
        raise ValueError("reason must be a single non-empty line")
    cleaned = " ".join(reason.split())
    if not cleaned or len(cleaned) > REASON_LIMIT or not cleaned.isprintable():
        raise ValueError(f"reason must be one printable line of 1 to {REASON_LIMIT} characters")
    return cleaned


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_regular_note(home: Home, rel: str) -> str:
    """Read an editable note strictly as a regular, non-linked file within its Home scope."""
    scoped = home.scoped(allow_private=rel.startswith("private/"))
    try:
        path = scoped._regular_path(rel)
    except PrivateAccessError:
        raise ValueError(f"{rel} is a link; notes must be regular files") from None
    if not path.exists():
        return ""
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"{rel} must be a regular file")
    return path.read_text(encoding="utf-8")


def apply_edit_op(rel: str, current: str, op: object, text: object = "", old: object = None) -> str:
    """Compute the proposed file content for an edit operation without writing anything."""
    if op not in _OPS:
        raise ValueError(f"op must be one of {', '.join(_OPS)}")
    if op == "add":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("add needs non-empty `text`")
        entry = _normalize_newlines(text).strip()
        if rel in UNBOUNDED:
            new = f"{current.rstrip(chr(10))}\n\n{entry}\n" if current.strip() else f"{entry}\n"
        else:
            if not entry.startswith(("- ", "* ", "#")):
                entry = f"- {entry}"
            new = f"{current.rstrip(chr(10))}\n{entry}" if current.strip() else entry
    elif op == "replace":
        if not isinstance(old, str) or not old:
            raise ValueError("replace needs non-empty `old`")
        if not isinstance(text, str):
            raise ValueError("replace needs `text`")
        old_norm = _normalize_newlines(old)
        text_norm = _normalize_newlines(text)
        count = current.count(old_norm)
        if count != 1:
            raise ValueError(f"`old` must match exactly once, found {count}")
        new = current.replace(old_norm, text_norm, 1)
    elif op == "remove":
        if not isinstance(old, str) or not old:
            raise ValueError("remove needs non-empty `old`")
        old_norm = _normalize_newlines(old)
        count = current.count(old_norm)
        if count != 1:
            raise ValueError(f"`old` must match exactly once, found {count}")
        if "\n" in old_norm:
            new = current.replace(old_norm, "", 1)
        else:
            lines = current.split("\n")
            new = "\n".join(line for line in lines if old_norm not in line)
    else:  # set
        if not isinstance(text, str):
            raise ValueError("set needs `text`")
        new = _normalize_newlines(text)

    if new == current:
        raise ValueError("That edit makes no change.")
    return new


def check_size_bounds(rel: str, current: str, new: str) -> None:
    """Respect each note's character bound (shrinking an already over-limit note stays allowed)."""
    limit = limit_for(rel)
    if limit is not None:
        if len(new) > limit and len(new) >= len(current):
            raise ValueError(f"over limit: {len(new)} > {limit} characters; merge or remove entries first")
        return
    if len(new) > MAX_JOURNAL_CHARS and len(new) >= len(current):
        raise ValueError(f"over limit: {len(new)} > {MAX_JOURNAL_CHARS} characters")


def _added_lines(old: str, new: str) -> list[str]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    added: list[str] = []
    for tag, _, _, j1, j2 in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new_lines[j1:j2])
    return added


def check_no_private_to_public(config: Config, home: Home, rel: str, old: str, new: str) -> None:
    """Refuse edits to public notes (`memory/`) that copy credentials or private notes into them."""
    if rel.startswith("private/"):
        return
    added = _added_lines(old, new)
    if not added:
        return
    added_text = "\n".join(added)
    secrets = [config.secret(name) for name in config.secret_names()]
    if any(secret and len(secret) >= 8 and (secret in added_text or secret in new) for secret in secrets):
        raise ValueError("Cannot move credentials into a public note.")

    private = home.scoped(allow_private=True)
    phrases: set[str] = set()
    bullet_lines: set[str] = set()
    for private_rel in _PRIVATE_FILES:
        try:
            content = read_regular_note(private, private_rel)
        except (OSError, ValueError):
            continue
        is_journal = private_rel == "private/JOURNAL.md"
        for raw_line in content.splitlines():
            clean = raw_line.strip(" \t#-*")
            if len(clean) < 8 or _DATE_HEADING.fullmatch(clean):
                continue
            folded = clean.casefold()
            phrases.add(folded)
            if not is_journal:
                if len(folded) >= 12:
                    bullet_lines.add(folded)
                for chunk in _CLAUSE_SPLIT.split(clean):
                    chunk = chunk.strip(" \t#-*")
                    if len(chunk) >= 12 and not _DATE_HEADING.fullmatch(chunk):
                        phrases.add(chunk.casefold())
            else:
                for sentence in _SENTENCE_SPLIT.split(clean):
                    sentence = sentence.strip(" \t#-*")
                    if len(sentence) >= 16 and not _DATE_HEADING.fullmatch(sentence):
                        phrases.add(sentence.casefold())

    folded_added = added_text.casefold()
    if any(phrase in folded_added for phrase in phrases):
        raise ValueError("Cannot move private notes into a public note.")
    if bullet_lines:
        for line in added:
            clean = line.strip(" \t#-*").casefold()
            if len(clean) >= 12 and not _DATE_HEADING.fullmatch(clean):
                if any(clean in pline for pline in bullet_lines):
                    raise ValueError("Cannot move private notes into a public note.")


def memory_diff(rel: str, old: str, new: str) -> list[str]:
    """Unified diff lines for a proposed note edit."""
    return list(unified_diff(old.splitlines(), new.splitlines(), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))


def prepare_proposal(config: Config, home: Home, body: dict) -> tuple[str, dict]:
    """Validate a `memory_edit` proposal and build its normalized title and body."""
    rel = resolve_note_path(body.get("file"))
    reason = clean_reason(body.get("reason"))
    current = read_regular_note(home, rel)
    if "op" in body:
        new = apply_edit_op(rel, current, body.get("op"), body.get("text", ""), body.get("old"))
    elif isinstance(body.get("new"), str):
        new = _normalize_newlines(body["new"])
        if new == current:
            raise ValueError("That edit makes no change.")
    else:
        raise ValueError("A memory edit needs `op` (add, replace, remove, set) or `new` text.")
    check_size_bounds(rel, current, new)
    check_no_private_to_public(config, home, rel, current, new)
    diff = "\n".join(memory_diff(rel, current, new))
    title = f"{rel}: {reason}"
    normalized = {
        "file": rel,
        "reason": reason,
        "old": current,
        "new": new,
        "version": content_version(current),
        "diff": diff,
    }
    return title, normalized


def apply_proposal(config: Config, home: Home, body: dict) -> None:
    """Apply an approved `memory_edit` proposal after re-checking path, version, privacy and bounds."""
    rel = resolve_note_path(body.get("file"))
    old = body.get("old")
    new = body.get("new")
    version = body.get("version")
    if not isinstance(old, str) or not isinstance(new, str) or not isinstance(version, str):
        raise ValueError("Malformed memory edit proposal.")
    current = read_regular_note(home, rel)
    if content_version(current) != version:
        raise ValueError(f"{rel} changed since this proposal was created.")
    check_size_bounds(rel, current, new)
    check_no_private_to_public(config, home, rel, current, new)
    scoped = home.scoped(allow_private=rel.startswith("private/"))
    try:
        result = scoped.save_memory(rel, new, version)
    except PrivateAccessError:
        raise ValueError(f"{rel} is a link; notes must be regular files") from None
    if not result.ok:
        if result.error and "changed since" in result.error:
            raise ValueError(f"{rel} changed since this proposal was created.")
        raise ValueError(f"{rel}: {result.error}")


def format_summary(heading: str, body: dict) -> str:
    """Format a `memory_edit` proposal for Telegram cards and /proposals."""
    diff = body.get("diff") or "\n".join(memory_diff(body["file"], body["old"], body["new"]))
    return f"{heading}\n\n{diff}" if diff else heading


async def _propose_memory_edit(args: dict, ctx: TurnCtx) -> str:
    app = ctx.app
    if app is None or not ctx.is_owner_dm():
        return "error: memory edits can only be proposed in the owner's DM"
    if not app.config.memory_self_edit:
        return "error: memory self-edit is turned off"
    pending = [row for row in await app.proposals.pending() if row["kind"] == "memory_edit"]
    if len(pending) >= PENDING_LIMIT:
        return f"error: {len(pending)} memory edit proposals already wait for the owner; wait for them to decide"
    payload = {key: args[key] for key in ("file", "op", "text", "old", "reason") if key in args}
    try:
        proposal_id = await app.proposals.create("memory_edit", "", payload, "chat")
    except (OSError, ValueError) as error:
        return f"error: {error}"
    return json.dumps({"id": proposal_id, "status": "waiting for the owner's approval"})


PROPOSE_MEMORY_EDIT_TOOL = Tool(
    name="propose_memory_edit",
    description=(
        "Owner's DM only. Propose an edit to one of your notes (private/USER.md, private/NOTES.md, "
        "private/JOURNAL.md, memory/JOURNAL.md, memory/MEMORY.md, memory/people/<id>.md, or memory/chats/<id>.md) "
        "with a one-line reason. Sends an approval card with a diff; nothing changes until the owner approves. "
        "Never move private notes into public memory/ files. SOUL.md, HABITS.md, config, skills and plugins cannot "
        "be edited here."
    ),
    schema={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": (
                    "Note path: private/USER.md, private/NOTES.md, private/JOURNAL.md, memory/JOURNAL.md, "
                    "memory/MEMORY.md, memory/people/<id>.md, or memory/chats/<id>.md."
                ),
            },
            "op": {
                "type": "string",
                "enum": ["add", "replace", "remove", "set"],
                "description": (
                    "add: append a new entry; replace: replace one unique match of `old` with `text`; "
                    "remove: delete lines matching unique `old`; set: replace the whole file with `text`."
                ),
            },
            "text": {"type": "string", "description": "Entry to add, replacement text, or full file content for `set`."},
            "old": {"type": "string", "description": "Exact unique text in the file (required for `replace` and `remove`)."},
            "reason": {"type": "string", "maxLength": REASON_LIMIT, "description": "One-line reason shown on the approval card."},
        },
        "required": ["file", "op", "reason"],
    },
    handler=_propose_memory_edit,
    scopes={"owner_dm"},
)
