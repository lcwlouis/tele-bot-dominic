"""Optional cloud rendering after the local studio has failed a screened job."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import re
import shutil
import struct
import tempfile
import wave
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import httpx
from PIL import Image, ImageOps

from dom import provenance
from dom.config import DEFAULT_VOICE, Config, StudioFallback, routing_preferences
from dom.studio.client import MAX_MEDIA_BYTES
from dom.studio.policy import Kind, backups as providers
from dom.usage import ModelCallSkipped, UsageRecorder, check_model_allowed, model_request, response_usage, token_counts

log = logging.getLogger(__name__)


def _backup_key(
    config: Config, provider: StudioFallback, running_chat_keys: Collection[str] = (),
) -> str | None:
    """The backup's key value, or None when it is unset or also a chat route's key (refused, with the reason logged)."""
    if config.media_key_shared(provider.api_key_env, extra_chat_values=running_chat_keys):
        log.warning("studio backup %s skipped: %s holds the same key as a chat route; give studio backups their own key",
                    provider.model, provider.api_key_env)
        return None
    try:
        return config.secret(provider.api_key_env)
    except (OSError, ValueError):
        return None
PNG = b"\x89PNG\r\n\x1a\n"
MAX_PORTRAIT_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 3 * 1024 * 1024
# Cloud avatars use an approved portrait and Dom's voice track.
VIDEO_POLL_SECONDS = 5
VIDEO_NOTE_SIDE = 640  # Telegram's round video notes are square, at most 640 px
VIDEO_NOTE_SECONDS = 59  # Telegram's limit is 60 s
MAX_VIDEO_SECONDS = 10  # a note stays short, so it stays cheap
AVATAR_MODELS = {"heygen/heygen-avatar-6", "heygen/heygen-avatar-6-turbo"}
SPEECH_COST_DELAYS = (0, 5, 10, 15, 15)
PHOTO_SIDE = 1280
PHOTO_BYTES = 2 * 1024 * 1024
VOICE_SAMPLE_RATE = 24000
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
VIDEO_MODELS_TTL = 24 * 60 * 60
MAX_VIDEO_MODELS_BYTES = 1024 * 1024
# After Dom's deadline, a background check keeps asking for the job's final cost for at most this long.
RECONCILE_SECONDS = 30 * 60
RECONCILE_POLL_SECONDS = 30
VIDEO_JOB = "studio:video-job:"  # kv prefix: an accepted job whose final cost isn't known yet


class VideoUnsupported(ValueError):
    """The model can't make a round video note within Dom's limits; the message is safe to show the owner."""


def _resolution_lines(value: str) -> int | None:
    match = re.fullmatch(r"(\d+)([pk])", value.strip().lower())
    if not match:
        return None
    number = int(match.group(1))
    return number if match.group(2) == "p" else {2: 1440, 4: 2160}.get(number, number * 540)


def video_plan(entry: dict) -> dict:
    """Select a documented audio-driven avatar and its smallest advertised resolution."""
    if entry.get("avatar") not in AVATAR_MODELS or not {"image", "audio"} <= set(entry.get("inputs", ())):
        raise VideoUnsupported("Pick a lip-sync avatar that takes both a portrait and an audio track.")
    sizes = sorted((lines, value) for value in entry.get("supported_resolutions") or []
                   if (lines := _resolution_lines(value)) is not None)
    if not sizes:
        raise VideoUnsupported("The model doesn't publish a video resolution.")
    return {"resolution": sizes[0][1]}


def video_rate(entry: dict, plan: dict) -> float:
    """Return the advertised USD per second at this resolution."""
    return _video_price(entry, plan, duration=1, include_image=False)


def video_estimate(entry: dict, plan: dict, duration: float) -> float:
    return round(_video_price(entry, plan, duration=duration, include_image=True), 6)


def _video_price(entry: dict, plan: dict, *, duration: float, include_image: bool) -> float:
    skus = {key.lower(): value for key, value in (entry.get("pricing_skus") or {}).items()}

    def usd(key: str) -> float | None:
        try:
            amount = float(skus.get(key))
        except (TypeError, ValueError):
            return None
        return amount / 100 if "cent" in key else amount

    seconds = [key for key in skus if "second" in key and "megapixel" not in key]
    tag = str(plan.get("resolution", "")).lower()
    chosen = [key for key in seconds if tag and tag in key] or [
        key for key in seconds if not re.search(r"\d+[pk]\b|_\d+[pk]", key)] or seconds
    rates = [rate for key in chosen if (rate := usd(key)) is not None and math.isfinite(rate) and rate > 0]
    if not rates:
        raise VideoUnsupported("The avatar doesn't publish a per-second price.")
    cost = max(rates) * duration
    if include_image:
        cost += max((rate for key in skus if "image" in key and (rate := usd(key)) is not None
                     and math.isfinite(rate) and rate >= 0), default=0.0)
        cost = max(cost, usd("minimum_cents_per_generation") or 0.0)
    return cost


async def _video_catalog(http: httpx.AsyncClient, url: str) -> list:
    async with http.stream("GET", url, follow_redirects=False, timeout=10) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_VIDEO_MODELS_BYTES:
                raise ValueError("video model list too large")
    data = json.loads(body).get("data")
    if not isinstance(data, list):
        raise ValueError("unexpected video model list")
    return [entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


async def video_models(http: httpx.AsyncClient, base: str) -> dict[str, dict]:
    """Join the video settings and price list to the catalog that publishes input modalities."""
    details, catalog = await asyncio.gather(
        _video_catalog(http, base + "/videos/models"),
        _video_catalog(http, base + "/models?output_modalities=video"),
    )
    by_id = {entry["id"]: entry for entry in catalog}
    result = {}
    for entry in details:
        avatar = entry["id"]
        architecture = by_id.get(avatar, {}).get("architecture") or {}
        if avatar not in AVATAR_MODELS or "video" not in (architecture.get("output_modalities") or []):
            continue
        item = {**entry, "avatar": avatar, "inputs": architecture.get("input_modalities") or []}
        try:
            video_rate(item, video_plan(item))
        except VideoUnsupported:
            continue
        result[avatar] = item
        if isinstance(alias := entry.get("canonical_slug"), str):
            result[alias] = item
    return result


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


STILL_SIDE = 768  # a still for a video note is small: the avatar and the studio's renderer work at that size
MAX_STILL_BYTES = 3 * 1024 * 1024


def png_still(data: bytes) -> bytes | None:
    """A generated photo as the PNG a video note is filmed from (at most 768 px), or None when it can't be read."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((STILL_SIDE, STILL_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, "PNG")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return out.getvalue() if len(out.getvalue()) <= MAX_STILL_BYTES else None


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


