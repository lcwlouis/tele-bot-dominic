"""aiogram Message -> IncomingMessage."""

from __future__ import annotations

from aiogram.types import Message

from dom.media import DEFAULT_MIME
from dom.models import IncomingMessage

_MEDIA_KINDS = ("photo", "voice", "video", "video_note", "audio", "animation", "sticker", "document")


def _media(message: Message) -> dict | None:
    for kind in _MEDIA_KINDS:
        value = getattr(message, kind, None)
        if not value:
            continue
        item = value
        if kind == "photo":
            item = max(value, key=lambda photo: (photo.width * photo.height, photo.file_size or 0))
        mime = getattr(item, "mime_type", None) or DEFAULT_MIME.get(kind, "application/octet-stream")
        media = {"kind": kind, "file_id": item.file_id, "file_size": item.file_size, "mime": mime}
        if kind == "sticker":
            if item.is_animated:
                media["mime"] = "application/x-tgsticker"
            elif item.is_video:
                media["mime"] = "video/webm"
            if (item.is_animated or item.is_video) and item.thumbnail:
                media["preview_file_id"] = item.thumbnail.file_id
            media["emoji"] = item.emoji
        return media
    return None


def to_incoming(message: Message) -> IncomingMessage:
    user = message.from_user
    chat = message.chat
    if message.sender_chat is not None:  # sent on behalf of a chat (anonymous admin, channel post)
        # Telegram still fills from_user with a placeholder bot: ignore it.
        sender = message.sender_chat
        sender_id, sender_name, username, is_bot = sender.id, sender.title or "", None, False
    elif user is not None:
        sender_id, sender_name, username, is_bot = user.id, user.full_name, user.username, user.is_bot
    else:
        sender_id, sender_name, username, is_bot = chat.id, chat.title or "", None, False
    return IncomingMessage(
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat.title or "",
        msg_id=message.message_id,
        sender_id=sender_id,
        sender_handle=f"@{username}" if username else None,
        sender_name=sender_name,
        is_bot=is_bot,
        text=message.text or message.caption or "",
        reply_to=message.reply_to_message.message_id if message.reply_to_message else None,
        media=_media(message),
        date=message.date,
        thread_id=message.message_thread_id if message.is_topic_message else None,
    )
