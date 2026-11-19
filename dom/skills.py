"""Public procedures stored as owner-approved Markdown in DOM_HOME."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import unified_diff

from dom.home import Home

NAME_LIMIT = 64
DESCRIPTION_LIMIT = 200
BODY_LIMIT = 4000
_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_READ_LIMIT = 4 * (BODY_LIMIT + DESCRIPTION_LIMIT + NAME_LIMIT) + 300


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or len(name) > NAME_LIMIT or not _NAME.fullmatch(name):
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
        ):
            raise ValueError("Skill description must be one line of up to 200 characters.")
        if not isinstance(self.body, str) or not self.body.strip() or len(self.body) > BODY_LIMIT or "\x00" in self.body:
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


def skills_list(home: Home) -> list[dict[str, str]]:
    public = home.scoped(allow_private=False)
    try:
        folder = public._regular_path("skills")
        if not folder.is_dir():
            return []
        names = sorted(path.name for path in folder.iterdir())
    except (OSError, ValueError):
        return []
    listed = []
    for name in names:
        try:
            if text := skill_text(public, name):
                skill = _parse(name, text)
                listed.append({"name": skill.name, "description": skill.description})
        except (OSError, ValueError):
            continue
    return listed


def skill_view(home: Home, name: str) -> str | None:
    text = skill_text(home, name)
    return _parse(name, text).body if text else None


def write_skill(home: Home, skill: Skill, expected: str) -> None:
    home = home.scoped(allow_private=False)
    if skill_text(home, skill.name) != expected:
        raise ValueError("The skill changed since this proposal was created.")
    home._write_regular(_rel(skill.name), skill.render())


def skill_diff(old: str, new: str) -> list[str]:
    return list(unified_diff(old.splitlines(), new.splitlines(), fromfile="Current", tofile="Proposed", lineterm=""))
