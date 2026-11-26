"""Dom's "world now" block for his turn note: the weather where the owner is, and the life sheet.

The place is the owner's `[world]` config. The weather is Open-Meteo's free forecast API (no key); only the
coordinates leave the host. It is fetched in the background and cached, so the reply path only reads a string, and
every failure is quiet: with no fresh data there is no line.

`memory/WORLD.md` is Dom's own life sheet (home, work, routine by weekday, regular spots, projects). The block shows
the sheet, the routine slot for right now, what the visible notes say about the places in that slot, and a variety
hint when Dom has just brought up one of his usual options. It reaches a chat through the same tag rules as every
note (docs/memory-format.md): a line tagged to another chat never appears, and the block is built fresh for each turn
after the chat window, so it can never touch the cached prefix.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

import httpx

from dom import notes
from dom.config import WorldConfig
from dom.home import WORLD, Home, PrivateAccessError
from dom.models import Scope

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


SHEET_CHARS = 1200  # of the sheet's own lines in one turn note
SLOT_LINES = 3  # routine lines shown for right now
PLACE_CHARS = 300  # of what a place's note says
PLACE_LINES = 2  # places described per turn
VARIETY_DAYS = 2  # Dom's own recent messages in this chat that count as "lately"
VARIETY_MENTIONS = 4  # repeated options named in one hint
BLOCK_HEAD = "your world (your own life; use it when it fits, never recite it):"

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# The words a routine label may use for a part of the day, and the hours each part covers.
PARTS = {
    "morning": ("morning", "breakfast"), "lunch": ("lunch", "midday", "noon"),
    "afternoon": ("afternoon",), "evening": ("evening", "dinner"), "night": ("night", "late"),
}
_HOURS = (("morning", 5, 11), ("lunch", 11, 14), ("afternoon", 14, 17), ("evening", 17, 22))
_OPTION_SPLIT = re.compile(r",|;|/|\bor\b", re.IGNORECASE)
_LEAD_WORD = re.compile(r"^(?:usually|often|sometimes|mostly|maybe|either|a|an|the)\s+", re.IGNORECASE)


def part_of_day(hour: int) -> str:
    return next((name for name, start, end in _HOURS if start <= hour < end), "night")


def _words(label: str) -> set[str]:
    return set(re.findall(r"[a-z]+", label.lower()))


def slot_matches(label: str, now: datetime) -> bool:
    """Whether a routine line's label (`Thursday evening`, `Weekend mornings`, `Every day`) covers this moment. A label
    naming no day covers every day, and one naming no part of the day covers all of it."""
    words = {word.rstrip("s") if word not in DAYS else word for word in _words(label)}
    today = DAYS[now.weekday()]
    named_days = words & set(DAYS)
    groups = words & {"weekday", "weekend"}
    if named_days or groups:
        in_weekend = now.weekday() >= 5
        if not (today in named_days or ("weekend" in groups and in_weekend) or ("weekday" in groups and not in_weekend)):
            return False
    named_parts = {part for part, names in PARTS.items() if words & {name.rstrip("s") for name in names}}
    return not named_parts or part_of_day(now.hour) in named_parts


def options(text: str) -> list[str]:
    """The choices a routine or spot line lists after its label (`Lunch: cai fan, laksa or kopi and toast`)."""
    _, colon, rest = text.partition(":")
    if not colon:
        return []
    found = []
    for part in _OPTION_SPLIT.split(rest):
        part = _LEAD_WORD.sub("", part.strip().strip(".").strip())
        if 3 <= len(part) <= 40:
            found.append(part)
    return list(dict.fromkeys(found))


def _mentions(option: str, texts: Sequence[str]) -> int:
    pattern = re.compile(rf"(?<!\w){re.escape(option)}(?!\w)", re.IGNORECASE)
    return sum(1 for text in texts if pattern.search(text))


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
        self._fetching = False  # a fetch is under way; the check and the mark have no await between them

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


    def block(self, home: Home, scope: Scope, chat_id: int, window: Sequence, now: datetime) -> str:
        """The "world now" lines of the turn note: the weather, then Dom's life sheet. Reads files, never the network."""
        return "\n".join(filter(None, [self.line(), sheet_block(home, scope, chat_id, window, now, self._now())]))


