"""Public procedures stored as owner-approved Markdown in DOM_HOME."""

from __future__ import annotations

import json
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from difflib import unified_diff
from hashlib import sha256
from xml.sax.saxutils import escape

from dom.home import Home

NAME_LIMIT = 64
DESCRIPTION_LIMIT = 200
BODY_LIMIT = 4000
PROMPT_LIMIT = 30  # skills named in the prompt; skills_list shows the rest
RETIRED_MARKER = ".retired"
SKILL_LOCK = threading.Lock()
_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_READ_LIMIT = 4 * (BODY_LIMIT + DESCRIPTION_LIMIT + NAME_LIMIT) + 300


# Unicode Default_Ignorable_Code_Point: rendered as nothing, so text can hide in them. Several are
# category Mn or Lo (U+034F, U+FE0F, U+3164), which the category check below does not catch.
_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)


def _visible(text: str) -> bool:
    """No invisible, bidi, tag, private-use, unassigned, control or line-separator characters (newline and tab aside)."""
    return all(
        char in "\n\t" or (
            unicodedata.category(char) not in ("Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp")
            and not any(low <= ord(char) <= high for low, high in _IGNORABLE)
        )
        for char in text
    )


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or len(name) > NAME_LIMIT or not _NAME.fullmatch(name) or not _visible(name):
        raise ValueError("Skill name must be lowercase words or numbers with hyphens, up to 64 characters.")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str

    def __post_init__(self) -> None:
        _validate_name(self.name)
        if (
            not isinstance(self.description, str) or not self.description.strip()
            or len(self.description) > DESCRIPTION_LIMIT or not self.description.isprintable()
            or not _visible(self.description)
        ):
            raise ValueError("Skill description must be one line of up to 200 characters.")
        if not isinstance(self.body, str) or not self.body.strip() or len(self.body) > BODY_LIMIT or not _visible(self.body):
            raise ValueError("Skill body must contain 1 to 4,000 characters of text.")

    def render(self) -> str:
        description = json.dumps(self.description, ensure_ascii=False)
        return f"---\nname: {self.name}\ndescription: {description}\n---\n{self.body}"


def _rel(name: str) -> str:
    _validate_name(name)
    return f"skills/{name}/SKILL.md"


def skill_text(home: Home, name: str) -> str:
    path = home.scoped(allow_private=False)._regular_path(_rel(name))
    if not path.is_file():
        return ""
    if path.stat().st_nlink != 1:
        raise ValueError("Skill files must have their own regular file.")
    with path.open(encoding="utf-8") as file:
        text = file.read(_READ_LIMIT + 1)
    if len(text) > _READ_LIMIT:
        raise ValueError("Skill file exceeds its size limit.")
    return text


def _parse(name: str, text: str) -> Skill:
    header, separator, body = text.removeprefix("---\n").partition("\n---\n")
    if not text.startswith("---\n") or not separator:
        raise ValueError("Skill file is missing its front matter.")
    fields = dict(line.split(":", 1) for line in header.splitlines() if ":" in line)
    if fields.get("name", "").strip().strip("\"'") != name:
        raise ValueError("Skill name must match its directory.")
    description = fields.get("description", "").strip()
    if description.startswith('"'):
        description = json.loads(description)
    elif description.startswith("'") and description.endswith("'"):
        description = description[1:-1].replace("''", "'")
    return Skill(name, description, body)


def _retired_rel(name: str) -> str:
    _validate_name(name)
    return f"skills/{name}/{RETIRED_MARKER}"


def is_retired(home: Home, name: str) -> bool:
    return home.scoped(allow_private=False)._regular_path(_retired_rel(name)).is_file()


def skill_exists(home: Home, name: str) -> bool:
    _validate_name(name)
    public = home.scoped(allow_private=False)
    try:
        folder = public._regular_path(f"skills/{name}")
    except ValueError:
        return False
    return folder.is_dir() and not folder.is_symlink()


