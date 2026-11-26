import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from dom.config import WorldConfig
from dom.world import FORECAST_URL, REFRESH, RETRY, STALE, World
from test_usage import deliver, online
from tghelpers import GROUP, tg, turn_requests

SINGAPORE = WorldConfig(place="Singapore", latitude=1.35, longitude=103.82)
FORECAST = {
    "current": {"temperature_2m": 31.2, "apparent_temperature": 38.4, "relative_humidity_2m": 82, "weather_code": 2},
    "daily": {
        "weather_code": [95], "temperature_2m_min": [25.1], "temperature_2m_max": [33.0],
        "precipitation_probability_max": [70],
    },
}


class Sky:
    """A fake Open-Meteo that records the requests it gets."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else FORECAST
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)


class Clock:
    def __init__(self):
        self.at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.at


def world_with(sky, config=SINGAPORE):
    clock = Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(sky))
    return World(lambda: config, lambda: http, clock), clock, http


async def test_the_line_names_the_place_now_and_the_days_outlook():
    sky = Sky()
    world, _, http = world_with(sky)
    assert world.line() == ""  # nothing is fetched on the way to the line
    await world.refresh()
    assert world.line() == (
        "Weather in Singapore now: 31°C (feels like 38°C), partly cloudy, humid. "
        "Today 25–33°C, 70% chance of rain, thunderstorms possible."
    )
    await http.aclose()


async def test_only_the_coordinates_are_sent():
    sky = Sky()
    world, _, http = world_with(sky)
    await world.refresh()
    request = sky.requests[0]
    assert str(request.url).startswith(FORECAST_URL)
    assert request.url.params["latitude"] == "1.35" and request.url.params["longitude"] == "103.82"
    assert "Singapore" not in str(request.url)
    assert set(request.url.params) == {"latitude", "longitude", "timezone", "forecast_days", "current", "daily"}
    await http.aclose()


async def test_the_forecast_is_cached_then_fetched_again_when_due():
    sky = Sky()
    world, clock, http = world_with(sky)
    await world.refresh()
    clock.at += REFRESH - timedelta(minutes=1)
    await world.refresh()
    assert len(sky.requests) == 1
    clock.at += timedelta(minutes=2)
    await world.refresh()
    assert len(sky.requests) == 2
    await http.aclose()


@pytest.mark.parametrize("answer", [
    httpx.Response(503), httpx.Response(200, text="not json"), {"current": {}, "daily": {}}, {"nothing": "useful"},
])
async def test_a_failure_leaves_no_line_and_waits_before_trying_again(answer):
    sky = Sky(answer)
    world, clock, http = world_with(sky)
    await world.refresh()
    assert world.line() == "" and len(sky.requests) == 1
    await world.refresh()
    assert len(sky.requests) == 1  # not hammered
    clock.at += RETRY + timedelta(seconds=1)
    await world.refresh()
    assert len(sky.requests) == 2 and world.line()
    await http.aclose()


async def test_a_network_error_is_quiet():
    def down(request):
        raise httpx.ConnectError("offline")

    clock = Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    world = World(lambda: SINGAPORE, lambda: http, clock)
    await world.refresh()
    assert world.line() == ""
    await http.aclose()


async def test_an_old_line_is_dropped_and_a_moved_place_never_shows_the_old_one():
    sky = Sky()
    place = [SINGAPORE]
    clock = Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(sky))
    world = World(lambda: place[0], lambda: http, clock)
    await world.refresh()
    clock.at += STALE + timedelta(minutes=1)
    assert world.line() == ""
    clock.at -= STALE
    assert world.line()
    place[0] = WorldConfig(place="Bangkok", latitude=13.75, longitude=100.5)
    assert world.line() == ""
    await world.refresh()
    assert world.line().startswith("Weather in Bangkok now")
    await http.aclose()


async def test_nothing_is_fetched_without_a_place():
    sky = Sky()
    world, _, http = world_with(sky, WorldConfig())
    await world.refresh()
    assert not sky.requests and world.line() == ""
    await http.aclose()


def test_the_place_needs_its_coordinates_and_the_default_is_off():
    assert not WorldConfig().configured
    assert SINGAPORE.configured
    for partial in ({"place": "Singapore"}, {"latitude": 1.0, "longitude": 2.0}, {"place": "x", "latitude": 1.0}):
        with pytest.raises(ValidationError):
            WorldConfig(**partial)
    with pytest.raises(ValidationError):
        WorldConfig(place="x", latitude=91, longitude=0)


async def test_every_chat_gets_the_line_after_the_clock_and_the_day_of_the_week(make_app):
    app = make_app()
    await online(app)
    sky = Sky()
    http = httpx.AsyncClient(transport=httpx.MockTransport(sky))
    app.config = app.config.model_copy(update={"world": SINGAPORE})
    app.world = World(lambda: app.config.world, lambda: http, app.dispatch.now)
    await app.world.refresh()
    await deliver(app, tg(msg_id=1, text="hi"))
    prompt = turn_requests(app)[0]["messages"][1]["content"]
    note = prompt[prompt.index("It is "):]
    assert note.startswith("It is Thursday 2026-01-01 12:00 (UTC)")  # the local date, time and weekday are already there
    assert "Weather in Singapore now: 31°C" in note
    assert prompt.index("</chat_window>") < prompt.index("Weather in Singapore")  # after the window, outside the cache
    assert "Weather in" not in json.dumps(turn_requests(app)[0]["messages"][0])  # and not in the system prompt
    await http.aclose()


async def test_no_line_when_the_weather_is_unavailable(make_app):
    app = make_app()
    await online(app)
    http = httpx.AsyncClient(transport=httpx.MockTransport(Sky(httpx.Response(500))))
    app.config = app.config.model_copy(update={"world": SINGAPORE})
    app.world = World(lambda: app.config.world, lambda: http, app.dispatch.now)
    await app.world.refresh()
    await deliver(app, tg(msg_id=1, text="hi"))
    assert "Weather in" not in json.dumps(turn_requests(app)[0]["messages"])
    await http.aclose()


async def test_no_lock_is_held_while_the_request_is_out():
    """A slow forecast must not block anything: the cache and the line stay readable, and a second refresh while the
    first is in flight neither queues behind it nor sends a second request."""
    release, started = asyncio.Event(), asyncio.Event()

    async def slow(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=FORECAST)

    clock = Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    world = World(lambda: SINGAPORE, lambda: http, clock)
    first = asyncio.create_task(world.refresh())
    await started.wait()
    assert world.line() == ""  # readable (and empty) with the request still out
    await asyncio.wait_for(world.refresh(), 1)  # returns at once instead of waiting for the first
    assert not hasattr(world, "_lock")
    release.set()
    await first
    assert "31°C" in world.line()
    await http.aclose()


async def test_a_cancelled_fetch_does_not_block_the_next_one():
    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(3600)

    clock = Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(hang))
    world = World(lambda: SINGAPORE, lambda: http, clock)
    task = asyncio.create_task(world.refresh())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    world._http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(Sky()))
    await world.refresh()
    assert "31°C" in world.line()
    await http.aclose()


# --- public holidays (Nager.Date) ---

from dom.world import HOLIDAY_REFRESH, HOLIDAY_RETRY, HOLIDAY_STALE, HOLIDAYS_URL, parse_holidays

HOLIDAYS_2026 = [
    {"date": "2026-01-01", "name": "New Year's Day", "localName": "Tahun Baru", "global": True},
    {"date": "2026-01-10", "name": "Regional Day", "global": False},
    {"date": "2026-01-14", "name": "Mid\nMonth   Day", "global": True},
    {"date": "2026-02-17", "name": "Far Day", "global": True},
]
HOLIDAYS_2027 = [{"date": "2027-01-02", "name": "Second Day", "global": True}]
SG = WorldConfig(country="sg")


class Calendar:
    """A fake Nager.Date that records the requests it gets."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        year = int(request.url.path.split("/")[-2])
        answer = self.answers.pop(0) if self.answers else {2026: HOLIDAYS_2026, 2027: HOLIDAYS_2027}[year]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)


