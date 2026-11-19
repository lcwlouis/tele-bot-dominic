"""Download and describe Telegram attachments once on arrival."""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable

from dom.llm import ChatClient

log = logging.getLogger("dom.media")
MAX_FILE_SIZE = 20_000_000
PROMPT = (
    "Describe this for a friend reading a group chat: what it shows/says, any text, the vibe. "
    "Voice/audio: transcribe, then one line on tone. Max 80 words."
)

DEFAULT_MIME = {
    "photo": "image/jpeg",
    "sticker": "image/webp",
    "voice": "audio/ogg",
    "audio": "audio/mpeg",
    "video": "video/mp4",
    "animation": "video/mp4",
    "video_note": "video/mp4",
}
_PART_TYPES = {
    "photo": "image_url",
    "sticker": "image_url",
    "voice": "input_audio",
    "audio": "input_audio",
    "video": "video_url",
    "animation": "video_url",
    "video_note": "video_url",
}
_AUDIO_FORMATS = {"mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "x-wav": "wav"}


class MediaDescriber:
    def __init__(self, client: ChatClient, bot):
        self.client, self.bot = client, bot

    async def describe(
        self, media: dict, *, authorize: Callable[[], Awaitable[bool]] | None = None,
    ) -> str | None:
        kind = media.get("kind")
        try:
            part_type = _PART_TYPES.get(kind)
            size = media.get("file_size")
            if part_type is None or (size is not None and size > MAX_FILE_SIZE):
                return None
            mime = (media.get("mime") or DEFAULT_MIME[kind]).partition(";")[0].strip().lower()
            file_id = media["file_id"]
            if kind == "sticker" and mime != DEFAULT_MIME["sticker"]:
                file_id = media.get("preview_file_id")
                if not file_id:
                    raise ValueError("animated sticker has no image preview")
                mime = "image/jpeg"
            downloaded = await self.bot.download(file_id)
            if downloaded is None:
                raise ValueError("no attachment returned")
            with downloaded:
                data = downloaded.read(MAX_FILE_SIZE + 1)
            if len(data) > MAX_FILE_SIZE:
                return None
            if not data:
                raise ValueError("empty attachment")
            if authorize is not None and not await authorize():
                return None

            encoded = base64.b64encode(data).decode("ascii")
            if part_type == "input_audio":
                subtype = mime.partition("/")[2]
                part = {"data": encoded, "format": _AUDIO_FORMATS.get(subtype, subtype)}
            else:
                part = {"url": f"data:{mime};base64,{encoded}"}
            messages = [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": part_type, part_type: part},
            ]}]
            result = await self.client.complete(messages, [])
            description = result.get("content")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("no description returned")
            return description.strip()
        except Exception as exc:
            log.warning("media description failed for %s (%s)", kind, type(exc).__name__)
            return None
