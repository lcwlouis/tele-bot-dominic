"""Optional cloud rendering after the local studio has failed a screened job."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from dom.config import Config, StudioFallback, routing_preferences
from dom.studio.client import MAX_MEDIA_BYTES
from dom.studio.policy import Kind, backups as providers
from dom.usage import ModelCallSkipped, UsageRecorder, check_model_allowed, model_request, response_usage

log = logging.getLogger(__name__)
PNG = b"\x89PNG\r\n\x1a\n"
MAX_PORTRAIT_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 3 * 1024 * 1024
# OpenRouter video: a square 480p clip with the model's own audio, polled every few seconds within the kind's deadline.
VIDEO_POLL_SECONDS = 5
VIDEO_RESOLUTION = "480p"
VIDEO_NOTE_SIDE = 640  # Telegram's round video notes are square, at most 640 px
VIDEO_NOTE_SECONDS = 59  # Telegram's limit is 60 s
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def video_seconds(text: str) -> int:
    """A clip long enough to say the line at a natural pace: 4, 6 or 8 s, durations most video models accept."""
    words = len(text.split())
    return 4 if words <= 8 else 6 if words <= 14 else 8


def video_prompt(text: str, reference: bool) -> str:
    who = "The person in the first frame" if reference else "A friendly young man"
    return (f"{who} looks into the camera in a casual selfie video and says, in a natural voice with matching lip "
            f"movement: \"{text}\". No captions, no on-screen text, no music.")


def _portrait(home: Path, outfit: str | None) -> bytes | None:
    if not outfit or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", outfit):
        return None
    folder = home / "studio-portraits"
    path = folder / f"{outfit}.png"
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as file:
            data = file.read(MAX_PORTRAIT_BYTES + 1)
    except OSError:
        return None
    return data if len(PNG) < len(data) <= MAX_PORTRAIT_BYTES and data.startswith(PNG) else None


def speech_pcm(data: bytes) -> bytes:
    """Extract PCM from a WAV envelope. A trailing C2PA chunk is outside the audio data."""
    if data.startswith(b"RIFF"):
        if data[8:12] != b"WAVE":
            raise ValueError("invalid WAV")
        offset = 12
        pcm_format = False
        while offset + 8 <= len(data):
            kind, size = struct.unpack_from("<4sI", data, offset)
            start = offset + 8
            end = start + size
            if end > len(data) or kind.lower() == b"c2pa":
                break
            if kind == b"fmt " and size >= 16:
                pcm_format = struct.unpack_from("<HHIIHH", data, start) == (1, 1, 24000, 48000, 2, 16)
            if kind == b"data" and pcm_format:
                return _validate_pcm(data[start:end])
            offset = end + size % 2
        raise ValueError("missing PCM audio")
    return _validate_pcm(data)


def _validate_pcm(data: bytes) -> bytes:
    if not data or len(data) > MAX_AUDIO_BYTES or len(data) % 2:
        raise ValueError("invalid PCM audio")
    return data


async def _opus(data: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1",
        "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await process.communicate(data)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode or not output.startswith(b"OggS") or len(output) > MAX_MEDIA_BYTES:
        raise ValueError("could not encode Opus")
    return output


def _video_note_file(data: bytes, folder: str) -> tuple[Path, Path]:
    source, target = Path(folder) / "source.mp4", Path(folder) / "note.mp4"
    source.write_bytes(data)
    return source, target


async def video_note(data: bytes) -> bytes:
    """Re-encode a generated clip as a Telegram video note: square, at most 640 px, H.264 MP4 with AAC, under 60 s."""
    with tempfile.TemporaryDirectory(prefix="dom-video-") as folder:
        source, target = await asyncio.to_thread(_video_note_file, data, folder)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-t", str(VIDEO_NOTE_SECONDS),
            "-vf", f"crop='trunc(min(iw,ih)/2)*2':'trunc(min(iw,ih)/2)*2',"
                   f"scale='min({VIDEO_NOTE_SIDE},iw)':'min({VIDEO_NOTE_SIDE},ih)',setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", "-f", "mp4", str(target),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await process.wait()
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode:
            raise ValueError("could not encode the video note")
        output = await asyncio.to_thread(target.read_bytes)
    if len(output) > MAX_MEDIA_BYTES or output[4:8] != b"ftyp":
        raise ValueError("invalid video note")
    return output


def _decode(encoded: str, maximum: int) -> bytes:
    if len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError("encoded media too large")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > maximum:
        raise ValueError("invalid media size")
    return data


class CloudStudio:
    def __init__(self, http: httpx.AsyncClient, usage: UsageRecorder):
        self.http = http
        self.usage = usage

    async def _read(
        self, url: str, headers: dict, mime: str, limit: int, *,
        payload: dict | None = None, params: dict | None = None,
    ) -> tuple[bytes, httpx.Headers]:
        async with self.http.stream(
            "GET" if payload is None else "POST", url, headers=headers, json=payload, params=params,
            follow_redirects=False, timeout=None,  # callers apply a whole-request deadline
        ) as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0].strip().lower() != mime:
                raise ValueError("unexpected content type")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(chunk) > limit:
                    raise ValueError("response too large")
                body.extend(chunk)
            return bytes(body), response.headers

    async def _speech_usage(self, base: str, headers: dict, generation_id: str | None) -> dict:
        if not generation_id:
            return {}
        try:
            async with asyncio.timeout(2):
                raw, _ = await self._read(
                    base + "/generation", headers, "application/json", 64 * 1024,
                    params={"id": generation_id},
                )
                data = json.loads(raw)["data"]
                return {"cost": data.get("total_cost"), "input_tokens": data.get("tokens_prompt"),
                        "output_tokens": data.get("tokens_completion")}
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, AttributeError, TypeError):
            return {}

    async def generate(
        self, config: Config, kind: Kind, text: str, outfit: str | None, chat_id: int,
        allowed: Callable[[], Awaitable[bool]],
    ) -> bytes | None:
        for provider in providers(config.studio, kind):
            try:
                media = await self._generate(config, provider, kind, text, outfit, chat_id, allowed)
            except ModelCallSkipped:
                return None
            except (httpx.HTTPError, TimeoutError, ValueError, OSError, KeyError, IndexError, TypeError, AttributeError) as error:
                log.warning("cloud studio request failed (%s)", type(error).__name__)
            else:
                if media:
                    return media
        return None

    async def _generate(
        self, config: Config, provider: StudioFallback, kind: Kind, text: str, outfit: str | None, chat_id: int,
        allowed: Callable[[], Awaitable[bool]],
    ) -> bytes | None:
        key = await asyncio.to_thread(config.secret, provider.api_key_env)
        if not key:
            return None
        base = provider.base_url
        headers = {"Authorization": f"Bearer {key}"}
        payload: dict = {"model": provider.model}
        if preferences := routing_preferences(provider.data_policy):
            payload["provider"] = preferences
        if provider.provider == "openrouter_video":
            return await self._video(config, provider, kind, text, outfit, chat_id, allowed, key, payload)
        timeout = config.studio.timeouts.voice
        mime, maximum = "application/json", MAX_AUDIO_BYTES * 4 // 3 + 65536
        if kind == "photo":
            portrait = await asyncio.to_thread(_portrait, config.home, outfit) if provider.supports_reference else None
            if not portrait and not provider.allow_without_reference:
                return None
            if portrait:
                reference = await asyncio.to_thread(lambda: base64.b64encode(portrait).decode("ascii"))
                payload["input_references"] = [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + reference}},
                ]
                text = "Keep the same identity and appearance as the reference. " + text
            path = "/images"
            payload["prompt"] = text
            maximum = MAX_MEDIA_BYTES * 4 // 3 + 65536
            timeout = config.studio.timeouts.images
        elif provider.provider == "openrouter_tts":
            path = "/audio/speech"
            payload.update(input=text, voice="Achird", response_format="pcm")
            mime, maximum = "audio/pcm", MAX_AUDIO_BYTES
        else:
            path = f"/models/{provider.model}:generateContent"
            headers = {"x-goog-api-key": key}
            payload = {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Achird"}}},
                },
            }

        usage_kind = "studio_image" if kind == "photo" else "studio_voice"
        usage: dict = {}
        with model_request(usage_kind, chat_id, allowed):
            await check_model_allowed()
            try:
                async with asyncio.timeout(timeout):
                    raw, response_headers = await self._read(base + path, headers, mime, maximum, payload=payload)
                    if provider.provider == "openrouter_tts":
                        usage = await self._speech_usage(base, headers, response_headers.get("X-Generation-Id"))
                        return await _opus(speech_pcm(raw))
                    result = await asyncio.to_thread(json.loads, raw)
                    if kind == "photo":
                        usage = response_usage(result)
                        image = result["data"][0]
                        if image.get("media_type", "image/png") != "image/png":
                            raise ValueError("unexpected image type")
                        data = await asyncio.to_thread(_decode, image["b64_json"], MAX_MEDIA_BYTES)
                        if not data.startswith(PNG):
                            raise ValueError("invalid PNG")
                        return data
                    metadata = result.get("usageMetadata") or {}
                    usage = {"input_tokens": metadata.get("promptTokenCount"),
                             "output_tokens": metadata.get("candidatesTokenCount")}
                    audio = result["candidates"][0]["content"]["parts"][0]["inlineData"]
                    audio_mime = audio["mimeType"].split(";")[0].lower()
                    if audio_mime not in {"audio/l16", "audio/wav", "audio/x-wav"}:
                        raise ValueError("unexpected audio type")
                    data = await asyncio.to_thread(_decode, audio["data"], MAX_AUDIO_BYTES)
                    if audio_mime != "audio/l16" and not data.startswith(b"RIFF"):
                        raise ValueError("invalid WAV")
                    return await _opus(speech_pcm(data))
            finally:
                await asyncio.shield(self.usage.record(
                    usage_kind, provider.model, httpx.URL(base).host, usage, chat_id, fallback=True,
                ))

    async def _video(
        self, config: Config, provider: StudioFallback, kind: Kind, text: str, outfit: str | None, chat_id: int,
        allowed: Callable[[], Awaitable[bool]], key: str, payload: dict,
    ) -> bytes | None:
        """POST /videos, poll the job, then download it through the authenticated content URL, all within the kind's
        deadline. The job's own URLs are never followed: every request goes to the fixed OpenRouter origin."""
        portrait = await asyncio.to_thread(_portrait, config.home, outfit) if provider.supports_reference else None
        if not portrait and not provider.allow_without_reference:
            return None  # a stranger's face is worse than no video
        payload.update(
            prompt=video_prompt(text, portrait is not None), duration=video_seconds(text),
            resolution=VIDEO_RESOLUTION, aspect_ratio="1:1", generate_audio=True,
        )
        if portrait:
            reference = await asyncio.to_thread(lambda: base64.b64encode(portrait).decode("ascii"))
            payload["frame_images"] = [{
                "type": "image_url", "image_url": {"url": "data:image/png;base64," + reference},
                "frame_type": "first_frame",
            }]
        base, headers = provider.base_url, {"Authorization": f"Bearer {key}"}
        timeout = config.studio.timeouts.special if kind == "special" else config.studio.timeouts.video_notes
        usage: dict = {}
        with model_request("studio_video", chat_id, allowed):
            await check_model_allowed()
            try:
                async with asyncio.timeout(timeout):
                    raw, _ = await self._read(base + "/videos", headers, "application/json", 64 * 1024, payload=payload)
                    job = json.loads(raw)
                    job_id = job.get("id") if isinstance(job, dict) else None
                    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
                        raise ValueError("invalid video job")
                    while True:
                        status = job.get("status")
                        if status == "completed":
                            usage = response_usage(job)
                            break
                        if status not in ("pending", "in_progress"):
                            raise ValueError("video job did not complete")
                        await asyncio.sleep(VIDEO_POLL_SECONDS)
                        raw, _ = await self._read(base + f"/videos/{job_id}", headers, "application/json", 64 * 1024)
                        job = json.loads(raw)
                        if not isinstance(job, dict):
                            raise ValueError("invalid video job")
                    clip, _ = await self._read(
                        base + f"/videos/{job_id}/content", headers, "video/mp4", MAX_MEDIA_BYTES, params={"index": 0},
                    )
                    return await video_note(clip)
            finally:
                await asyncio.shield(self.usage.record(
                    "studio_video", provider.model, httpx.URL(base).host, usage, chat_id, fallback=True,
                ))
