"""Optional cloud rendering after the local studio has failed a screened job."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import shutil
import struct
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import httpx

from dom.config import Config, StudioFallback, routing_preferences
from dom.studio.client import MAX_MEDIA_BYTES
from dom.studio.policy import Kind, backups as providers
from dom.usage import ModelCallSkipped, UsageRecorder, check_model_allowed, model_request, response_usage

log = logging.getLogger(__name__)
PNG = b"\x89PNG\r\n\x1a\n"
MAX_PORTRAIT_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 3 * 1024 * 1024
# OpenRouter video: a square (or near-square) low-resolution clip with the model's own audio, chosen from what the
# model publishes, polled every few seconds within the kind's deadline.
VIDEO_POLL_SECONDS = 5
VIDEO_NOTE_SIDE = 640  # Telegram's round video notes are square, at most 640 px
VIDEO_NOTE_SECONDS = 59  # Telegram's limit is 60 s
MAX_VIDEO_SECONDS = 10  # a note stays short, so it stays cheap
NEAR_SQUARE = math.log(4 / 3) + 1e-9  # 4:3 or 3:4 crops to a square without losing the face
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
VIDEO_MODELS_TTL = 24 * 60 * 60
MAX_VIDEO_MODELS_BYTES = 1024 * 1024
# Reported cost unknown from the model's price list (token-priced models): the estimate a note counts as until the
# job's real cost arrives.
VIDEO_ESTIMATE_CEILING_USD = 1.0
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


def _ratio(value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d+)", value.strip())
    if not match or not int(match.group(1)) or not int(match.group(2)):
        return None
    return math.log(int(match.group(1)) / int(match.group(2)))


def video_plan(entry: dict, text: str) -> dict:
    """The request fields for a note from the model's published capabilities: the squarest aspect ratio (at most 4:3),
    its smallest resolution from 480p up, and the shortest duration that fits the line, at most 10 s."""
    ratios = [(abs(r), value) for value in entry.get("supported_aspect_ratios") or [] if (r := _ratio(value)) is not None]
    if not ratios:
        raise VideoUnsupported("The model doesn't publish its aspect ratios, so Dom can't ask it for a square video.")
    distance, aspect = min(ratios)
    if distance > NEAR_SQUARE:
        raise VideoUnsupported("The model makes no square or near-square video, so it can't make a round video note.")
    plan: dict = {"aspect_ratio": aspect}
    sizes = sorted((lines, value) for value in entry.get("supported_resolutions") or []
                   if (lines := _resolution_lines(value)) is not None)
    if sizes:
        plan["resolution"] = next((value for lines, value in sizes if lines >= 480), sizes[-1][1])
    want = video_seconds(text)
    durations = sorted(d for d in entry.get("supported_durations") or [] if isinstance(d, int) and 0 < d <= MAX_VIDEO_SECONDS)
    if entry.get("supported_durations") and not durations:
        raise VideoUnsupported(f"The model makes no video of {MAX_VIDEO_SECONDS} seconds or less.")
    plan["duration"] = next((d for d in durations if d >= want), durations[-1]) if durations else want
    if entry.get("generate_audio"):
        plan["generate_audio"] = True
    return plan


def video_estimate(entry: dict, plan: dict, *, reference: bool) -> float:
    """The most a note may cost from the model's price list (USD), counted against the spend cap until the job reports
    its real cost. Per-second prices for the chosen resolution, else the dearest per-second price; a ceiling when the
    list prices tokens."""
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
    rates = [rate for key in chosen if (rate := usd(key)) is not None]
    if not rates:
        return VIDEO_ESTIMATE_CEILING_USD
    cost = max(rates) * plan.get("duration", MAX_VIDEO_SECONDS)
    if reference:
        cost += max((rate for key in skus if "image" in key and (rate := usd(key)) is not None), default=0.0)
    floor = usd("minimum_cents_per_generation") or 0.0
    return round(max(cost, floor), 6)


async def video_models(http: httpx.AsyncClient, base: str) -> dict[str, dict]:
    """OpenRouter's public video model list by ID: one GET, no redirects, a size cap."""
    async with http.stream("GET", base + "/videos/models", follow_redirects=False, timeout=10) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_VIDEO_MODELS_BYTES:
                raise ValueError("video model list too large")
    data = json.loads(body).get("data")
    if not isinstance(data, list):
        raise ValueError("unexpected video model list")
    return {entry["id"]: entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)}


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


def _video_note_files(data: bytes) -> tuple[Path, Path, Path]:
    folder = Path(tempfile.mkdtemp(prefix="dom-video-"))
    source, target = folder / "source.mp4", folder / "note.mp4"
    source.write_bytes(data)
    return folder, source, target


def _read_note(target: Path) -> bytes:
    if not target.is_file() or target.stat().st_size > MAX_MEDIA_BYTES:
        raise ValueError("invalid video note")
    with target.open("rb") as file:
        return file.read(MAX_MEDIA_BYTES + 1)