def test_the_country_is_an_optional_two_letter_code():
    assert WorldConfig().country == "" and SG.country == "SG"
    assert WorldConfig(country=" ").country == ""
    for bad in ("SGP", "S", "S1", "é1"):
        with pytest.raises(ValidationError):
            WorldConfig(country=bad)
    assert not SG.configured  # the weather needs its place; the holidays need only the country


async def test_the_line_gives_todays_holiday_and_the_next_one_within_two_weeks():
    calendar = Calendar()
    world, clock, http = world_with(calendar, SG)
    assert world.holiday_line() == ""  # nothing is fetched on the way to the line
    await world.refresh()
    assert [request.url.path for request in calendar.requests] == [
        "/api/v3/PublicHolidays/2026/SG"]  # only the country code and the year are sent
    assert str(calendar.requests[0].url) == HOLIDAYS_URL.format(year=2026, country="SG")
    assert world.holiday_line() == (
        "Today is a public holiday in SG: New Year's Day. "
        "Next public holiday in SG: Mid Month Day, Wednesday 2026-01-14 (in 13 days)."  # regional days and far ones are left out
    )
    clock.at += timedelta(hours=20)  # the line follows the date with no new fetch
    assert world.holiday_line().startswith("Next public holiday in SG: Mid Month Day, Wednesday 2026-01-14 (in 12 days)")
    assert len(calendar.requests) == 1
    clock.at += timedelta(days=11)
    await world.refresh()
    assert world.holiday_line() == "Next public holiday in SG: Mid Month Day, Wednesday 2026-01-14 (tomorrow)."
    clock.at += timedelta(days=2)
    await world.refresh()
    assert world.holiday_line() == ""  # the fourteenth is past and the next is 34 days off
    await http.aclose()