async def _ffmpeg_audio(args: tuple[str, ...], data: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", *args,
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
    if process.returncode or not output:
        raise ValueError("could not convert studio audio")
    return output


async def _opus(data: bytes) -> bytes:
    output = await _ffmpeg_audio((
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1",
    ), data)
    if not output.startswith(b"OggS") or len(output) > MAX_MEDIA_BYTES:
        raise ValueError("could not encode Opus")
    return output


async def _avatar_audio(voice: bytes) -> tuple[bytes, float]:
    pcm = await _ffmpeg_audio((
        "-f", "ogg", "-i", "pipe:0", "-map", "0:a:0", "-t", str(MAX_VIDEO_SECONDS + 1),
        "-ac", "1", "-ar", str(VOICE_SAMPLE_RATE), "-f", "s16le", "pipe:1",
    ), voice)
    duration = len(pcm) / (2 * VOICE_SAMPLE_RATE)
    if duration > MAX_VIDEO_SECONDS:
        raise VideoUnsupported(f"The voice line exceeds {MAX_VIDEO_SECONDS} seconds.")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, VOICE_SAMPLE_RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    return buffer.getvalue(), duration


def _video_note_files(data: bytes) -> tuple[Path, Path, Path]:
    folder = Path(tempfile.mkdtemp(prefix="dom-video-"))
    source, target = folder / "source.mp4", folder / "note.mp4"
    try:
        source.write_bytes(data)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder, source, target


def _read_note(target: Path) -> bytes:
    if not target.is_file() or target.stat().st_size > MAX_MEDIA_BYTES:
        raise ValueError("invalid video note")
    with target.open("rb") as file:
        return file.read(MAX_MEDIA_BYTES + 1)


async def video_note(data: bytes, audio: bytes, duration: float) -> bytes:
    """Re-encode a generated clip as a Telegram video note: square, at most 640 px, H.264 MP4 with AAC, under 60 s.
    The encoder stops at the size cap, and the temporary files are made and removed off the event loop."""
    files = asyncio.create_task(asyncio.to_thread(_video_note_files, data))
    try:
        folder, source, target = await asyncio.shield(files)
    except asyncio.CancelledError:
        folder, _, _ = await files
        await asyncio.to_thread(shutil.rmtree, folder, True)
        raise
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "mov", "-protocol_whitelist", "file", "-i", str(source),
            "-f", "wav", "-i", "pipe:0", "-map", "0:v:0", "-map", "1:a:0",
            "-t", str(min(VIDEO_NOTE_SECONDS, duration)),
            "-vf", f"crop='trunc(min(iw,ih)/2)*2':'trunc(min(iw,ih)/2)*2',"
                   f"scale='min({VIDEO_NOTE_SIDE},iw)':'min({VIDEO_NOTE_SIDE},ih)',setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart",
            "-fs", str(MAX_MEDIA_BYTES), "-f", "mp4", str(target),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await process.communicate(audio)
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode:
            raise ValueError("could not encode the video note")
        output = await asyncio.to_thread(_read_note, target)
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    if len(output) >= MAX_MEDIA_BYTES or output[4:8] != b"ftyp":  # -fs stops at the cap: a capped file is cut short
        raise ValueError("invalid video note")
    return output


def _avatar_references(portrait: bytes, wav: bytes) -> list[dict]:
    return [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(portrait).decode("ascii")}},
        {"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")}},
    ]


