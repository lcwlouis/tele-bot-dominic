"""Turn an owner's text export into private memory proposals using a line parser."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from importlib import resources
from io import BytesIO
from pathlib import PurePath
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


def parse_facts(text: str, *, existing: Iterable[str] = ()) -> list[str]:
    seen = {_key(fact) for fact in existing}
    facts = []
    for line in text.splitlines():
        fact = _fact(line)
        if not fact or len(fact) > MAX_FACT_LENGTH:
            continue
        key = _key(fact)
        if key in seen:
            continue
        seen.add(key)
        facts.append(fact)
        if len(facts) >= MAX_FACTS:
            break
    return facts


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

    async def run(self, service: str, text: str) -> int:
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
            facts = parse_facts(text, existing=seen)
            for fact in facts:
                await app.proposals.create(
                    "memory_fact", fact, {"file": "USER.md", "text": fact}, f"import:{service}", notify=False,
                )
        return len(facts)
