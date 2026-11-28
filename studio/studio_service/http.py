"""The versioned HTTP surface shared with the bot."""

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
from functools import partial
from hmac import compare_digest
import json
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


from .process import WorkerError
from .service import read_output


VERSION = "0.8.6.1"
MAX_BODY = 16 * 1024
MAX_STILL = 3 * 1024**2  # a fresh still for a video note, as a PNG
MAX_STILL_BODY = MAX_STILL * 4 // 3 + MAX_BODY  # that still as base64, plus the rest of the request
MAX_STT_AUDIO = 20 * 1024**2  # matches Telegram's bot file download limit
PNG = b"\x89PNG\r\n\x1a\n"
MIME = {"voice": "audio/ogg", "image": "image/png", "video": "video/mp4", "special": "video/mp4"}
VoiceText = Annotated[str, Field(min_length=1, max_length=600)]


def bounded_outfit(name: str) -> str | None:
    return name if 1 <= len(name) <= 128 else None


OutfitName = Annotated[str, AfterValidator(bounded_outfit)]


class Payload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Voice(Payload):
    text: VoiceText
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)] | None = None


class Video(Payload):
    text: Annotated[str, Field(min_length=1, max_length=200)]
    special: bool = False
    outfit: OutfitName | None = None
    # A fresh still of the host's character in the scene to film, sent as base64 PNG and held as the decoded bytes.
    # Without it the outfit's portrait is animated.
    still: bytes | None = None

    @field_validator("still", mode="before")
    @classmethod
    def decode_still(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > MAX_STILL * 4 // 3 + 4:
            raise ValueError("still must be base64 text of at most 3 MiB")
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("still is not base64") from None
        if not data.startswith(PNG) or len(data) > MAX_STILL:
            raise ValueError("still must be a PNG of at most 3 MiB")
        return data

    @model_validator(mode="after")
    def validate_special_text(self):
        if self.special and len(self.text) > 120:
            raise ValueError("special video text exceeds 120 characters")
        return self


class Image(Payload):
    scene: Annotated[str, Field(min_length=1, max_length=300)]
    outfit: OutfitName | None = None
    size: Annotated[int, Field(ge=768, le=768)] = 768


MODELS = {"voice": Voice, "video": Video, "image": Image}


def error(status, message):
    return JSONResponse({"error": message}, status_code=status)


async def disconnected(request):
    while (await request.receive())["type"] != "http.disconnect":
        pass


def create_app(studio, sources, token):
    async def refresh():
        while True:
            await asyncio.sleep(60)
            await sources.refresh()

    async def sample_idle():
        while True:
            await asyncio.sleep(2)
            if not studio.lane.locked():
                await studio.read_memory()

    @asynccontextmanager
    async def lifespan(_):
        await sources.refresh()
        await studio.read_memory()
        tasks = [asyncio.create_task(coro()) for coro in (refresh, studio.warm, sample_idle)]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await studio.close()

    def authorize(request):
        if not sources.permits(request.scope.get("client")):
            return Response(status_code=403)
        headers = [value for key, value in request.scope["headers"] if key == b"authorization"]
        if (len(headers) != 1 or not headers[0].lower().startswith(b"bearer ")
                or not compare_digest(headers[0][7:], token)):
            return error(401, "unauthorized")
        return None

    async def health(request):
        denied = authorize(request)
        if denied is not None:
            return denied
        reading = studio.reading
        return JSONResponse({
            "workers": [name for name, worker in studio.workers.items() if worker.loaded],
            "available_memory_bytes": reading.available if reading else None,
            "queue_depth": len(studio.pending),
            "version": VERSION,
            "video_still": True,
            "stt": "stt" in studio.workers,
            "models": {
                name: cfg.model
                for name in studio.workers
                if (cfg := getattr(studio.config.workers, name)) is not None and cfg.model
            },
        })

    async def portrait(request):
        denied = authorize(request)
        if denied is not None:
            return denied
        outfit = studio.config.outfits.get(request.path_params["outfit"])
        if outfit is None:
            return error(404, "unknown-outfit")
        try:
            content = await asyncio.to_thread(read_output, outfit.portrait)
        except (OSError, WorkerError):
            return error(503, "portrait-unavailable")
        return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def submit(request, *, kind):
        denied = authorize(request)
        if denied is not None:
            return denied
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return error(415, "expected-json")
        body = bytearray()
        limit = MAX_STILL_BODY if kind == "video" else MAX_BODY
        try:
            async with asyncio.timeout(5 if kind != "video" else 15):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > limit:
                        return error(413, "body-too-large")
                    body.extend(chunk)
            values = MODELS[kind].model_validate(json.loads(body))
        except (ValueError, RecursionError):
            return error(422, "invalid-request")
        except TimeoutError:
            return error(408, "request-timeout")
        except ClientDisconnect:
            return Response(status_code=499)
        payload = values.model_dump()
        if payload.get("special"):
            kind = "special"
        if len(studio.pending) >= 3:
            return error(503, "busy")
        job = asyncio.create_task(studio.job(kind, payload))
        studio.pending.add(job)
        disconnect = asyncio.create_task(disconnected(request))
        try:
            done, _ = await asyncio.wait((job, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                return Response(status_code=499)
            status, result = job.result()
            if status != 200:
                return error(status, result)
            return Response(result, media_type=MIME[kind], headers={"Cache-Control": "no-store"})
        finally:
            job.cancel()
            disconnect.cancel()
            await asyncio.gather(job, disconnect, return_exceptions=True)
            studio.pending.discard(job)

    async def transcribe(request):
        denied = authorize(request)
        if denied is not None:
            return denied
        if "stt" not in studio.workers:
            return error(503, "stt-not-configured")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
            return error(415, "expected-multipart")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_STT_AUDIO + MAX_BODY:
                    return error(413, "body-too-large")
            except ValueError:
                return error(422, "invalid-request")
        try:
            async with asyncio.timeout(15):
                form = await request.form(max_part_size=MAX_STT_AUDIO + 1)
        except TimeoutError:
            return error(408, "request-timeout")
        except ClientDisconnect:
            return Response(status_code=499)
        except Exception:
            return error(422, "invalid-request")
        try:
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                return error(422, "missing-file")
            audio = await upload.read(MAX_STT_AUDIO + 1)
            if not audio:
                return error(422, "empty-file")
            if len(audio) > MAX_STT_AUDIO:
                return error(413, "body-too-large")
            language_raw = form.get("language")
            if language_raw is not None and not isinstance(language_raw, str):
                return error(422, "invalid-language")
            language = (language_raw or "").strip()
            if len(language) > 16:
                return error(422, "invalid-language")
        finally:
            await form.close()
        if len(studio.pending) >= 3:
            return error(503, "busy")
        job = asyncio.create_task(studio.job("stt", {"audio": audio, "language": language}))
        studio.pending.add(job)
        disconnect = asyncio.create_task(disconnected(request))
        try:
            done, _ = await asyncio.wait((job, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                return Response(status_code=499)
            status, result = job.result()
            if status != 200:
                return error(status, result)
            return JSONResponse({"text": result}, headers={"Cache-Control": "no-store"})
        finally:
            job.cancel()
            disconnect.cancel()
            await asyncio.gather(job, disconnect, return_exceptions=True)
            studio.pending.discard(job)

    return Starlette(lifespan=lifespan, routes=[
        Route("/v1/health", health),
        Route("/v1/portrait/{outfit}", portrait),
        Route("/v1/voice", partial(submit, kind="voice"), methods=["POST"]),
        Route("/v1/video_note", partial(submit, kind="video"), methods=["POST"]),
        Route("/v1/image", partial(submit, kind="image"), methods=["POST"]),
        Route("/v1/audio/transcriptions", transcribe, methods=["POST"]),
    ])