def _decode(encoded: str, maximum: int) -> bytes:
    if len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError("encoded media too large")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > maximum:
        raise ValueError("invalid media size")
    return data


def _prepare_photo(encoded: str, mime: str) -> bytes:
    formats = {"image/png": "PNG", "image/jpeg": "JPEG"}
    if mime not in formats:
        raise ValueError("unexpected image type")
    data = _decode(encoded, MAX_MEDIA_BYTES)
    try:
        with Image.open(io.BytesIO(data), formats=[formats[mime]]) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("image dimensions too large")
            if len(data) <= PHOTO_BYTES and max(image.size) <= PHOTO_SIDE:
                return data
            photo = ImageOps.exif_transpose(image).convert("RGB")
            photo.thumbnail((PHOTO_SIDE, PHOTO_SIDE), Image.Resampling.LANCZOS)
            with io.BytesIO() as output:
                photo.save(output, format="JPEG", quality=90, optimize=True)
                return output.getvalue()
    except Image.DecompressionBombError as error:
        raise ValueError("image dimensions too large") from error


class CloudStudio:
    def __init__(
        self,
        http: httpx.AsyncClient,
        usage: UsageRecorder,
        *,
        running_chat_keys: Callable[[], Collection[str]] | None = None,
    ):
        self.http = http
        self.usage = usage
        self.running_chat_keys = running_chat_keys
        self._video_models: tuple[float, dict[str, dict]] | None = None
        self._reconciling: set[asyncio.Task] = set()
        self._video_reserve_lock = asyncio.Lock()

    def _chat_key_values(self) -> Collection[str]:
        return self.running_chat_keys() if self.running_chat_keys is not None else ()

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

    async def _speech_usage(self, base: str, headers: dict, generation_id: str) -> dict:
        try:
            async with asyncio.timeout(2):
                raw, _ = await self._read(
                    base + "/generation", headers, "application/json", 64 * 1024,
                    params={"id": generation_id},
                )
                data = json.loads(raw)["data"]
                return {"cost": data.get("total_cost"), "input_tokens": data.get("tokens_prompt"),
                        "output_tokens": data.get("tokens_completion")}
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, AttributeError, TypeError, RuntimeError):
            return {}  # RuntimeError: the HTTP client closed during shutdown

    def _track_reconcile(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._reconciling.add(task)
        task.add_done_callback(self._reconciling.discard)

    async def _reconcile_speech(
        self, base: str, headers: dict, generation_id: str, recorded: asyncio.Task,
    ) -> None:
        row = await recorded
        if row is None:
            return
        for delay in SPEECH_COST_DELAYS:
            await asyncio.sleep(delay)
            usage = await self._speech_usage(base, headers, generation_id)
            cost = usage.get("cost")
            if type(cost) in (int, float) and 0 < cost < math.inf:  # 0 can mean the stats haven't settled yet
                inputs, outputs, _ = token_counts(usage)
                try:
                    await asyncio.shield(self.usage.store.set_usage_cost(
                        row, float(cost), input_tokens=inputs, output_tokens=outputs,
                    ))
                except Exception as error:
                    log.warning("could not record a voice note's cost (%s)", type(error).__name__)
                return

    async def aclose(self) -> None:
        """At shutdown, before the HTTP client closes: stop the background cost checks. A voice cost not yet
        reported stays at 0 (bounded spend, not money lost); a video job's estimate survives in the store."""
        tasks = list(self._reconciling)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def generate(
        self, config: Config, kind: Kind, text: str, outfit: str | None, chat_id: int,
        allowed: Callable[[], Awaitable[bool]], place_reference: bytes | None = None,
    ) -> bytes | None:
        """`place_reference` is the owner's photo of the place a picture is set in (dom/place_photos.py); only a photo
        model that takes references is sent it."""
        if kind in {"video_note", "special"}:
            raise ValueError("Use generate_video with Dom's voice path.")
        for provider in providers(config.studio, kind):
            try:
                media = await self._generate(config, provider, kind, text, outfit, chat_id, allowed, place_reference)
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
        allowed: Callable[[], Awaitable[bool]], place_reference: bytes | None = None,
    ) -> bytes | None:
        key = await asyncio.to_thread(_backup_key, config, provider, self._chat_key_values())
        if not key:
            return None
        base = provider.base_url
        headers = {"Authorization": f"Bearer {key}"}
        payload: dict = {"model": provider.model}
        if preferences := routing_preferences(provider.data_policy):
            payload["provider"] = preferences
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
            if place_reference and provider.supports_reference:  # the second picture: where the scene is set
                place = await asyncio.to_thread(lambda: base64.b64encode(place_reference).decode("ascii"))
                payload.setdefault("input_references", []).append(
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + place}},
                )
                text += (
                    " The scene is set at the real place shown in the last reference: keep how that place looks."
                    if portrait else " The scene is set at the real place shown in the reference: keep how it looks."
                )
            path = "/images"
            payload["prompt"] = text
            if re.fullmatch(r"bytedance-seed/seedream-5-5(?:-[0-9]{8})?", provider.model):
                payload.update(resolution="1K", aspect_ratio="1:1")
            maximum = MAX_MEDIA_BYTES * 4 // 3 + 65536
            timeout = config.studio.timeouts.images
        elif provider.provider == "openrouter_tts":
            path = "/audio/speech"
            payload.update(input=text, voice=provider.voice or DEFAULT_VOICE, response_format="pcm")
            mime, maximum = "audio/pcm", MAX_AUDIO_BYTES
        else:
            path = f"/models/{provider.model}:generateContent"
            headers = {"x-goog-api-key": key}
            payload = {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": provider.voice or DEFAULT_VOICE}}},
                },
            }

        usage_kind = "studio_image" if kind == "photo" else "studio_voice"
        usage: dict = {}
        generation_id = None
        with model_request(usage_kind, chat_id, allowed):
            await check_model_allowed()
            try:
                async with asyncio.timeout(timeout):
                    raw, response_headers = await self._read(base + path, headers, mime, maximum, payload=payload)
                    if provider.provider == "openrouter_tts":
                        generation_id = response_headers.get("X-Generation-Id")
                        return await _opus(speech_pcm(raw))
                    result = await asyncio.to_thread(json.loads, raw)
                    if kind == "photo":
                        usage = response_usage(result)
                        image = result["data"][0]
                        return await asyncio.to_thread(
                            _prepare_photo, image["b64_json"], image.get("media_type", "image/png"),
                        )
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
                recorded = asyncio.create_task(self.usage.record(
                    usage_kind, provider.model, httpx.URL(base).host, usage, chat_id, fallback=True,
                ))
                if generation_id:
                    self._track_reconcile(self._reconcile_speech(base, headers, generation_id, recorded))
                await asyncio.shield(recorded)

    async def _video_entry(self, base: str, model: str) -> dict:
        if self._video_models is None or monotonic() - self._video_models[0] > VIDEO_MODELS_TTL:
            self._video_models = (monotonic(), await video_models(self.http, base))
        entry = self._video_models[1].get(model)
        if entry is None:
            raise VideoUnsupported("OpenRouter doesn't list that model as a supported lip-sync avatar.")
        return entry

    async def _job(self, base: str, headers: dict, job_id: str) -> dict:
        raw, _ = await self._read(base + f"/videos/{job_id}", headers, "application/json", 64 * 1024)
        job = json.loads(raw)
        if not isinstance(job, dict):
            raise ValueError("invalid video job")
        return job

    async def generate_video(
        self, config: Config, kind: Kind, outfit: str | None, chat_id: int,
        allowed: Callable[[], Awaitable[bool]], make_voice: Callable[[], Awaitable[bytes | None]],
        still: bytes | None = None,
    ) -> bytes | None:
        """A round video note from the voice `make_voice` returns. `still` (a PNG) is the picture the avatar is filmed
        from; without it the outfit's approved portrait is used."""
        timeout = config.studio.timeouts.special if kind == "special" else config.studio.timeouts.video_notes
        try:
            async with asyncio.timeout(timeout):
                with model_request("studio_video", chat_id, allowed):
                    await check_model_allowed()
                portrait = still or await asyncio.to_thread(_portrait, config.home, outfit)
                if portrait is None:
                    return None
                references = None
                for provider in providers(config.studio, kind):
                    try:
                        key = await asyncio.to_thread(_backup_key, config, provider, self._chat_key_values())
                        if not key:
                            continue
                        entry = await self._video_entry(provider.base_url, provider.model)
                        plan = video_plan(entry)
                    except VideoUnsupported:
                        log.warning("video backup %s isn't a supported lip-sync avatar; replace it in Settings → Models",
                                    provider.model)
                        continue
                    except (httpx.HTTPError, TimeoutError, ValueError, OSError, TypeError, AttributeError):
                        continue
                    if references is None:
                        with model_request("studio_voice", chat_id, allowed):
                            await check_model_allowed()
                            async with asyncio.timeout(config.studio.timeouts.voice):
                                voice = await make_voice()
                        if voice is None:
                            return None
                        wav, duration = await _avatar_audio(voice)
                        references = await asyncio.to_thread(_avatar_references, portrait, wav)
                    try:
                        return await self._video(provider, chat_id, allowed, key, entry, plan, wav, duration, references)
                    except (httpx.HTTPError, ValueError, OSError, TypeError, AttributeError) as error:
                        log.warning("cloud avatar request failed (%s)", type(error).__name__)
        except (ModelCallSkipped, TimeoutError, ValueError, OSError) as error:
            log.warning("cloud video note failed (%s)", type(error).__name__)
        return None

    async def _video(
        self, provider: StudioFallback, chat_id: int, allowed: Callable[[], Awaitable[bool]], key: str,
        entry: dict, plan: dict, wav: bytes, duration: float, references: list[dict],
    ) -> bytes:
        """Submit, account for the accepted job, poll, download, and mux Dom's audio."""
        base, headers = provider.base_url, {"Authorization": f"Bearer {key}"}
        payload = {"model": provider.model, **plan, "input_references": references,
                   "provider": routing_preferences(provider.data_policy)}
        estimate = video_estimate(entry, plan, duration)
        host = httpx.URL(base).host
        with model_request("studio_video", chat_id, allowed):
            # Reserve the estimated cost under a lock so concurrent video submits cannot both pass the cap,
            # then release the lock before the network call.
            async with self._video_reserve_lock:
                await check_model_allowed()
                row = await self.usage.record(
                    "studio_video", provider.model, host, {"cost": estimate}, chat_id, fallback=True,
                )
            job_id: str | None = None
            settled = False
            try:
                raw, _ = await self._read(base + "/videos", headers, "application/json", 64 * 1024, payload=payload)
                job = json.loads(raw)
                parsed_id = job.get("id") if isinstance(job, dict) else None
                if not isinstance(parsed_id, str) or not JOB_ID.fullmatch(parsed_id):
                    raise ValueError("invalid video job")
                job_id = parsed_id
                if row is not None:
                    await asyncio.shield(self._persist_job(provider, job_id, row, estimate))
                while True:
                    status = job.get("status")
                    if status not in ("pending", "in_progress"):
                        await self._settle(job_id, row, job)
                        settled = True
                        if status != "completed":
                            raise ValueError("video job did not complete")
                        break
                    await asyncio.sleep(VIDEO_POLL_SECONDS)
                    job = await self._job(base, headers, job_id)
                clip, _ = await self._read(
                    base + f"/videos/{job_id}/content", headers, "video/mp4", MAX_MEDIA_BYTES, params={"index": 0},
                )
                return await video_note(clip, wav, duration)
            finally:
                if not settled:
                    if job_id is not None:
                        self._track_reconcile(self._reconcile(base, headers, job_id, row))
                    elif row is not None:
                        await asyncio.shield(self.usage.store.delete_usage(row))
                        provenance.discard(row)

    async def _persist_job(self, provider: StudioFallback, job_id: str, row: int, estimate: float) -> None:
        await self.usage.store.kv_set(VIDEO_JOB + job_id, json.dumps({
            "row": row,
            "model": provider.model,
            "base_url": provider.base_url,
            "api_key_env": provider.api_key_env,
            "estimate": estimate,
            "at": datetime.now(timezone.utc).isoformat(),
        }))

    async def _settle(self, job_id: str, row: int | None, job: dict) -> None:
        """Replace a job's estimate with its reported cost (none reported for a failed job means nothing was billed)."""
        cost = response_usage(job).get("cost")
        if row is not None:
            reported = cost if isinstance(cost, (int, float)) and cost >= 0 else (
                0.0 if job.get("status") != "completed" else None)
            if reported is not None:
                await asyncio.shield(self.usage.store.set_usage_cost(row, float(reported)))
        await asyncio.shield(self.usage.store.kv_delete(VIDEO_JOB + job_id))

    async def _reconcile(
        self, base: str, headers: dict, job_id: str, row: int | None, *, deadline_seconds: float = RECONCILE_SECONDS,
    ) -> None:
        """After the deadline or a restart: keep asking for the job's final cost for a bounded time.
        Until then the estimate stays."""
        try:
            async with asyncio.timeout(max(0.0, deadline_seconds)):
                while True:
                    try:
                        job = await self._job(base, headers, job_id)
                    except (httpx.HTTPError, ValueError):
                        job = {"status": "pending"}
                    if job.get("status") not in ("pending", "in_progress"):
                        await self._settle(job_id, row, job)
                        return
                    await asyncio.sleep(RECONCILE_POLL_SECONDS)
        except TimeoutError:
            log.warning("a cloud video job's final cost never arrived; its estimate stays")
            await asyncio.shield(self.usage.store.kv_delete(VIDEO_JOB + job_id))

    async def reconcile_pending(self, config: Config) -> int:
        """On startup: resume reconciling any persisted video jobs whose final cost was not settled before shutdown."""
        entries = await self.usage.store.kv_prefix(VIDEO_JOB)
        scheduled = 0
        now = datetime.now(timezone.utc)
        for key, raw in entries:
            job_id = key.removeprefix(VIDEO_JOB)
            try:
                data = json.loads(raw)
                row = data.get("row")
            except (ValueError, AttributeError):
                await self.usage.store.kv_delete(key)
                continue
            if not JOB_ID.fullmatch(job_id) or type(row) is not int:
                await self.usage.store.kv_delete(key)
                continue
            model = data.get("model")
            base_url = data.get("base_url")
            api_key_env = data.get("api_key_env")
            provider = next(
                (p for p in config.studio.video_fallbacks
                 if (not base_url or p.base_url == base_url)
                 and (not api_key_env or p.api_key_env == api_key_env)
                 and (not model or p.model == model)),
                next((p for p in config.studio.video_fallbacks if model and p.model == model), None),
            )
            base = base_url or (provider.base_url if provider is not None else None)
            key_env = api_key_env or (provider.api_key_env if provider is not None else None)
            secret = (
                await asyncio.to_thread(
                    config.media_secret, key_env, extra_chat_values=self._chat_key_values(),
                )
                if key_env else None
            )
            if not base or not secret:
                log.warning("cannot reconcile persisted video job %s: missing endpoint or media key", job_id)
                continue
            remaining = float(RECONCILE_SECONDS)
            if isinstance(at := data.get("at"), str):
                try:
                    started = datetime.fromisoformat(at)
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    remaining = max(5.0, RECONCILE_SECONDS - (now - started).total_seconds())
                except ValueError:
                    pass
            headers = {"Authorization": f"Bearer {secret}"}
            self._track_reconcile(self._reconcile(base, headers, job_id, row, deadline_seconds=remaining))
            scheduled += 1
        return scheduled
