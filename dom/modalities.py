"""What each chat model accepts as input, and how the real photo, voice note or video reaches it.

A model's input types come from, in order:
1. `input = [...]` on its route in config.toml;
2. the provider's model list (OpenRouter's `architecture.input_modalities`), cached by dom.model_lists;
3. a one-off probe for OpenAI-compatible servers that list nothing: a tiny generated image, and a sensible answer
   counts as support. The result is cached per base URL and model in the kv table.
Anything else is text-only. Learning (list fetch, probe) runs in the background or on the owner's request, never on
the reply path.

A prompt carries media as `attachments` on its user message: small JSON-able stubs that hold the bytes. The chat
client turns them into content parts after the text (so the cacheable prefix is unchanged) when its model takes the
type; for a model that doesn't, or one that refuses the request, each attachment becomes its description from the
media route.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import time
import zlib
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from dom import model_lists, turn_log

if TYPE_CHECKING:
    from dom.config import ProviderEndpoint

log = logging.getLogger("dom.modalities")

MODALITIES = ("image", "audio", "video")
# What a stored attachment's kind is made of, as input to a model.
MODALITY_OF = {
    "photo": "image", "sticker": "image", "voice": "audio", "audio": "audio",
    "video": "video", "animation": "video", "video_note": "video",
}
_PART_TYPES = {"image": "image_url", "audio": "input_audio", "video": "video_url"}
_AUDIO_FORMATS = {"mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "x-wav": "wav"}
PROBE_PROMPT = "What colour is this image? Answer with one word."
PROBE_TOKENS = 400  # room for a reasoning model to think before its one word


class Attachment(dict):
    """A photo, recording or clip for one prompt. As JSON it is a small stub (kind, size, reference), so hashing or
    logging a prompt never touches the bytes; `data` holds them until the request is built."""

    def __init__(
        self, modality: str, mime: str, data: bytes, ref: str,
        describe: Callable[[], Awaitable[str | None]] | None = None,
    ):
        super().__init__(modality=modality, mime=mime, bytes=len(data), ref=ref)
        self.data = data
        self.describe = describe  # the media route's text for it, for a model that can't take the real thing


def part(attachment: Attachment) -> dict:
    """The OpenAI-style content part carrying the attachment."""
    modality, mime = attachment["modality"], attachment["mime"]
    encoded = base64.b64encode(attachment.data).decode("ascii")
    if modality == "audio":
        subtype = mime.partition("/")[2]
        return {"type": "input_audio", "input_audio": {"data": encoded, "format": _AUDIO_FORMATS.get(subtype, subtype)}}
    kind = _PART_TYPES[modality]
    return {"type": kind, kind: {"url": f"data:{mime};base64,{encoded}"}}


async def _described(attachments: Iterable[Attachment]) -> str:
    lines = []
    for item in attachments:
        text = None
        if item.describe is not None:
            try:
                text = await item.describe()
            except Exception:
                log.warning("could not describe an attachment", exc_info=True)
        lines.append(f"[{item['ref']}] {text}" if text else f"[{item['ref']}] could not be shown to you")
    return "\n\n<attached_media>\n" + "\n".join(lines) + "\n</attached_media>"


async def prepare(messages: list[dict], accepts: frozenset[str]) -> tuple[list[dict], bool]:
    """Messages as they go on the wire, and whether any real media went with them. An attachment the model takes
    becomes content parts after the message text; one it doesn't becomes its description in that text."""
    out, native, described = [], False, False
    for message in messages:
        attachments = message.get("attachments")
        if not attachments:
            out.append(message)
            continue
        text = message["content"]
        keep = [item for item in attachments if item["modality"] in accepts]
        drop = [item for item in attachments if item["modality"] not in accepts]
        if drop:
            text += await _described(drop)
            described = True
        wired = {key: value for key, value in message.items() if key != "attachments"}
        if keep:
            native = True
            parts: list[dict] = [{"type": "text", "text": text}]
            for item in keep:
                parts += [{"type": "text", "text": f"[{item['ref']}]"}, part(item)]
            wired["content"] = parts
        else:
            wired["content"] = text
        out.append(wired)
    if described:
        turn_log.step("media described")
    if native:
        turn_log.step("media sent as is")
    return out, native


def has_attachments(messages: list[dict]) -> bool:
    return any(message.get("attachments") for message in messages)


def test_image() -> bytes:
    """A 32x32 solid red PNG, made here so the probe needs no file."""
    width = height = 32
    row = b"\x00" + b"\xff\x00\x00" * width

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


