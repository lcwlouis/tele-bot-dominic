"""The final result of a turn: what Dom does in the chat."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