def content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def skill_hash(home: Home, name: str) -> str:
    """SHA-256 hex digest of an existing skill's SKILL.md, or FileNotFoundError if absent."""
    text = skill_text(home, name)
    if not text:
        raise FileNotFoundError(f"Skill {name} not found.")
    return content_hash(text)


def skills_inventory(home: Home) -> list[dict[str, object]]:
    """Every valid skill on disk, active and retired, for owner management."""
    public = home.scoped(allow_private=False)
    try:
        folder = public._regular_path("skills")
        if not folder.is_dir():
            return []
        names = sorted(path.name for path in folder.iterdir())
    except (OSError, ValueError):
        return []
    listed: list[dict[str, object]] = []
    for name in names:
        try:
            if text := skill_text(public, name):
                skill = _parse(name, text)
                listed.append({
                    "name": skill.name,
                    "description": skill.description,
                    "body": skill.body,
                    "hash": content_hash(text),
                    "retired": is_retired(public, name),
                })
        except (OSError, ValueError):
            continue
    return listed


def skills_list(home: Home) -> list[dict[str, str]]:
    return [
        {"name": str(skill["name"]), "description": str(skill["description"])}
        for skill in skills_inventory(home) if not skill["retired"]
    ]


def skill_view(home: Home, name: str) -> str | None:
    if is_retired(home, name):
        return None
    text = skill_text(home, name)
    return _parse(name, text).body if text else None


def view_text(home: Home, name: str) -> str:
    """A skill's body for a tool result, or why there is none. Chat and desktop tools both return this."""
    try:
        return skill_view(home, name) or "Skill not found."
    except (OSError, ValueError):
        return "Skill unavailable."


def prompt_block(home: Home) -> str:
    """Name and description of each approved skill for the stable prompt prefix; empty when there are none."""
    listed = skills_list(home)
    if not listed:
        return ""
    lines = [f"- {skill['name']}: {escape(skill['description'])}" for skill in listed[:PROMPT_LIMIT]]
    if len(listed) > PROMPT_LIMIT:
        lines.append(f"({len(listed) - PROMPT_LIMIT} more: call skills_list)")
    return "<skills>\nApproved procedures. Call skill_view(name) to read one when it applies.\n" + "\n".join(lines) + "\n</skills>"


def write_skill(home: Home, skill: Skill, expected: str) -> None:
    with SKILL_LOCK:
        home = home.scoped(allow_private=False)
        if skill_text(home, skill.name) != expected:
            raise ValueError("The skill changed since this proposal was created.")
        home._write_regular(_rel(skill.name), skill.render())


def retire_skill(home: Home, name: str) -> bool:
    """Turn off an existing skill while keeping its files on disk. False if already retired."""
    with SKILL_LOCK:
        public = home.scoped(allow_private=False)
        text = skill_text(public, name)
        if not text:
            raise FileNotFoundError(f"Skill {name} not found.")
        _parse(name, text)
        if is_retired(public, name):
            return False
        public._write_regular(_retired_rel(name), "retired\n")
        return True


def restore_skill(home: Home, name: str) -> bool:
    """Re-enable a retired skill. False if it is not retired."""
    with SKILL_LOCK:
        public = home.scoped(allow_private=False)
        text = skill_text(public, name)
        if not text:
            raise FileNotFoundError(f"Skill {name} not found.")
        _parse(name, text)
        marker = public._regular_path(_retired_rel(name))
        if not marker.is_file():
            return False
        marker.unlink()
        return True


def delete_skill_files(home: Home, name: str) -> bool:
    """Remove a skill's folder from disk."""
    with SKILL_LOCK:
        _validate_name(name)
        public = home.scoped(allow_private=False)
        folder = public._regular_path(f"skills/{name}")
        if not folder.exists():
            return False
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("Skill path must be a regular directory.")
        shutil.rmtree(folder)
        return True


def skill_diff(old: str, new: str) -> list[str]:
    return list(unified_diff(old.splitlines(), new.splitlines(), fromfile="Current", tofile="Proposed", lineterm=""))
