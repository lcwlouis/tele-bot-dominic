"""What it is like outside for the owner right now: one weather line for Dom's turn note.

The place is the owner's `[world]` config. The data is Open-Meteo's free forecast API (no key); only the coordinates
leave the host. It is fetched in the background and cached, so the reply path only reads a string, and every failure
is quiet: with no fresh data there is no line.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from dom.config import WorldConfig

log = logging.getLogger("dom.world")
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH = timedelta(minutes=45)  # a fetch is reused for this long
RETRY = timedelta(minutes=10)  # after a failed fetch
STALE = timedelta(hours=3)  # past this the line is dropped rather than shown out of date
TIMEOUT = 10.0
HUMID_PERCENT = 75

# WMO weather interpretation codes, as Open-Meteo reports them, in plain words.
CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def _key(config: WorldConfig) -> tuple[str, float | None, float | None]:
    return config.place, config.latitude, config.longitude


def describe(place: str, data: dict) -> str:
    """The turn-note line for an Open-Meteo forecast body. Raises KeyError, TypeError or ValueError for one that is
    not shaped as asked."""
    now, today = data["current"], data["daily"]
    temperature, feels = round(float(now["temperature_2m"])), round(float(now["apparent_temperature"]))
    words = [CONDITIONS.get(int(now["weather_code"]), "")]
    if float(now["relative_humidity_2m"]) >= HUMID_PERCENT:
        words.append("humid")
    now_part = f"{temperature}°C" + (f" (feels like {feels}°C)" if abs(feels - temperature) >= 2 else "")
    line = f"Weather in {place} now: {', '.join([now_part, *filter(None, words)])}."
    low, high = round(float(today["temperature_2m_min"][0])), round(float(today["temperature_2m_max"][0]))
    outlook = f"Today {low}–{high}°C"
    rain = today.get("precipitation_probability_max", [None])[0]
    if rain is not None:
        outlook += f", {round(float(rain))}% chance of rain"
    stormy = int(today["weather_code"][0]) >= 95
    return f"{line} {outlook}{', thunderstorms possible' if stormy else ''}."


class World:
    def __init__(
        self, config: Callable[[], WorldConfig], http: Callable[[], httpx.AsyncClient], now: Callable[[], datetime],
    ) -> None:
        self._config, self._http, self._now = config, http, now
        self._line = ""
        self._for: tuple[str, float | None, float | None] | None = None  # the place the cached line is about
        self._fetched_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._lock = asyncio.Lock()

    def line(self) -> str:
        """The weather line for the turn note, or "" when no place is set or nothing fresh is cached. Never waits."""
        config = self._config()
        if (not config.configured or self._for != _key(config) or self._fetched_at is None
                or self._now() - self._fetched_at > STALE):
            return ""
        return self._line

    async def refresh(self) -> None:
        """Fetch the forecast if the cache is due. Called from the background tick, never from a reply."""
        config = self._config()
        now = self._now()
        if not config.configured or self._lock.locked():
            return
        if self._for == _key(config) and self._fetched_at is not None and now - self._fetched_at < REFRESH:
            return
        if self._retry_at is not None and now < self._retry_at:
            return
        async with self._lock:
            try:
                response = await self._http().get(
                    FORECAST_URL, timeout=TIMEOUT,
                    params={
                        "latitude": config.latitude, "longitude": config.longitude, "timezone": "auto",
                        "forecast_days": 1,
                        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    },
                )
                response.raise_for_status()
                line = describe(" ".join(config.place.split()), response.json())
            except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
                # The request URL holds the coordinates: log the kind of failure only.
                log.info("weather unavailable (%s)", type(exc).__name__)
                self._retry_at = now + RETRY
                return
            self._line, self._for, self._fetched_at, self._retry_at = line, _key(config), now, None