def _lines(text: str, chat_id: int, owner_dm: bool) -> list[notes.Line]:
    """A note's lines as this chat may read them: the owner's DM reads all, every other chat only its own and the
    untagged and `{any}` ones. Filtered in code, whatever the prompt says."""
    return [notes.parse_line(raw) for raw in (text if owner_dm else notes.limit_to_chat(text, chat_id)).split("\n")]


def _read(home: Home, rel: str) -> str:
    try:
        return home.scoped(allow_private=False).read(rel)
    except (OSError, PrivateAccessError, ValueError):  # a missing or unreadable sheet is no sheet: never fail a turn
        return ""


def sheet_block(home: Home, scope: Scope, chat_id: int, window: Sequence, now: datetime, clock: datetime) -> str:
    """Dom's life sheet for one turn, or "" while it has no line this chat may read. `now` is local time; `clock` the
    time to measure "lately" against (Dom's own recent messages in this chat, taken from the window)."""
    owner_dm = scope == "owner_dm"
    sheet, section = [], ""
    slots: list[notes.Line] = []
    choices: dict[str, None] = {}
    for line in _lines(_read(home, WORLD), chat_id, owner_dm):
        body = line.body.strip()
        if body.startswith("#"):
            section = body.lstrip("#").strip().lower()
            continue
        if not notes.is_bullet(body):
            continue
        text = re.sub(r"^[-*]\s+", "", body).strip()
        if not text:
            continue
        if "routine" in section:
            if slot_matches(text.partition(":")[0], now):
                slots.append(notes.Line(text, entities=line.entities))
        else:
            sheet.append(text)
        if "routine" in section or "spot" in section:
            choices.update(dict.fromkeys(options(text)))
    if not sheet and not slots:
        return ""
    lines, size = [BLOCK_HEAD], 0
    for text in sheet:
        if size + len(text) > SHEET_CHARS:
            break
        size += len(text)
        lines.append(f"- {escape(text)}")
    if slots:
        lines.append("right now, by your routine: " + "; ".join(escape(slot.body[:200]) for slot in slots[:SLOT_LINES]))
        for place in _places(home, slots, chat_id, owner_dm):
            lines.append(place)
    if hint := _variety(list(choices), slots, window, clock):
        lines.append(hint)
    return "\n".join(lines)


def _places(home: Home, slots: list[notes.Line], chat_id: int, owner_dm: bool) -> list[str]:
    """What the notes this chat may read say about the places a routine slot names (`{entities place:slug}`)."""
    slugs = list(dict.fromkeys(
        ref.partition(":")[2] for slot in slots for ref in slot.entities if ref.startswith("place:")
    ))
    found = []
    for slug in slugs[:PLACE_LINES]:
        facts = [
            re.sub(r"^[-*]\s+", "", line.body.strip()) for line in _lines(_read(home, f"memory/places/{slug}.md"), chat_id, owner_dm)
            if notes.is_bullet(line.body)
        ]
        if facts:
            found.append(f"place, {slug.replace('-', ' ')}: " + escape(" ".join(facts)[:PLACE_CHARS]))
    return found


def _variety(choices: list[str], slots: list[notes.Line], window: Sequence, clock: datetime) -> str:
    """A nudge when Dom has just said one of his usual options in this chat, so lunch and dinner are not the same."""
    since = clock - timedelta(days=VARIETY_DAYS)
    said = [
        message.text for message in window
        if message.role == "assistant" and message.text and datetime.fromisoformat(message.created_at) >= since
    ]
    counted = {option: n for option in choices if (n := _mentions(option, said))}
    if not counted:
        return ""
    used = ", ".join(f"{option} ({'once' if n == 1 else f'{n} times'})" for option, n in
                     sorted(counted.items(), key=lambda item: -item[1])[:VARIETY_MENTIONS])
    fresh = [  # a routine line that lists a real choice (two or more) gives the ones not said lately
        option for slot in slots if len(listed := options(slot.body)) >= 2 for option in listed if option not in counted
    ][:VARIETY_MENTIONS]
    return escape(
        f"variety: in the last {VARIETY_DAYS} days here you already brought up {used}. Pick something different this "
        "time unless you have a reason, and say it if you do" + (f". Not lately: {', '.join(fresh)}" if fresh else "") + "."
    )
