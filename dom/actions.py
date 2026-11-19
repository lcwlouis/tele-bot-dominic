"""The final result of a turn: what Dom does in the chat."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Bubble(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reply_to: int | None = None
    delete_after: float | None = Field(default=None, ge=1, le=30)


class Actions(BaseModel):
    no_response: bool = False
    react: str | None = None
    bubbles: list[Bubble] = Field(default_factory=list, max_length=6)
