"""HTTP client for the owner's local media generation service."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Callable

import httpx

from dom.config import StudioConfig

log = logging.getLogger(__name__)
HEALTH_TTL = 60
AWAY_SECONDS = 300
MAX_MEDIA_BYTES = 25 * 1024 * 1024


class StudioClient:
    def __init__(self, config: StudioConfig, token: str, http: httpx.AsyncClient,
                 clock: Callable[[], float] = monotonic):
        self.config = config
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        self._clock = clock
        self._health: dict | None = None
        self._health_until = 0.0
        self._probing = False
        self._health_failures = 0  # health probes failed in a row (a connect error resets it: the studio is away)
        self._away_until = 0.0
        self._away_since: datetime | None = None
        self._busy_until = 0.0

    @property
    def health(self) -> dict | None:
        # A probe in flight keeps the last good health: it expires when a probe is due, not while one is running.
        return self._health if self._probing or self._clock() < self._health_until else None

    @property
    def ready(self) -> bool:
        return self.health is not None and self._clock() >= max(self._busy_until, self._away_until)

    @property
    def status(self) -> str:
        if self._away_since is not None:
            return f"away since {self._away_since:%H:%M} UTC"
        if self._clock() < self._busy_until:
            return "busy"
        return "on" if self.ready else "away"

    def model_for(self, worker: str) -> str:
        """The model a studio worker ("voice", "image", "video" or "special") runs, when the studio's health says."""
        models = (self.health or {}).get("models")
        model = models.get(worker) if isinstance(models, dict) else None
        return model if isinstance(model, str) and model else "local-studio"

    async def _request(self, path: str, timeout: float, *, payload: dict | None = None,
                       mime: str = "application/json") -> bytes | None:
        if self._clock() < self._away_until:
            return None
        name = path.split("/")[0]  # the request kind (a portrait's outfit is left out of the log)
        try:
            async with asyncio.timeout(timeout):
                async with self._http.stream(
                    "GET" if payload is None else "POST", self.config.base_url + "/v1/" + path,
                    json=payload, headers=self._headers, timeout=timeout, follow_redirects=False,
                ) as response:
                    if response.status_code == 503 and not path.startswith("portrait/"):
                        # The renderer is busy or short of memory. A missing portrait is not: it pauses nothing.
                        self._busy_until = self._clock() + 15
                    response.raise_for_status()
                    if response.headers.get("content-type", "").split(";")[0].strip().lower() != mime:
                        raise ValueError("unexpected content type")
                    maximum = 64 * 1024 if mime == "application/json" else MAX_MEDIA_BYTES
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > maximum:
                            raise ValueError("response too large")
                        data.extend(chunk)
                    if not data:
                        raise ValueError("empty response")
                    return bytes(data)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            # Only a studio that cannot be reached is away. A slow render (a special note can take minutes) or a
            # dropped stream fails this one request without pausing the others.
            self._away_since = self._away_since or datetime.now(timezone.utc)
            self._away_until = self._clock() + AWAY_SECONDS
            self._health = None
            self._health_failures = 0
            log.warning("studio %s connection failed (%s)", name, type(error).__name__)
        except (httpx.TransportError, TimeoutError) as error:
            self._log_failure(name, "timed out or broke off", error)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            self._log_failure(name, "failed", error)
        return None

    def _log_failure(self, name: str, what: str, error: BaseException | str) -> None:
        """One failed health probe is Wi-Fi noise (INFO); a second in a row, or any other request, is a WARNING."""
        quiet = name == "health" and self._health_failures == 0
        detail = error if isinstance(error, str) else type(error).__name__
        log.log(logging.INFO if quiet else logging.WARNING, "studio %s request %s (%s)", name, what, detail)

    async def refresh(self) -> None:
        if self._probing or self._clock() < max(self._health_until, self._away_until):
            return
        self._probing = True
        try:
            data = await self._request("health", self.config.timeouts.health)
            self._health_until = self._clock() + HEALTH_TTL
            health = None
            if data is not None:
                try:
                    health = json.loads(data)
                    if not isinstance(health, dict):
                        raise ValueError("expected an object")
                except ValueError:
                    health = None
                    self._log_failure("health", "returned invalid JSON", "ValueError")
            if health is not None:
                self._health, self._health_failures, self._away_since = health, 0, None
            elif self._clock() < self._away_until:
                pass  # the request found the studio unreachable and already marked it away
            else:
                # One failed probe keeps the last good health for one more interval; two in a row clear it.
                self._health_failures += 1
                if self._health_failures >= 2:
                    self._health = None
        finally:
            self._probing = False

    async def voice(self, text: str) -> bytes | None:
        return await self._request("voice", self.config.timeouts.voice, payload={"text": text}, mime="audio/ogg")

    @property
    def takes_still(self) -> bool:
        """The studio can film a note from a fresh still (0.8.4 on); an older one only animates the portrait."""
        return bool(self.health and self.health.get("video_still"))

    async def video(self, text: str, special: bool, outfit: str | None, still: bytes | None = None) -> bytes | None:
        payload = {"text": text, "special": special}
        if outfit is not None:
            payload["outfit"] = outfit
        if still is not None and self.takes_still:  # an older studio refuses the field, so it is never sent to one
            payload["still"] = base64.b64encode(still).decode("ascii")
        timeout = self.config.timeouts.special if special else self.config.timeouts.video_notes
        return await self._request("video_note", timeout, payload=payload, mime="video/mp4")

    async def image(self, scene: str, outfit: str | None) -> bytes | None:
        payload = {"scene": scene}
        if outfit is not None:
            payload["outfit"] = outfit
        return await self._request("image", self.config.timeouts.images, payload=payload, mime="image/png")

    async def portrait(self, outfit: str) -> bytes | None:
        return await self._request(f"portrait/{outfit}", self.config.timeouts.portrait, mime="image/png")
