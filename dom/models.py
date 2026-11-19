"""Shared data models: chat scopes, stored messages, identities, incoming Telegram messages."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Scope = Literal["owner_dm", "friend_dm", "group"]

# Values for messages.visibility_scope. Per-chat rows use f"chat:{chat_id}".
VISIBILITY_OWNER_PRIVATE = "owner_private"
VISIBILITY_PUBLIC = "global_public"


class StoredMessage(BaseModel):
    id: int | None = None
    chat_id: str
    msg_id: int | None = None
    reply_to_msg_id: int | None = None
    sender_user_id: int | None = None
    sender_handle: str = ""
    sender_name: str = ""
    is_bot: bool = False
    role: Literal["user", "assistant", "system"] = "user"
    visibility_scope: str
    text: str
    media_json: str | None = None
    is_edited: bool = False
    original_seen_before_edit: bool = Field(
        default=False,
        description=(
            "True ONLY if Dom was online and had already read this msg_id before the edit occurred. "
            "If edited while Dom was offline/unread, Dom only sees the final text with edited=True."
        ),
    )
    previous_seen_text: str | None = Field(
        default=None,
        description="Populated for the prompt only when original_seen_before_edit is True.",
    )
    is_deleted: bool = False
    created_at: str
    updated_at: str | None = None


class PersonIdentity(BaseModel):
    person_key: str
    telegram_user_id: int | None = None
    current_handle: str
    known_handles: list[str] = Field(default_factory=list)
    display_name: str
    preferred_name: str = ""
    profile_path: str
    relationship_tag: str = ""


class IncomingMessage(BaseModel):
    chat_id: int
    chat_type: Literal["private", "group", "supergroup"]
    chat_title: str
    msg_id: int
    sender_id: int
    sender_handle: str | None
    sender_name: str
    is_bot: bool
    text: str
    reply_to: int | None
    media: dict | None
    date: datetime
