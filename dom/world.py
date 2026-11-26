"""What it is like outside for the owner right now: a weather line and a public-holiday line for Dom's turn note.

The place is the owner's `[world]` config. The weather is Open-Meteo's free forecast API (no key); only the coordinates
leave the host. The holidays are Nager.Date's free public-holiday API (no key); only the country code and the year
leave the host. Both are fetched in the background and cached, so the reply path only reads from memory, and every
failure is quiet: with no usable data there is no line.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo

import httpx

from dom.config import WorldConfig

log = logging.getLogger("dom.world")
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH = timedelta(minutes=45)  # a fetch is reused for this long
RETRY = timedelta(minutes=10)  # after a failed fetch
STALE = timedelta(hours=3)  # past this the line is dropped rather than shown out of date
TIMEOUT = 10.0
HOLIDAYS_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
HOLIDAY_REFRESH = timedelta(days=1)  # a holiday fetch is reused for a day
HOLIDAY_RETRY = timedelta(hours=1)  # after a failed one
HOLIDAY_STALE = timedelta(days=7)  # holidays rarely move, so old data serves a while; past this the line is dropped
HOLIDAY_AHEAD = timedelta(days=14)
HOLIDAY_NAME_MAX = 80
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


def parse_holidays(body: object) -> list[tuple[date, str]]:
    """The nationwide holidays in a Nager.Date year list, as (date, English name). Raises KeyError, TypeError or
    ValueError for a body that is not shaped as asked. The names are text from the web on its way into a prompt, so they
    are flattened to one short line."""
    if not isinstance(body, list):
        raise TypeError("holiday list expected")
    found = []
    for item in body:
        if item.get("global") is False:  # a holiday of some regions only is not the owner's day off
            continue
        name = " ".join(str(item["name"]).split())[:HOLIDAY_NAME_MAX]
        if name:
            found.append((date.fromisoformat(item["date"]), name))
    return found


def holiday_line(country: str, holidays: list[tuple[date, str]], today: date) -> str:
    """The turn-note line for today's public holiday and the next one within two weeks, or "" for neither."""
    parts = []
    if names := [name for day, name in holidays if day == today]:
        parts.append(f"Today is a public holiday in {country}: {' / '.join(names)}.")
    coming = sorted((day, name) for day, name in holidays if today < day <= today + HOLIDAY_AHEAD)
    if coming:
        day = coming[0][0]
        names = " / ".join(name for when, name in coming if when == day)
        away = (day - today).days
        parts.append(f"Next public holiday in {country}: {names}, {day:%A %Y-%m-%d} ({'tomorrow' if away == 1 else f'in {away} days'}).")
    return " ".join(parts)


class World:
    def __init__(
        self, config: Callable[[], WorldConfig], http: Callable[[], httpx.AsyncClient], now: Callable[[], datetime],
        tz: Callable[[], tzinfo] = lambda: timezone.utc,
    ) -> None:
        self._config, self._http, self._now, self._tz = config, http, now, tz
        self._line = ""
        self._for: tuple[str, float | None, float | None] | None = None  # the place the cached line is about
        self._fetched_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._fetching = False  # a fetch is under way; the check and the mark have no await between them
        self._holidays: dict[int, list[tuple[date, str]]] = {}  # by year, for the years the line can reach
        self._holidays_for = ""  # the country the cached holidays are about
        self._holidays_at: datetime | None = None
        self._holidays_retry_at: datetime | None = None
        self._holidays_fetching = False

    def line(self) -> str:
        """The weather line for the turn note, or "" when no place is set or nothing fresh is cached. Never waits."""
        config = self._config()
        if (not config.configured or self._for != _key(config) or self._fetched_at is None
                or self._now() - self._fetched_at > STALE):
            return ""
        return self._line

    def holiday_line(self) -> str:
        """Today's public holiday and the next within 14 days, or "" with no country, no holiday or no usable data.
        Never waits: it reads the cached year lists against today's date."""
        country = self._config().country
        now = self._now()
        if not country or self._holidays_for != country or self._holidays_at is None or now - self._holidays_at > HOLIDAY_STALE:
            return ""
        today = now.astimezone(self._tz()).date()  # the owner's date, not UTC's
        return holiday_line(country, [item for year in self._holidays.values() for item in year], today)

    async def refresh(self) -> None:
        """Fetch what is due: the forecast and the holidays, each with its own cache. Called from the background tick,
        never from a reply."""
        await asyncio.gather(self._refresh_weather(), self._refresh_holidays())

    async def _refresh_holidays(self) -> None:
        country = self._config().country
        now = self._now()
        if not country or self._holidays_fetching:
            return
        today = now.astimezone(self._tz()).date()
        years = sorted({today.year, (today + HOLIDAY_AHEAD).year})
        if (self._holidays_for == country and self._holidays_at is not None and now - self._holidays_at < HOLIDAY_REFRESH
                and all(year in self._holidays for year in years)):
            return
        if self._holidays_retry_at is not None and now < self._holidays_retry_at:
            return
        self._holidays_fetching = True
        try:
            try:
                fetched = {}
                for year in years:
                    response = await self._http().get(HOLIDAYS_URL.format(year=year, country=country), timeout=TIMEOUT)
                    response.raise_for_status()
                    fetched[year] = parse_holidays(response.json())
            except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError) as exc:
                log.info("holidays unavailable (%s)", type(exc).__name__)
                self._holidays_retry_at = now + HOLIDAY_RETRY
                return
            self._holidays, self._holidays_for, self._holidays_at, self._holidays_retry_at = fetched, country, now, None
        finally:
            self._holidays_fetching = False

    async def _refresh_weather(self) -> None:
        """Fetch the forecast if the cache is due."""
        config = self._config()
        now = self._now()
        if not config.configured or self._fetching:
            return
        if self._for == _key(config) and self._fetched_at is not None and now - self._fetched_at < REFRESH:
            return
        if self._retry_at is not None and now < self._retry_at:
            return
        self._fetching = True  # held only as this flag: the request below runs with no lock
        try:
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
        finally:
            self._fetching = False