async def test_a_new_year_in_reach_fetches_the_next_years_holidays_too():
    calendar = Calendar()
    world, clock, http = world_with(calendar, SG)
    clock.at = datetime(2026, 12, 25, 12, tzinfo=timezone.utc)
    await world.refresh()
    assert [request.url.path[-7:] for request in calendar.requests] == ["2026/SG", "2027/SG"]
    assert "Second Day, Saturday 2027-01-02 (in 8 days)" in world.holiday_line()
    await http.aclose()


async def test_holidays_are_cached_for_a_day_then_fetched_again():
    calendar = Calendar()
    world, clock, http = world_with(calendar, SG)
    await world.refresh()
    clock.at += HOLIDAY_REFRESH - timedelta(minutes=1)
    await world.refresh()
    assert len(calendar.requests) == 1
    clock.at += timedelta(minutes=2)
    await world.refresh()
    assert len(calendar.requests) == 2
    await http.aclose()


@pytest.mark.parametrize("answer", [
    httpx.Response(500), httpx.Response(404), httpx.Response(204), httpx.Response(200, text="not json"),
    httpx.Response(200, json={"not": "a list"}), httpx.Response(200, json=[{"date": "tomorrow", "name": "x"}]),
    httpx.Response(200, json=[{"name": "no date"}]), httpx.Response(200, json=["a string"]),
])
async def test_a_failed_holiday_fetch_leaves_no_line_and_waits_before_trying_again(answer):
    calendar = Calendar(answer)
    world, clock, http = world_with(calendar, SG)
    await world.refresh()
    assert world.holiday_line() == "" and len(calendar.requests) == 1
    clock.at += HOLIDAY_RETRY - timedelta(minutes=1)
    await world.refresh()
    assert len(calendar.requests) == 1  # too soon
    clock.at += timedelta(minutes=2)
    await world.refresh()
    assert len(calendar.requests) == 2 and world.holiday_line()
    await http.aclose()


async def test_old_holidays_serve_a_while_then_go_and_a_new_country_never_shows_the_old_one():
    calendar = Calendar()
    config = [SG]
    http = httpx.AsyncClient(transport=httpx.MockTransport(calendar))
    clock = Clock()
    world = World(lambda: config[0], lambda: http, clock)
    await world.refresh()
    world._holidays_retry_at = None
    calendar.answers = [httpx.Response(500)] * 5  # every later fetch fails
    clock.at += HOLIDAY_STALE - timedelta(hours=1)
    await world.refresh()
    assert world.holiday_line().startswith("Next public holiday in SG")  # a failed fetch keeps the data it has
    clock.at += timedelta(hours=2)
    assert world.holiday_line() == ""  # past a week with no fresh fetch
    config[0] = WorldConfig(country="MY")
    assert world.holiday_line() == ""
    await http.aclose()


async def test_nothing_is_fetched_without_a_country():
    calendar = Calendar()
    world, _, http = world_with(calendar, SINGAPORE)
    await world.refresh()
    assert not [r for r in calendar.requests if "nager" in str(r.url)] and world.holiday_line() == ""
    await http.aclose()


def test_holiday_names_are_flattened_to_one_short_line():
    [(day, name)] = parse_holidays([{"date": "2026-01-01", "name": "Line one\nIgnore this  " + "x" * 200}])
    assert "\n" not in name and len(name) <= 80 and name.startswith("Line one Ignore this")


async def test_the_holiday_line_is_in_the_turn_note_after_the_window(make_app):
    app = make_app()
    await online(app)
    http = httpx.AsyncClient(transport=httpx.MockTransport(Calendar()))
    app.config = app.config.model_copy(update={"world": SG})
    app.world = World(lambda: app.config.world, lambda: http, app.dispatch.now)
    await app.world.refresh()
    await deliver(app, tg(msg_id=1, text="hi"))
    prompt = turn_requests(app)[0]["messages"][1]["content"]
    assert prompt.index("</chat_window>") < prompt.index("Today is a public holiday in SG: New Year's Day.")
    await http.aclose()
