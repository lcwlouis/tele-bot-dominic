"""The final result of a turn: what Dom does in the chat."""

from __future__ import annotations

import re

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
SCENE_MAX = 200


class VideoNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=VIDEO_TEXT_MAX, strict=True)
    special: bool = Field(default=False, strict=True)
    scene: str = Field(default="", max_length=SCENE_MAX, strict=True)  # where Dom is; the note is filmed there

    @model_validator(mode="after")
    def _special_is_shorter(self) -> VideoNote:
        if self.special and len(self.text) > SPECIAL_TEXT_MAX:
            raise ValueError(f"a special video note's text is at most {SPECIAL_TEXT_MAX} characters")
        return self


MAX_BUBBLES = 6
# How Dom's own media and Telegram media read in the chat window: a "[kind]" label on his sent media and a
# <media kind=...> tag on any message with media.
_FAKE_MEDIA = re.compile(
    r"<media\s+kind=|</media>|^\s*\[(?:photo|image|picture|voice|voice note|video|video note|video_note|special|"
    r"sticker|gif|animation|audio|document)\](?!\()",
    re.IGNORECASE | re.MULTILINE)


_MEDIA_LABEL = re.compile(
    r"\s*\[(photo|image|picture|voice|voice note|video|video note|video_note)\](?!\()\s*", re.IGNORECASE)
_MEDIA_TAG = re.compile(r"<media\b[^>]*>.*?</media>|<media\b[^>]*/?>", re.IGNORECASE | re.DOTALL)


def fake_media(text: str) -> bool:
    """Whether a bubble's text imitates a media message instead of being something to say."""
    return bool(_FAKE_MEDIA.search(text))


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

    # The media field set from a bubble written as text (see _media_written_as_text), else None. Such media goes out
    # only if its kind was offered on the turn, and never falls back to text.
    _inferred: str | None = PrivateAttr(default=None)

    @property
    def inferred_media(self) -> str | None:
        return self._inferred

    @model_validator(mode="wrap")
    @classmethod
    def _media_written_as_text(cls, data: object, handler):
        """Models copy how media reads in the chat window ("[photo] a scene", <media kind="photo">) instead of setting
        the action field. The intent is plain, so the first such bubble becomes the action when no media field is set.
        A bubble set to delete itself stays text (dropped by _fold_extra_bubbles), since media can't."""
        inferred = None
        if (isinstance(data, dict) and isinstance(data.get("bubbles"), list)
                and not any(data.get(field) for field in ("voice", "video_note", "image"))):
            for item in data["bubbles"]:
                if isinstance(item, dict) and item.get("delete_after") is not None:
                    continue
                text = item if isinstance(item, str) else item.get("text") if isinstance(item, dict) else None
                match = _MEDIA_LABEL.match(text) if isinstance(text, str) else None
                content = _MEDIA_TAG.sub("", text[match.end():]).strip() if match else ""
                if not content:
                    continue
                kind = match.group(1).lower().replace("_", " ")
                if kind in ("photo", "image", "picture"):
                    inferred, data = "image", {**data, "image": content[:300]}
                elif kind in ("voice", "voice note"):
                    inferred, data = "voice", {**data, "voice": content[:600]}
                elif kind in ("video note", "video"):
                    inferred, data = "video_note", {**data, "video_note": {"text": content[:VIDEO_TEXT_MAX]}}
                break
        actions = handler(data)
        actions._inferred = inferred
        return actions

    def without_inferred_media(self) -> Actions:
        """These actions without the media field inferred from a bubble."""
        if self._inferred is None:
            return self
        return self.model_copy(update={self._inferred: None})

    @field_validator("bubbles", mode="before")
    @classmethod
    def _fold_extra_bubbles(cls, value: object) -> object:
        """A bubble written as a bare string is read as its text. Extra bubbles beyond MAX_BUBBLES join the last
        allowed bubble. Telegram renders and splits each logical bubble at delivery time."""
        if not isinstance(value, list):
            return value
        # Models sometimes write a bubble as a bare string; it means the same as {"text": ...}.
        value = [{"text": item} if isinstance(item, str) else item for item in value]
        # A bubble that imitates how media looks in the chat window ("[photo] ...", <media kind=...>) stands in for
        # media that was never made: drop it rather than send machinery as text.
        value = [item for item in value
                 if not (isinstance(item, dict) and isinstance(item.get("text"), str) and fake_media(item["text"]))]
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
