"""The final result of a turn: what Dom does in the chat."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The emoji Telegram accepts as a bot reaction (Bot API, ReactionTypeEmoji). A test keeps this in
# step with the list in aiogram's documentation of that type.
REACTIONS = (
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌",
    "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓",
    "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡",
    "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀",
    "😡",
)
SEEN = "👀"


TELEGRAM_TEXT_LIMIT = 4096
MAX_BUBBLE_CHARS = TELEGRAM_TEXT_LIMIT

_PARAGRAPH_BREAK = re.compile(r"([ \t]*\n[ \t]*\n\s*)")
_SENTENCE_END = re.compile(r"""(?:[.!?…]["'”’)\]]*(\s+)|([。！？][”’）】」』]*)([ \t]*))""")
_WORD_BREAK = re.compile(r"(\s+)")


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, as Telegram counts message length."""
    return len(text.encode("utf-16-le")) // 2


def _max_prefix_index(text: str, limit: int) -> int:
    """Largest index `i` such that utf16_len(text[:i]) <= limit."""
    units = 0
    for i, ch in enumerate(text):
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > limit:
            return i
        units += w
    return len(text)


def _is_emoji_continuation(ch: str) -> bool:
    code = ord(ch)
    return (
        code == 0x200D  # ZWJ
        or 0xFE00 <= code <= 0xFE0F  # Variation Selectors
        or 0xE0100 <= code <= 0xE01EF  # Variation Selectors Supplement
        or 0x1F3FB <= code <= 0x1F3FF  # Emoji skin-tone modifiers
        or 0xE0020 <= code <= 0xE007F  # Tag characters
        or unicodedata.category(ch) in ("Mn", "Mc", "Me")  # Combining marks (incl. keycap U+20E3)
    )


def _safe_hard_cut(text: str, max_end: int) -> int:
    """Back off from `max_end` so a hard split never cuts inside an emoji or combining sequence."""
    cut = max_end
    while cut > 0 and cut < len(text):
        prev, nxt = text[cut - 1], text[cut]
        if prev == "\u200d" or _is_emoji_continuation(nxt):
            cut -= 1
            continue
        if 0x1F1E6 <= ord(prev) <= 0x1F1FF and 0x1F1E6 <= ord(nxt) <= 0x1F1FF:
            run = 0
            i = cut - 1
            while i >= 0 and 0x1F1E6 <= ord(text[i]) <= 0x1F1FF:
                run += 1
                i -= 1
            if run % 2 == 1:
                cut -= 1
                continue
        break
    return cut if cut > 0 else max_end


def _find_split(text: str, max_end: int) -> tuple[int, int]:
    """Return (cut, resume) with cut <= max_end: paragraph, then sentence, then word, then hard boundary."""
    probe = text[: max_end + 32]

    best: tuple[int, int] | None = None
    for match in _PARAGRAPH_BREAK.finditer(probe):
        start, end = match.span(1)
        if start > max_end:
            break
        if start > 0 and text[:start].strip():
            best = (start, end)
    if best is not None:
        return best

    for match in _SENTENCE_END.finditer(probe):
        if match.group(1) is not None:
            cut, resume = match.span(1)
        else:
            cut, resume = match.end(2), match.end(3)
        if cut > max_end:
            break
        if cut > 0 and text[:cut].strip():
            best = (cut, resume)
    if best is not None:
        return best

    for match in _WORD_BREAK.finditer(probe):
        start, end = match.span(1)
        if start > max_end:
            break
        if start > 0 and text[:start].strip():
            best = (start, end)
    if best is not None:
        return best

    safe = _safe_hard_cut(text, max_end)
    return safe, safe


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split text into chunks of at most `limit` UTF-16 code units at paragraph, sentence, then word boundaries."""
    if utf16_len(text) <= limit or not text.strip():
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if utf16_len(remaining) <= limit:
            chunks.append(remaining)
            break
        max_end = _max_prefix_index(remaining, limit) or 1
        cut, resume = _find_split(remaining, max_end)
        chunk = remaining[:cut].rstrip() if resume > cut else remaining[:cut]
        if chunk:
            chunks.append(chunk)
        next_remaining = remaining[resume:].lstrip() if resume > cut else remaining[resume:]
        if len(next_remaining) >= len(remaining):
            next_remaining = remaining[max_end:]
        remaining = next_remaining
    return chunks


class Bubble(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    reply_to: int | None = None
    delete_after: float | None = Field(default=None, ge=1, le=30)

    @field_validator("text")
    @classmethod
    def _fits_telegram(cls, value: str) -> str:
        if utf16_len(value) > TELEGRAM_TEXT_LIMIT:
            raise ValueError(f"bubble text exceeds Telegram's {TELEGRAM_TEXT_LIMIT} UTF-16 code unit limit")
        return value


MAX_BUBBLES = 6


class Actions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_response: bool = False
    private: bool = Field(default=False, strict=True)
    react: str | None = None
    sticker: str | None = Field(default=None, min_length=1, max_length=64, strict=True)
    gif: str | None = Field(default=None, min_length=1, max_length=50, strict=True)
    bubbles: list[Bubble] = Field(default_factory=list)
    elsewhere: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _private_in_bubbles(cls, data: object) -> object:
        """Models put "private": true inside each bubble instead of next to them. Privacy covers the whole answer, so
        any bubble marked private makes all of it private; the key is then dropped from the bubbles."""
        if not isinstance(data, dict) or not isinstance(data.get("bubbles"), list):
            return data
        bubbles = data["bubbles"]
        if not any(isinstance(b, dict) and "private" in b for b in bubbles):
            return data
        marked = any(isinstance(b, dict) and b.get("private") is True for b in bubbles)
        cleaned = [{k: v for k, v in b.items() if k != "private"} if isinstance(b, dict) else b for b in bubbles]
        return {**data, "bubbles": cleaned, **({"private": True} if marked else {})}

    @field_validator("bubbles", mode="before")
    @classmethod
    def _fold_extra_bubbles(cls, value: object) -> object:
        """A bubble written as a bare string is read as its text. Extra bubbles beyond MAX_BUBBLES join the last
        allowed bubble, and any bubble exceeding Telegram's 4096 UTF-16 code unit limit is split at paragraph,
        sentence, then word boundaries instead of failing validation."""
        if not isinstance(value, list):
            return value
        # Models sometimes write a bubble as a bare string; it means the same as {"text": ...}.
        value = [{"text": item} if isinstance(item, str) else item for item in value]
        if len(value) > MAX_BUBBLES:
            tail = value[MAX_BUBBLES - 1:]
            if not all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in tail):
                raise ValueError(f"at most {MAX_BUBBLES} bubbles")
            merged = {"text": "\n\n".join(item["text"].strip() for item in tail if item["text"].strip())}
            value = [*value[: MAX_BUBBLES - 1], merged]
        expanded = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and utf16_len(item["text"]) > TELEGRAM_TEXT_LIMIT:
                parts = split_text(item["text"], TELEGRAM_TEXT_LIMIT)
                for index, part in enumerate(parts):
                    piece = {**item, "text": part}
                    if index > 0 and "reply_to" in piece:
                        piece["reply_to"] = None
                    expanded.append(piece)
            else:
                expanded.append(item)
        return expanded

    @field_validator("react", mode="before")
    @classmethod
    def _allowed_reaction(cls, value: object) -> str | None:
        """Telegram rejects any other emoji, so it becomes no reaction rather than a failed API call."""
        if not isinstance(value, str):
            return None
        emoji = value.strip().replace("️", "")  # models write "❤️"; Telegram lists "❤"
        return emoji if emoji in REACTIONS else None


@dataclass(frozen=True)
class SendFile:
    """A desktop result attachment prepared by the host, outside chat model actions."""

    filename: str
    data: bytes
