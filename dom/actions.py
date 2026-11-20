"""The final result of a turn: what Dom does in the chat."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class Bubble(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    reply_to: int | None = None
    delete_after: float | None = Field(default=None, ge=1, le=30)


class Actions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_response: bool = False
    private: bool = Field(default=False, strict=True)
    react: str | None = None
    sticker: str | None = Field(default=None, min_length=1, max_length=64, strict=True)
    bubbles: list[Bubble] = Field(default_factory=list, max_length=6)

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
