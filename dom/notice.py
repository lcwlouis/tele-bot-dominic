"""House style for Dom's system notices: messages that code composes, not his in-character chat.

A notice is a bold lead line (what happened), at most two short detail lines or three short bullets, an optional
italic tag (for example "web summary, unchecked") and, when `web.public_url` is a safe https address, a
"Details on the dashboard" button that deep-links to the item. Formatting is built as Telegram entities, never
parsed from text, so nothing in a detail line can turn into markup or a link. Text is shortened at a word
boundary with "…", never mid-word.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MessageEntity

from dom.actions import utf16_len

LEAD_LIMIT = 60
DETAIL_LIMIT = 80
MAX_DETAILS = 2
MAX_BULLETS = 3
DETAILS_LABEL = "Details on the dashboard"
REVIEW_LABEL = "Review on dashboard"
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=\S)")
_LEADING_LABEL = re.compile(r"^(?:done|summary|result|finished|complete[d]?)\s*[.:\-–—]\s*", re.IGNORECASE)


def shorten(text: str, limit: int) -> str:
    """Whitespace collapsed; cut at a word boundary with "…" if longer than `limit` characters."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if text[limit - 1].isspace():  # the cut fell exactly at the end of a word
        return cut.rstrip() + "…"
    head, space, _ = cut.rpartition(" ")
    # A single unbroken word longer than the limit has no boundary to cut at: keep its start.
    return (head if space and head else cut).rstrip(" ,;:.-–—") + "…"


def first_sentence(text: str, limit: int = DETAIL_LIMIT) -> str:
    """The first sentence of `text`, with a leading "Done:" style label removed, shortened to `limit`."""
    text = " ".join(str(text).split())
    text = _LEADING_LABEL.sub("", text)
    return shorten(_SENTENCE.split(text, maxsplit=1)[0], limit)


def sentences(text: str, count: int = MAX_DETAILS, limit: int = DETAIL_LIMIT) -> list[str]:
    """The first `count` sentences of `text`, each shortened to `limit`: detail lines for a free-text message."""
    text = " ".join(str(text).split())
    return [shorten(part, limit) for part in _SENTENCE.split(text)[:count] if part]


def dashboard_url(base: str, path: str) -> str | None:
    """`base` + `path` when base is a plain https address (no credentials, query or fragment), else None."""
    base = base.strip().rstrip("/")
    url = urlsplit(base)
    if url.scheme == "https" and url.netloc and url.username is None and not (url.query or url.fragment):
        return base + path
    return None


def link_button(app, path: str | None, label: str = DETAILS_LABEL) -> InlineKeyboardButton | None:
    """A URL button to the dashboard `path`, or None without a safe `web.public_url` (or without a path)."""
    url = dashboard_url(app.config.web.public_url, path) if path else None
    return InlineKeyboardButton(text=label, url=url) if url else None


def compose(
    lead: str, details: Sequence[str] = (), *, tag: str | None = None, bullets: bool = False,
) -> tuple[str, list[MessageEntity]]:
    """The notice text with its bold lead and italic tag as entities (UTF-16 offsets, as Telegram counts)."""
    lead = shorten(lead, LEAD_LIMIT)
    lines = [shorten(item, DETAIL_LIMIT) for item in details if item and item.strip()]
    lines = [f"• {line}" for line in lines[:MAX_BULLETS]] if bullets else lines[:MAX_DETAILS]
    text = "\n".join([lead, *lines])
    entities = [MessageEntity(type="bold", offset=0, length=utf16_len(lead))]
    if tag := shorten(tag or "", DETAIL_LIMIT):
        entities.append(MessageEntity(type="italic", offset=utf16_len(text) + 1, length=utf16_len(tag)))
        text += "\n" + tag
    return text, entities


def markup(
    app, path: str | None = None, *, label: str = DETAILS_LABEL, buttons: Sequence[InlineKeyboardButton] = (),
) -> InlineKeyboardMarkup | None:
    """One row of `buttons`, then the dashboard link on its own row; None when there is nothing to show."""
    rows = [list(buttons)] if buttons else []
    if link := link_button(app, path, label):
        rows.append([link])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_notice(
    app, chat_id: int, lead: str, details: Sequence[str] = (), *, path: str | None = None, label: str = DETAILS_LABEL,
    tag: str | None = None, bullets: bool = False, buttons: Sequence[InlineKeyboardButton] = (), **options,
):
    """Send one notice. Without a public dashboard address it is the same short text with no link."""
    from dom.telegram.executor import send_chunk  # imported here: the executor's own imports reach modules that send notices

    text, entities = compose(lead, details, tag=tag, bullets=bullets)
    options.setdefault("link_preview_options", LinkPreviewOptions(is_disabled=True))
    options.setdefault("sleep", app.sleep)
    return await send_chunk(
        app.bot, chat_id, (text, entities), reply_markup=markup(app, path, label=label, buttons=buttons), **options,
    )
