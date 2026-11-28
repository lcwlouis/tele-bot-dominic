"""OpenAI-compatible speech-to-text client for a local STT server (whisper.cpp, mlx-whisper, faster-whisper)."""

from __future__ import annotations

import logging
import struct
from urllib.parse import urlsplit

import httpx

from dom.usage import UsageRecorder, on_own_network

log = logging.getLogger("dom.stt")

_EXTENSIONS = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}


def _silent_wav(seconds: float = 0.2, sample_rate: int = 16000) -> bytes:
    """A tiny valid 16 kHz mono 16-bit PCM WAV of silence, used only for the dashboard's Test button."""
    frames = max(1, int(seconds * sample_rate))
    data = b"\x00\x00" * frames
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", len(data),
    )
    return header + data


def transcriptions_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/audio/transcriptions"):
        return clean
    return f"{clean}/audio/transcriptions"


class SttClient:
    """POST audio bytes to an OpenAI-compatible `/audio/transcriptions` endpoint."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        model: str = "whisper-1",
        api_key: str | None = None,
        language: str = "",
        *,
        usage: UsageRecorder | None = None,
        timeout: float = 60.0,
    ):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.url = transcriptions_url(self.base_url)
        self.host = (urlsplit(self.url).hostname or "").lower()
        self.model = model or "whisper-1"
        self.api_key = api_key or None
        self.language = language.strip()
        self.usage = usage
        self.timeout = timeout

    async def transcribe(self, audio: bytes, *, mime: str = "audio/ogg", filename: str | None = None) -> str:
        """Transcribe `audio` and return the text (empty string when the server hears no speech).
        Raises on network, HTTP or unexpected payload errors so the caller can fall back to `[models.media]`."""
        clean_mime = mime.partition(";")[0].strip().lower() or "audio/ogg"
        ext = _EXTENSIONS.get(clean_mime, ".ogg")
        name = filename or f"audio{ext}"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data: dict[str, str] = {"model": self.model}
        if self.language:
            data["language"] = self.language
        response = await self.http.post(
            self.url,
            headers=headers,
            data=data,
            files={"file": (name, audio, clean_mime)},
            timeout=self.timeout,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type or response.text.lstrip().startswith("{"):
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise ValueError("STT server returned an unexpected JSON payload")
            text = payload["text"].strip()
        else:
            text = response.text.strip()
        if self.usage is not None and not on_own_network(self.host):
            try:
                await self.usage.record_response(response, "media", self.model, self.host)
            except Exception:
                log.debug("could not record STT usage", exc_info=True)
        return text

    async def probe(self) -> str:
        """Send a 0.2s silent WAV to verify the endpoint accepts transcription requests. Returns a short status note."""
        text = await self.transcribe(_silent_wav(), mime="audio/wav", filename="probe.wav")
        return f"Answered ({text[:40]!r})" if text else "Answered (silence)"
