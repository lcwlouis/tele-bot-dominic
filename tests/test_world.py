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
