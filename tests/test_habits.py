from datetime import time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dom.home import Home
from dom.models import StoredMessage, VISIBILITY_OWNER_PRIVATE
from dom.presence import parse_habits
from tghelpers import FRIEND, GROUP, OWNER


def quiet(start, length):
    counts = [10] * 24
    for offset in range(length):
        counts[(start + offset) % 24] = 0
    return counts


def test_learns_window_across_midnight_and_limits_both_ends():
    from dom.habits import learn

    counts = quiet(23, 8)
    assert learn(counts, parse_habits("sleep: 23:15-07:15"), "normal") == (time(23), time(7))
    assert learn(counts, parse_habits("sleep: 01:00-09:00"), "normal") == (time(0, 30), time(8, 30))


@pytest.mark.parametrize(("chronotype", "expected"), [
    ("early", (time(22, 30), time(6, 30))),
    ("normal", (time(23), time(7))),
    ("late", (time(23, 30), time(7, 30))),
])
def test_chronotype_shifts_target(chronotype, expected):
    from dom.habits import learn

    current = parse_habits("sleep: 23:00-07:00")
    assert learn(quiet(23, 8), current, chronotype) == expected


def test_ties_favor_current_window_and_fewer_than_fifty_messages_do_nothing():
    from dom.habits import learn

    current = parse_habits("sleep: 23:00-07:00")
    assert learn([5] * 24, current, "normal") == (time(23), time(7))
    assert learn([2] * 24, current, "late") is None


@pytest.mark.parametrize(("sleep", "start", "length"), [
    ("23:59-04:00", 12, 4),
    ("00:00-10:59", 12, 11),
    ("21:30-22:30", 7, 4),
    ("20:00-12:00", 8, 11),
])
def test_sleep_duration_still_clamped(sleep, start, length):
    from dom.habits import learn

    begin, end = learn(quiet(start, length), parse_habits(f"sleep: {sleep}"), "normal")
    duration = (end.hour * 60 + end.minute - begin.hour * 60 - begin.minute) % (24 * 60)
    assert 4 * 60 <= duration <= 11 * 60


def test_write_only_changes_sleep_line(tmp_path):
    path = tmp_path / "memory" / "HABITS.md"
    path.parent.mkdir()
    path.write_bytes(b'# rhythm\r\n- sleep: "23:00-07:00"\r\nawake: 12\r\n\r\nlast line')
    Home(tmp_path).write_habits_line(time(22, 30), time(6, 30))
    assert path.read_bytes() == b'# rhythm\r\n- sleep: "22:30-06:30"\r\nawake: 12\r\n\r\nlast line'


async def test_counts_only_incoming_humans_in_approved_chats_in_local_hours(make_app):
    app = make_app()
    now = app.dispatch.now()
    await app.store.set_chat_auth_status(GROUP, "approved")
    await app.store.set_chat_auth_status(FRIEND, "pending_approval")

    async def record(msg_id, chat_id=GROUP, *, role="user", is_bot=False, delta=None):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(chat_id), msg_id=msg_id, role=role, is_bot=is_bot,
            visibility_scope=VISIBILITY_OWNER_PRIVATE if chat_id == OWNER else f"chat:{chat_id}",
            text="content is irrelevant", created_at=(now - (delta or timedelta())).isoformat(),
        ))

    await record(1)
    await record(2, OWNER, delta=timedelta(hours=5))
    await record(3, FRIEND)
    await record(4, role="assistant")
    await record(5, role="event")
    await record(6, is_bot=True)
    await record(7, delta=timedelta(days=15))
    await record(8, delta=timedelta(days=-1))
    counts = await app.store.hourly_message_counts(tz=ZoneInfo("Asia/Singapore"), now=now)
    assert len(counts) == 24 and sum(counts) == 2
    assert counts[20] == counts[15] == 1


async def test_daily_habit_job_writes_one_event_and_no_message(make_app, monkeypatch):
    from dom import habits

    app = make_app()
    now = app.dispatch.now

    async def counts(days=14, tz=timezone.utc, *, now=None):
        return quiet(22, 8)

    monkeypatch.setattr(app.store, "hourly_message_counts", counts)
    app.dispatch.jobs.register("habits", habits.due, lambda: habits.run(app))
    now.advance(minutes=-1)
    await app.dispatch.jobs.tick()
    assert app.home.read("memory/HABITS.md") == ""
    now.advance(minutes=1)
    await app.dispatch.jobs.tick()
    await app.dispatch.jobs.tick()
    assert app.home.read("memory/HABITS.md") == 'sleep: "23:29-07:00"\n'
    events = await app.store.get_recent_chat_window(OWNER)
    assert [(message.role, message.text) for message in events] == [
        ("event", "Dom's sleep moved to 23:29–07:00"),
    ]
    assert not app.bot.calls
