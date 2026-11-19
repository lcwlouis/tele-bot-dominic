from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dom.presence import Presence, parse_habits
from tghelpers import GROUP, Clock, FixedRandom


@pytest.fixture
def rhythm(make_app):
    app = make_app()
    clock, rng = Clock(), FixedRandom()
    return Presence(app.store, ZoneInfo("UTC"), rng, clock), clock, rng


@pytest.mark.parametrize(("hour", "minute", "second", "expected"), [
    (0, 30, 0, "asleep"),
    (23, 59, 30, "asleep"),
    (12, 0, 0, "offline"),
])
async def test_sleep_window_crosses_midnight(rhythm, hour, minute, second, expected):
    presence, clock, _ = rhythm
    clock.at = clock.at.replace(hour=hour, minute=minute, second=second)
    state, until = await presence.state(GROUP, parse_habits(""))
    assert state == expected
    if expected == "asleep":
        assert until > clock.at
        assert until.time() == time(7, 30)


def test_parse_habits_defaults_and_clamps():
    defaults = parse_habits("")
    assert (defaults.sleep_start, defaults.sleep_end) == (time(23, 59), time(7, 30))
    assert (defaults.online_minutes, defaults.offline_minutes) == ((5, 25), (15, 90))
    assert (defaults.wake_after_messages, defaults.kapoh_wake) == (5, 2.0)
    habits = parse_habits(
        'sleep: "23:00-01:00"\nonline_minutes: broken\n'
        'offline_minutes: 20-30\nwake_after_messages: 9\nkapoh_wake: 2.5'
    )
    assert (habits.sleep_start, habits.sleep_end) == (time(23), time(3))
    assert (habits.online_minutes, habits.offline_minutes) == ((5, 25), (20, 30))
    assert (habits.wake_after_messages, habits.kapoh_wake) == (9, 2.5)
    assert parse_habits("sleep: 20:00-12:00").sleep_end == time(7)


async def test_online_session_expires_to_offline(rhythm):
    presence, clock, _ = rhythm
    habits = parse_habits("")
    online_until = await presence.go_online(GROUP, habits, minutes=5)
    assert await presence.state(GROUP, habits) == ("online", online_until)
    clock.advance(minutes=5)
    state, until = await presence.state(GROUP, habits)
    assert state == "offline"
    assert until == clock.at + timedelta(minutes=52.5)
    clock.advance(hours=2)
    assert (await presence.state(GROUP, habits))[0] == "offline"


async def test_wake_overrides_sleep(rhythm):
    presence, clock, rng = rhythm
    clock.at = clock.at.replace(hour=2)
    habits = parse_habits("")
    assert (await presence.state(GROUP, habits))[0] == "asleep"
    until = await presence.wake(GROUP, habits)
    assert until == clock.at + timedelta(minutes=30)
    restarted = Presence(presence.store, ZoneInfo("UTC"), rng, clock)
    assert await restarted.state(GROUP, habits) == ("online", until)
    clock.advance(minutes=30)
    assert (await restarted.state(GROUP, habits))[0] == "asleep"


async def test_extend_online_capped(rhythm):
    presence, _, _ = rhythm
    habits = parse_habits("")
    online_until = await presence.go_online(GROUP, habits, minutes=5)
    assert await presence.extend_online(GROUP, 55) == online_until + timedelta(minutes=30)


async def test_triangular_uses_injected_rng(rhythm):
    presence, clock, rng = rhythm
    habits = parse_habits("")
    assert await presence.go_online(GROUP, habits) == clock.at + timedelta(minutes=15)
    assert await presence.go_offline(GROUP, habits) == clock.at + timedelta(minutes=52.5)
    assert rng.draws == [(5, 25, 21), (15, 90, 30)]
