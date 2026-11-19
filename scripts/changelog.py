"""Print a Keep a Changelog section built from conventional-commit subjects.

    uv run python scripts/changelog.py v0.7.0 HEAD                     # an [Unreleased] section
    uv run python scripts/changelog.py v0.7.0 HEAD --title "[0.8.0] - 2026-12-01"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

SECTIONS = {"feat": "Added", "fix": "Fixed"}
CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?!?: (?P<text>.+)$")
TRAILER = re.compile(r"\s*co-authored-by:.*$", re.IGNORECASE)
SEPARATOR = "\x1e"


def subject(message: str) -> str:
    """First non-trailer line of a commit message, without any Co-Authored-By text."""
    for line in message.splitlines():
        line = TRAILER.sub("", line).strip()
        if line:
            return line
    return ""


def entry(line: str) -> tuple[str, str]:
    """Return the changelog heading and bullet text for one commit subject."""
    match = CONVENTIONAL.match(line)
    if match is None or match["type"] not in SECTIONS:
        return "Other", line
    scope = f"**{match['scope']}:** " if match["scope"] else ""
    return SECTIONS[match["type"]], f"{scope}{match['text']}"


def build_section(title: str, subjects: list[str]) -> str:
    groups: dict[str, list[str]] = {"Added": [], "Fixed": [], "Other": []}
    for line in subjects:
        heading, text = entry(line)
        groups[heading].append(f"- {text}")
    parts = [f"## {title}\n"]
    parts += [f"### {heading}\n\n" + "\n".join(bullets) + "\n" for heading, bullets in groups.items() if bullets]
    return "\n".join(parts)


def commit_subjects(start: str, end: str) -> list[str]:
    log = subprocess.check_output(
        ["git", "log", "--no-merges", f"--format=%B{SEPARATOR}", f"{start}..{end}"], text=True,
    )
    return [line for message in log.split(SEPARATOR) if (line := subject(message))]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start", help="exclusive start ref, usually the previous release tag")
    parser.add_argument("end", help="inclusive end ref, e.g. HEAD")
    parser.add_argument("--title", default="[Unreleased]", help='heading text, e.g. "[0.8.0] - 2026-12-01"')
    args = parser.parse_args(argv)
    sys.stdout.write(build_section(args.title, commit_subjects(args.start, args.end)))


if __name__ == "__main__":
    main()
