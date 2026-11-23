"""The final result of a turn: what Dom does in the chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

if TYPE_CHECKING:
    from dom.telegram.format import TextChunk

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


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, as Telegram counts message length."""
    return len(text.encode("utf-16-le")) // 2


class Bubble(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    reply_to: int | None = None
    delete_after: float | None = Field(default=None, ge=1, le=30)

    # An in-memory continuation after Telegram migrates a group mid-reply.
    _telegram_chunk: TextChunk | None = PrivateAttr(default=None)


# Video renders slowly (about 10 s per second of speech for an everyday note, about 36 s for a special one), so its
# text is capped to what finishes inside the studio's deadlines (180 s and 420 s). The studio enforces the same caps.
VIDEO_TEXT_MAX = 200
SPECIAL_TEXT_MAX = 120


class VideoNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=VIDEO_TEXT_MAX, strict=True)
    special: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _special_is_shorter(self) -> VideoNote:
        if self.special and len(self.text) > SPECIAL_TEXT_MAX:
            raise ValueError(f"a special video note's text is at most {SPECIAL_TEXT_MAX} characters")
        return self


MAX_BUBBLES = 6


class Actions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_response: bool = False
    private: bool = Field(default=False, strict=True)
    react: str | None = None
    sticker: str | None = Field(default=None, min_length=1, max_length=64, strict=True)
    gif: str | None = Field(default=None, min_length=1, max_length=50, strict=True)
    voice: str | None = Field(default=None, min_length=1, max_length=600, strict=True)
    video_note: VideoNote | None = None
    image: str | None = Field(default=None, min_length=1, max_length=300, strict=True)
    outfit: str | None = Field(default=None, min_length=1, max_length=64, strict=True)
    bubbles: list[Bubble] = Field(default_factory=list)
    elsewhere: str | None = None

    @field_validator("elsewhere", mode="before")
    @classmethod
    def _optional_elsewhere(cls, value: object) -> object:
        """The elsewhere line is optional: a wrongly typed one is dropped instead of costing the reply a repair round."""
        return value if isinstance(value, str) or value is None else None

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
        allowed bubble. Telegram renders and splits each logical bubble at delivery time."""
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
        return value

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
