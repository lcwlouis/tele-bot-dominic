"""Turn an owner's text export into private memory proposals using a line parser."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from io import BytesIO
from pathlib import PurePath
from uuid import uuid4
from typing import TYPE_CHECKING

from aiogram.types import Document

from dom.proposals import MEMORY_FILES

if TYPE_CHECKING:
    from dom.telegram.handlers import App

SERVICES = ("chatgpt", "claude", "muse")
MAX_FACTS = 300
MAX_FACT_LENGTH = 300
MAX_DOCUMENT_BYTES = 1024 * 1024
_DOCUMENT_LIMIT_ERROR = "Import documents must be 1 MB or smaller."
_BULLET = re.compile(r"\s*-[ \t]+(?:\[[^\]\r\n]+\](?:[ \t]+|$))?(.*)")


def _fact(line: str) -> str | None:
    match = _BULLET.fullmatch(line)
    return match[1].strip() if match else None


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class ParsedFacts:
    facts: list[str]
    too_long: int = 0  # bullets over MAX_FACT_LENGTH
    duplicates: int = 0  # bullets already pending, already in a memory file or repeated in the export


def parse_facts(text: str, *, existing: Iterable[str] = ()) -> ParsedFacts:
    seen = {_key(fact) for fact in existing}
    facts, too_long, duplicates = [], 0, 0
    for line in text.splitlines():
        fact = _fact(line)
        if not fact:
            continue
        if len(fact) > MAX_FACT_LENGTH:
            too_long += 1
            continue
        key = _key(fact)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        facts.append(fact)
        if len(facts) >= MAX_FACTS:
            break
    return ParsedFacts(facts, too_long, duplicates)


def summary(service: str, parsed: ParsedFacts) -> str:
    """The owner's DM after an import: what was imported, what was skipped and why only some facts will fit."""
    text = f"Imported {len(parsed.facts)} facts from {service}."
    skipped = []
    if parsed.duplicates:
        skipped.append(f"{parsed.duplicates} duplicate{'s' if parsed.duplicates != 1 else ''}")
    if parsed.too_long:
        skipped.append(f"{parsed.too_long} over {MAX_FACT_LENGTH} characters")
    if skipped:
        text += f" Skipped {' and '.join(skipped)}."
    text += " Review them in the dashboard or with /proposals."
    if parsed.facts:
        text += (
            " USER.md holds about 1,375 characters, so only the most important facts fit; "
            "Dom's notes pass keeps it trimmed."
        )
    return text


def export_prompt() -> str:
    return resources.files("dom").joinpath("starter_home", "EXPORT_PROMPT.md").read_text(encoding="utf-8")


class _DocumentTooLarge(ValueError):
    pass


class _LimitedBuffer(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_DOCUMENT_BYTES:
            raise _DocumentTooLarge(_DOCUMENT_LIMIT_ERROR)
        return super().write(data)


async def download_text(bot, document: Document) -> str:
    if document.file_size is not None and document.file_size > MAX_DOCUMENT_BYTES:
        raise ValueError(_DOCUMENT_LIMIT_ERROR)
    if PurePath(document.file_name or "").suffix.lower() not in (".txt", ".md"):
        raise ValueError("Send a .txt or .md document for the import.")
    with _LimitedBuffer() as destination:
        try:
            await bot.download(document, destination=destination)
        except _DocumentTooLarge:
            raise
        except Exception:
            raise ValueError("Could not download the import document.") from None
        try:
            return destination.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("The import document must contain UTF-8 text.") from None


class MemoryImport:
    def __init__(self, app: App):
        self.app = app
        self._lock = asyncio.Lock()

    async def run(self, service: str, text: str) -> ParsedFacts:
        app = self.app
        if service not in SERVICES or app.owner_id is None:
            raise ValueError("Memory imports need an owner and a supported service.")
        async with self._lock:
            seen = set()
            for row in await app.proposals.pending():
                if row["kind"] == "memory_fact":
                    body = json.loads(row["body_json"])
                    if body["file"] == "USER.md":
                        seen.add(body["text"])
            home = app.home.scoped(allow_private=True)
            for rel in MEMORY_FILES.values():
                for line in home.read(rel).splitlines():
                    bullet = _fact(line)
                    seen.add(line if bullet is None else bullet)
            parsed = parse_facts(text, existing=seen)
            batch = uuid4().hex
            for fact in parsed.facts:
                await app.proposals.create(
                    "memory_fact", fact, {"file": "USER.md", "text": fact, "import_batch": batch}, f"import:{service}", notify=False,
                )
        return parsed