async def probe_image(client) -> bool | None:
    """Ask the model what colour a generated image is. True when it says red, False when it answers otherwise or the
    server refuses an image; None when nothing could be learned (the server is down), so nothing is cached."""
    data = base64.b64encode(test_image()).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROBE_PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
    ]}]
    try:
        reply = await client.complete(messages, [], once=True, max_tokens=PROBE_TOKENS, timeout=60)
    except httpx.HTTPStatusError as exc:
        return False if 400 <= exc.response.status_code < 500 and exc.response.status_code not in (401, 402, 403, 408, 429) else None
    except Exception:
        return None
    content = reply.get("content")
    return isinstance(content, str) and "red" in content.lower()


def _probe_key(base_url: str, model: str) -> str:
    return "modalities:probe:" + hashlib.sha256(f"{base_url.rstrip('/')}\n{model}".encode()).hexdigest()


class Modalities:
    """Which of image, audio and video a chat endpoint takes. Answers come from memory when they can: the model
    list is large and the check happens on every turn."""

    TTL = 600  # seconds an answer is trusted before the cached list or probe is read again
    PROBE_TTL = 1800  # seconds a home/OpenAI-compatible probe stays fresh in kv before re-checking in the background

    def __init__(
        self,
        store,
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store = store
        self._clock = now
        self._wall = wall
        self._known: dict[tuple, tuple[float, frozenset[str] | None]] = {}

    @staticmethod
    def _identity(endpoint: ProviderEndpoint) -> tuple:
        return (endpoint.base_url.rstrip("/"), endpoint.model, endpoint.api_key_env, endpoint.input)

    async def accepts(self, endpoint: ProviderEndpoint) -> frozenset[str]:
        found = await self.lookup(endpoint)
        return found if found is not None else frozenset()

    async def lookup(self, endpoint: ProviderEndpoint) -> frozenset[str] | None:
        """The input types (image, audio, video) the endpoint takes, or None when nothing is known yet."""
        key = self._identity(endpoint)
        now = self._clock()
        cached = self._known.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]
        answer, ttl = await self._read(endpoint)
        self._known[key] = (now + min(self.TTL, ttl), answer)
        return answer

    async def _read(self, endpoint: ProviderEndpoint) -> tuple[frozenset[str] | None, float]:
        if endpoint.input is not None:
            return frozenset(endpoint.input) & frozenset(MODALITIES), float(self.TTL)
        listed = await model_lists.cached(self.store, endpoint.base_url, endpoint.api_key_env, fresh_only=False)
        entry = next((item for item in listed.models if item.id == endpoint.model), None) if listed else None
        if entry is not None and entry.input is not None:
            return frozenset(entry.input) & frozenset(MODALITIES), float(self.TTL)
        raw = await self.store.kv_get(_probe_key(endpoint.base_url, endpoint.model))
        if raw:
            try:
                data = json.loads(raw)
                stamp = data.get("at")
                if not isinstance(stamp, str):
                    return None, float(self.TTL)
                remaining = self.PROBE_TTL - (self._wall() - datetime.fromisoformat(stamp)).total_seconds()
                if remaining <= 0:
                    return None, float(self.TTL)
                found = {kind for kind in MODALITIES if data.get(kind)}
                return frozenset(found), remaining
            except (ValueError, AttributeError, TypeError):
                return None, float(self.TTL)
        return None, float(self.TTL)

    def forget(self, endpoint: ProviderEndpoint | None = None) -> None:
        if endpoint is None:
            self._known.clear()
        else:
            self._known.pop(self._identity(endpoint), None)

    async def reset(self, endpoint: ProviderEndpoint) -> None:
        """Drop the cached probe result, so the next probe decides again."""
        await self.store.kv_delete(_probe_key(endpoint.base_url, endpoint.model))
        self.forget(endpoint)

    async def remember_probe(
        self, endpoint: ProviderEndpoint, image: bool, *, audio: bool = False, video: bool = False,
    ) -> None:
        stamp = self._wall().isoformat()
        payload = {"image": image, "audio": audio, "video": video, "at": stamp}
        await self.store.kv_set(_probe_key(endpoint.base_url, endpoint.model), json.dumps(payload))
        self.forget(endpoint)

    async def explain(self, endpoint: ProviderEndpoint) -> dict:
        """What is known about the endpoint and where it came from, for the dashboard."""
        accepted = await self.lookup(endpoint)
        if endpoint.input is not None:
            source = "config"
        elif accepted is None:
            source = "unknown"
        else:
            listed = await model_lists.cached(self.store, endpoint.base_url, endpoint.api_key_env, fresh_only=False)
            entry = next((item for item in listed.models if item.id == endpoint.model), None) if listed else None
            source = "list" if entry is not None and entry.input is not None else "probe"
        return {
            "model": endpoint.model, "accepts": sorted(accepted or ()), "source": source,
        }