async def video_note(data: bytes) -> bytes:
    """Re-encode a generated clip as a Telegram video note: square, at most 640 px, H.264 MP4 with AAC, under 60 s.
    The encoder stops at the size cap, and the temporary files are made and removed off the event loop."""
    folder, source, target = await asyncio.to_thread(_video_note_files, data)
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-t", str(VIDEO_NOTE_SECONDS),
            "-vf", f"crop='trunc(min(iw,ih)/2)*2':'trunc(min(iw,ih)/2)*2',"
                   f"scale='min({VIDEO_NOTE_SIDE},iw)':'min({VIDEO_NOTE_SIDE},ih)',setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart",
            "-fs", str(MAX_MEDIA_BYTES), "-f", "mp4", str(target),
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
        output = await asyncio.to_thread(_read_note, target)
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
    if len(output) >= MAX_MEDIA_BYTES or output[4:8] != b"ftyp":  # -fs stops at the cap: a capped file is cut short
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
        self._video_models: tuple[float, dict[str, dict]] | None = None
        self._reconciling: set[asyncio.Task] = set()

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

    async def _video_entry(self, base: str, model: str) -> dict:
        if self._video_models is None or monotonic() - self._video_models[0] > VIDEO_MODELS_TTL:
            self._video_models = (monotonic(), await video_models(self.http, base))
        entry = self._video_models[1].get(model)
        if entry is None:
            raise VideoUnsupported("OpenRouter doesn't list that video model.")
        return entry

    async def _job(self, base: str, headers: dict, job_id: str) -> dict:
        raw, _ = await self._read(base + f"/videos/{job_id}", headers, "application/json", 64 * 1024)
        job = json.loads(raw)
        if not isinstance(job, dict):
            raise ValueError("invalid video job")
        return job

    async def _video(
        self, config: Config, provider: StudioFallback, kind: Kind, text: str, outfit: str | None, chat_id: int,
        allowed: Callable[[], Awaitable[bool]], key: str, payload: dict,
    ) -> bytes | None:
        """POST /videos, poll the job, then download it through the authenticated content URL, all within the kind's
        deadline. The job's own URLs are never followed: every request goes to the fixed OpenRouter origin. Once a job
        is accepted its estimated cost is recorded at once (so it counts toward the spend cap), and corrected to the
        reported cost when the job ends, by a bounded background check if that is after the deadline."""
        base, headers = provider.base_url, {"Authorization": f"Bearer {key}"}
        timeout = config.studio.timeouts.special if kind == "special" else config.studio.timeouts.video_notes
        with model_request("studio_video", chat_id, allowed):
            await check_model_allowed()
            async with asyncio.timeout(timeout):
                entry = await self._video_entry(base, provider.model)
                plan = video_plan(entry, text)
                frames = entry.get("supported_frame_images") or []
                usable = provider.supports_reference and "first_frame" in frames
                portrait = await asyncio.to_thread(_portrait, config.home, outfit) if usable else None
                if not portrait and not provider.allow_without_reference:
                    return None  # a stranger's face is worse than no video
                payload.update(prompt=video_prompt(text, portrait is not None), **plan)
                if portrait:
                    reference = await asyncio.to_thread(lambda: base64.b64encode(portrait).decode("ascii"))
                    payload["frame_images"] = [{
                        "type": "image_url", "image_url": {"url": "data:image/png;base64," + reference},
                        "frame_type": "first_frame",
                    }]
                estimate = video_estimate(entry, plan, reference=portrait is not None)
                await check_model_allowed()  # the spend cap, with every unresolved estimate counted, before submitting
                raw, _ = await self._read(base + "/videos", headers, "application/json", 64 * 1024, payload=payload)
                job = json.loads(raw)
                job_id = job.get("id") if isinstance(job, dict) else None
                if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
                    raise ValueError("invalid video job")
            # Accepted: it may be billed whatever happens next, so its estimate counts now and survives a restart.
            row = await asyncio.shield(self.usage.record(
                "studio_video", provider.model, httpx.URL(base).host, {"cost": estimate}, chat_id, fallback=True,
            ))
            if row is not None:
                await self.usage.store.kv_set(VIDEO_JOB + job_id, json.dumps({
                    "row": row, "model": provider.model, "estimate": estimate,
                    "at": datetime.now(timezone.utc).isoformat(),
                }))
            settled = False
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        status = job.get("status")
                        if status == "completed":
                            await self._settle(job_id, row, job)
                            settled = True
                            break
                        if status not in ("pending", "in_progress"):
                            await self._settle(job_id, row, job)
                            settled = True
                            raise ValueError("video job did not complete")
                        await asyncio.sleep(VIDEO_POLL_SECONDS)
                        job = await self._job(base, headers, job_id)
                    clip, _ = await self._read(
                        base + f"/videos/{job_id}/content", headers, "video/mp4", MAX_MEDIA_BYTES, params={"index": 0},
                    )
                    return await video_note(clip)
            finally:
                if not settled:
                    task = asyncio.create_task(self._reconcile(base, headers, job_id, row))
                    self._reconciling.add(task)
                    task.add_done_callback(self._reconciling.discard)

    async def _settle(self, job_id: str, row: int | None, job: dict) -> None:
        """Replace a job's estimate with its reported cost (none reported for a failed job means nothing was billed)."""
        cost = response_usage(job).get("cost")
        if row is not None:
            reported = cost if isinstance(cost, (int, float)) and cost >= 0 else (
                0.0 if job.get("status") != "completed" else None)
            if reported is not None:
                await asyncio.shield(self.usage.store.set_usage_cost(row, float(reported)))
        await asyncio.shield(self.usage.store.kv_delete(VIDEO_JOB + job_id))

    async def _reconcile(self, base: str, headers: dict, job_id: str, row: int | None) -> None:
        """After the deadline: keep asking for the job's final cost for a bounded time. Until then the estimate stays."""
        try:
            async with asyncio.timeout(RECONCILE_SECONDS):
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
