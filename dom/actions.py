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


MAX_BUBBLES = 6


class Actions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_response: bool = False
    private: bool = Field(default=False, strict=True)
    react: str | None = None
    sticker: str | None = Field(default=None, min_length=1, max_length=64, strict=True)
    gif: str | None = Field(default=None, min_length=1, max_length=50, strict=True)
    bubbles: list[Bubble] = Field(default_factory=list, max_length=MAX_BUBBLES)

    @field_validator("bubbles", mode="before")
    @classmethod
    def _fold_extra_bubbles(cls, value: object) -> object:
        """A bubble written as a bare string is read as its text. A long answer (an itinerary, say) can come back as
        more bubbles than Dom sends. Rather than throw the whole reply away, the extra ones join the last allowed
        bubble; a bubble that is then too long still fails."""
        if not isinstance(value, list):
            return value
        # Models sometimes write a bubble as a bare string; it means the same as {"text": ...}.
        value = [{"text": item} if isinstance(item, str) else item for item in value]
        if len(value) <= MAX_BUBBLES:
            return value
        tail = value[MAX_BUBBLES - 1:]
        if not all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in tail):
            return value
        merged = {"text": "\n\n".join(item["text"].strip() for item in tail if item["text"].strip())}
        return [*value[: MAX_BUBBLES - 1], merged]

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
