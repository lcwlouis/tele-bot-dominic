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


def test_parse_habits_accepts_bullets_and_clamps_ranges():
    habits = parse_habits(
        '- sleep: "22:00-06:00"\n* online_minutes: 0-500\n  - offline_minutes: 1-9000\n'
        '\twake_after_messages: 500\nkapoh_wake: 9'
    )
    assert (habits.sleep_start, habits.sleep_end) == (time(22), time(6))
    assert (habits.online_minutes, habits.offline_minutes) == ((1, 120), (5, 600))
    assert (habits.wake_after_messages, habits.kapoh_wake) == (50, 3.0)
    low = parse_habits("wake_after_messages: 0\nkapoh_wake: 0.2\noffline_minutes: 1-3")
    assert (low.wake_after_messages, low.kapoh_wake, low.offline_minutes) == (3, 1.0, (5, 5))


@pytest.mark.parametrize("text", [
    "online_minutes: 30-10\noffline_minutes: 90-15\nwake_after_messages: many\nkapoh_wake: nan",
    "online_minutes: soon\noffline_minutes: 10\nkapoh_wake: inf",
])
def test_parse_habits_malformed_values_keep_defaults(text):
    habits = parse_habits(text)
    assert (habits.online_minutes, habits.offline_minutes) == ((5, 25), (15, 90))
    assert (habits.wake_after_messages, habits.kapoh_wake) == (5, 2.0)


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
    assert await presence.extend_online(GROUP, habits, 55) == online_until + timedelta(minutes=30)


async def test_triangular_uses_injected_rng(rhythm):
    presence, clock, rng = rhythm
    habits = parse_habits("")
    assert await presence.go_online(GROUP, habits) == clock.at + timedelta(minutes=15)
    assert await presence.go_offline(GROUP, habits) == clock.at + timedelta(minutes=52.5)
    assert rng.draws == [(5, 25, 21), (15, 90, 30)]


async def test_extending_cannot_pass_two_hours_from_the_start_of_the_session(rhythm):
    presence, clock, _ = rhythm
    habits = parse_habits("")
    await presence.go_online(GROUP, habits, minutes=5)
    ends = [await presence.extend_online(GROUP, habits, 30) for _ in range(6)]
    assert [end.strftime("%H:%M") for end in ends] == ["12:35", "13:05", "13:35", "14:00", "14:00", "14:00"]
    assert (await presence.state(GROUP, habits)) == ("online", clock.at.replace(hour=14))


async def test_extending_stops_at_the_sleep_window(rhythm):
    presence, clock, _ = rhythm
    habits = parse_habits("")
    clock.at = clock.at.replace(hour=23, minute=0)
    await presence.go_online(GROUP, habits, minutes=30)
    assert (await presence.extend_online(GROUP, habits, 30)).strftime("%H:%M") == "23:59"
    assert (await presence.extend_online(GROUP, habits, 30)).strftime("%H:%M") == "23:59"


async def test_owner_wake_session_may_extend_past_the_sleep_window_but_not_two_hours(rhythm):
    presence, clock, _ = rhythm
    habits = parse_habits("")
    clock.at = clock.at.replace(hour=23, minute=40)
    await presence.wake(GROUP, habits)
    ends = [await presence.extend_online(GROUP, habits, 30) for _ in range(5)]
    assert [end.strftime("%d %H:%M") for end in ends] == ["02 00:40", "02 01:10", "02 01:40", "02 01:40", "02 01:40"]
    assert (await presence.state(GROUP, habits))[0] == "online"


async def test_sleep_lasts_until_wake_time_or_eight_hours(rhythm):
    presence, clock, _ = rhythm
    habits = parse_habits("")
    assert await presence.sleep(GROUP, habits) == clock.at.replace(hour=20)  # next 07:30 is 19.5 h away
    assert (await presence.state(GROUP, habits)) == ("asleep", clock.at.replace(hour=20))
    clock.at = clock.at.replace(hour=2)
    assert await presence.sleep(GROUP, habits) == clock.at.replace(hour=7, minute=30)


async def test_session_end_is_reported_once_per_online_session(rhythm):
    presence, clock, _ = rhythm
    ended = []
    presence.on_session_end = ended.append
    habits = parse_habits("")
    await presence.go_online(GROUP, habits, minutes=5)
    await presence.state(GROUP, habits)
    assert ended == []
    clock.advance(minutes=5)
    await presence.state(GROUP, habits)
    await presence.state(GROUP, habits)
    assert ended == [GROUP]


async def test_session_end_is_reported_when_sent_to_sleep_or_when_sleep_hours_begin(rhythm):
    presence, clock, _ = rhythm
    ended = []
    presence.on_session_end = ended.append
    habits = parse_habits("")
    await presence.sleep(GROUP, habits)  # was not online: nothing ended
    assert ended == []
    await presence.go_online(GROUP, habits, minutes=60)
    await presence.sleep(GROUP, habits)
    assert ended == [GROUP]
    clock.at = clock.at.replace(hour=23, minute=30)
    await presence.go_online(GROUP, habits, minutes=60)
    clock.advance(minutes=31)  # past 23:59: sleep hours cut the session short
    assert (await presence.state(GROUP, habits))[0] == "asleep"
    assert ended == [GROUP, GROUP]
