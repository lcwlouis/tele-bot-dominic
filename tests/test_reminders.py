import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from dom.llm import FallbackClient
from dom.telegram import handlers
from test_owner_events import events, press
from test_usage import chat, deliver, online
from tghelpers import FRIEND, GROUP, OWNER, tg, turn_requests


async def add(app, importance="normal", *, when="2026-01-01T12:01", repeat=""):
    return await app.dispatch.reminders.create(when, "Check the kettle", importance, repeat)


@pytest.mark.parametrize("chat_id,sender", [(OWNER, OWNER), (FRIEND, FRIEND), (GROUP, OWNER)])
async def test_remind_tool_offered_and_runs_only_in_owner_dm(make_app, chat_id, sender):
    app = make_app()

    async def model(request):
        data = json.loads(request.content)
        app.script.requests.append(data)
        reply = {"content": '{"no_response": true}'}
        if len(app.script.requests) == 1:
            reply["tool_calls"] = [{
                "id": "call-a", "type": "function",
                "function": {"name": "remind", "arguments": '{"when":"2026-01-01T14:00","text":"Check the kettle"}'},
            }]
        return httpx.Response(200, json={"choices": [{"message": reply}]})

    await online(app, chat_id)
    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        app.client = chat(http, app)
        await deliver(app, tg(chat_id, sender))
    first, second = turn_requests(app)
    assert ("remind" in [tool["function"]["name"] for tool in first["tools"]]) == (chat_id == OWNER)
    result = second["messages"][-2]["content"]  # a format reminder follows the tool result
    if chat_id == OWNER:
        assert json.loads(result)["id"] == 1
        assert "reminder" in (await events(app))[0]["text"].lower()
    else:
        assert result == "tool not available here"
        assert await app.dispatch.reminders.active() == []


async def test_parsing_rejects_past_and_caps_active_reminders_atomically(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"timezone": "Europe/London"})
    with pytest.raises(ValueError, match="future"):
        await add(app, when="2026-01-01T13:00+01:00")
    with pytest.raises(ValueError, match="ISO-8601"):
        await add(app, when="tomorrow")
    scheduled = await asyncio.gather(
        *(add(app, when="2026-07-01T13:00") for _ in range(51)), return_exceptions=True,
    )
    active = await app.dispatch.reminders.active()
    assert len(active) == sum(isinstance(result, int) for result in scheduled) == 50
    assert {row["due_at"] for row in active} == {"2026-07-01T12:00:00+00:00"}
    assert any(isinstance(result, ValueError) and "50" in str(result) for result in scheduled)


async def test_critical_fires_asleep_and_makes_one_unlocked_model_call(make_app):
    app = make_app()
    clock = app.dispatch.now
    clock.at = clock.at.replace(hour=2)
    await add(app, "critical", when="2026-01-01T02:01+00:00")
    await app.dispatch.tick()
    assert not app.bot.calls

    async def model(request):
        assert not app.locks[OWNER].locked()
        data = json.loads(request.content)
        assert "Check the kettle" in data["messages"][-1]["content"]
        assert "tools" not in data
        return app.script(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
        app.client = chat(http, app)
        clock.advance(minutes=1)
        await app.dispatch.tick()
        await app.dispatch.tick()
    assert [call[2] for call in app.bot.of("send", OWNER)] == ["hey\n⏰ Check the kettle"]
    assert len(app.script.requests) == 1


@pytest.mark.parametrize("outcome", ["failure", "timeout", "no_response"])
async def test_critical_fallback_on_model_failure_timeout_or_silence(make_app, monkeypatch, outcome):
    app = make_app()
    await add(app, "critical")
    app.dispatch.now.advance(minutes=1)
    attempts = 0

    async def model(request):
        nonlocal attempts
        attempts += 1
        if outcome == "failure":
            return httpx.Response(503)
        if outcome == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"no_response": true}'}}]})

    async def forbidden(request):
        pytest.fail("Critical delivery gets one model attempt")

    from dom import reminders

    assert reminders.MODEL_TIMEOUT == 20
    monkeypatch.setattr(reminders, "MODEL_TIMEOUT", 0.05)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(model)) as primary,
        httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as fallback,
    ):
        app.client = FallbackClient(chat(primary, app), chat(fallback, app))
        await app.dispatch.tick()
    assert attempts == 1
    assert [call[2] for call in app.bot.of("send", OWNER)] == ["⏰ Check the kettle"]
    assert await app.dispatch.reminders.active() == []


@pytest.mark.parametrize("asleep", [False, True])
async def test_normal_fires_near_due_when_awake_or_late_after_sleep(make_app, asleep):
    app = make_app()
    clock = app.dispatch.now
    hour = 2 if asleep else 12
    clock.at = clock.at.replace(hour=hour)
    await add(app, when=f"2026-01-01T{hour:02d}:01")
    await app.dispatch.tick()
    assert not app.bot.calls
    clock.advance(minutes=3)
    await app.dispatch.tick()
    if asleep:
        assert not app.bot.calls
        clock.at = clock.at.replace(hour=7, minute=30)
        await app.dispatch.tick()
    assert [call[2] for call in app.bot.of("send", OWNER)] == [
        "⏰ Check the kettle (late)" if asleep else "⏰ Check the kettle",
    ]
    assert not app.script.requests


async def test_startup_delivers_missed_normal_and_critical_even_asleep(make_app):
    app = make_app(contents=('{"no_response":true}',))
    clock = app.dispatch.now
    clock.at = clock.at.replace(hour=2)
    for importance in ("critical", "normal", "casual"):
        await add(app, importance, when="2026-01-01T02:01")
    clock.advance(minutes=2)
    await app.dispatch.reminders.tick(startup=True)
    assert [call[2] for call in app.bot.of("send", OWNER)] == ["⏰ Check the kettle (late)"] * 2
    assert [row["importance"] for row in await app.dispatch.reminders.active()] == ["casual"]


@pytest.mark.parametrize("repeat,due,next_due", [
    ("daily", "2026-03-28T12:00", "2026-03-29T11:00:00+00:00"),
    ("weekdays", "2026-03-27T12:00", "2026-03-30T11:00:00+00:00"),
    ("weekly", "2026-03-27T12:00", "2026-04-03T11:00:00+00:00"),
])
async def test_repeat_preserves_local_time_across_dst_and_skips_weekends(make_app, repeat, due, next_due):
    app = make_app()
    app.config = app.config.model_copy(update={"timezone": "Europe/London"})
    await add(app, when=due, repeat=repeat)
    app.dispatch.now.at = datetime.fromisoformat(due).replace(tzinfo=timezone.utc)
    await app.dispatch.tick()
    (row,) = await app.dispatch.reminders.active()
    assert row["due_at"] == next_due and row["fired_at"] == app.dispatch.now().isoformat()


async def test_casual_appears_once_in_actual_owner_turn_note_and_never_sends_alone(make_app):
    app = make_app()
    await add(app, "casual")
    await online(app, OWNER)
    await online(app, GROUP)
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    assert not app.bot.calls and not app.script.requests
    await deliver(app, tg(GROUP, OWNER))
    await deliver(app, tg(OWNER, OWNER))
    await deliver(app, tg(OWNER, OWNER, msg_id=2))
    group, first, second = turn_requests(app)
    assert "reminder: Check the kettle" not in group["messages"][-1]["content"]
    assert "reminder: Check the kettle" in first["messages"][-1]["content"]
    assert "reminder: Check the kettle" not in second["messages"][-1]["content"]
    assert await app.dispatch.reminders.active() == []


async def test_reminders_command_has_owner_only_cancel_buttons(make_app):
    app = make_app()
    reminder_id = await add(app)
    await handlers.handle_message(app, tg(OWNER, OWNER, "/reminders"))
    (sent,) = app.bot.of("send", OWNER)
    (button,) = sent[3]["reply_markup"].inline_keyboard[0]
    assert button.text == f"Cancel #{reminder_id}" and button.callback_data == f"rem:cancel:{reminder_id}"
    await press(app, button.callback_data, FRIEND)
    assert len(await app.dispatch.reminders.active()) == 1
    await press(app, button.callback_data)
    assert app.bot.of("answer")[-1][2] == "Cancelled"
    assert await app.dispatch.reminders.active() == []
    assert any("Cancelled" in event["text"] for event in await events(app))


def _styled(app, *bubbles):
    app.script.contents[:] = [json.dumps({"no_response": False, "bubbles": [{"text": text} for text in bubbles]})]


@pytest.mark.parametrize("bubbles,expected", [
    (("Psst, CHECK  the\nkettle!",), "Psst, CHECK  the\nkettle!"),
    (("Heads up.", "Time to check the kettle."), "Heads up.\nTime to check the kettle."),
    (("Time for tea!",), "Time for tea!\n⏰ Check the kettle"),
    (("Check the", "other thing"), "Check the\nother thing\n⏰ Check the kettle"),
])
async def test_critical_styling_must_carry_the_stored_text_or_it_is_appended(make_app, bubbles, expected):
    app = make_app()
    await add(app, "critical")
    _styled(app, *bubbles)
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    assert [call[2] for call in app.bot.of("send", OWNER)] == [expected]


def _flaky_sends(app, fail_on):
    """Make the bot's send_message raise on the given 1-based call numbers. Returns the counter of attempts."""
    send, attempts = app.bot.send_message, []

    async def flaky(chat_id, text, **kw):
        attempts.append(text)
        if len(attempts) in fail_on or fail_on == "all":
            raise OSError("network down")
        return await send(chat_id, text, **kw)

    app.bot.send_message = flaky
    return attempts


async def test_failed_critical_delivery_retries_with_backoff_and_styles_only_once(make_app):
    app = make_app()
    await add(app, "critical")
    attempts = _flaky_sends(app, {1, 2})
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    await app.dispatch.tick()
    assert len(attempts) == 1  # backing off for 1 minute
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    assert len(attempts) == 2
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    assert len(attempts) == 2  # the second failure backs off for 2 minutes
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    assert len(attempts) == 3
    assert [call[2] for call in app.bot.of("send", OWNER)] == ["hey\n⏰ Check the kettle"]
    assert len(app.script.requests) == 1
    assert await app.dispatch.reminders.active() == []


async def test_delivery_is_cancelled_with_an_error_after_ten_failed_attempts(make_app, caplog):
    app = make_app()
    await add(app)
    attempts = _flaky_sends(app, "all")
    app.dispatch.now.advance(minutes=1)
    for _ in range(12):
        await app.dispatch.tick()
        app.dispatch.now.advance(minutes=60)
    assert len(attempts) == 10
    assert await app.dispatch.reminders.active() == []
    status = app.store._conn.execute("SELECT status FROM reminders").fetchone()["status"]
    assert status == "cancelled"
    assert any(record.levelname == "ERROR" and "reminder" in record.getMessage() for record in caplog.records)


async def test_backoff_doubles_and_caps_at_an_hour(make_app):
    app = make_app()
    await add(app)
    attempts = _flaky_sends(app, "all")
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    waits = []
    for _ in range(9):
        waited, before = 0, len(attempts)
        while len(attempts) == before:
            app.dispatch.now.advance(minutes=1)
            waited += 1
            await app.dispatch.tick()
        waits.append(waited)
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60]


async def test_chunks_already_sent_are_not_resent_on_retry(make_app):
    app = make_app()
    text = "".join(chr(ord("a") + i // 2000) * 2000 for i in range(5))[:9000]
    await app.dispatch.reminders.create("2026-01-01T12:01", text, "normal", "")
    attempts = _flaky_sends(app, {3})
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    sent = [call[2] for call in app.bot.of("send", OWNER)]
    assert len(attempts) == 6 and len(sent) == 5
    assert "".join(sent) == f"⏰ {text}"
    assert await app.dispatch.reminders.active() == []


async def test_casual_reminder_survives_a_silent_or_failed_turn_until_dom_answers(make_app):
    app = make_app(contents=('{"no_response": true}',))
    await add(app, "casual")
    await online(app, OWNER)
    app.dispatch.now.advance(minutes=1)
    await deliver(app, tg(OWNER, OWNER))  # Dom stays silent
    assert len(await app.dispatch.reminders.active()) == 1

    async def broken(request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
        failing, app.client = app.client, chat(http, app)
        await deliver(app, tg(OWNER, OWNER, msg_id=2))  # the model is down
        app.client = failing
    assert len(await app.dispatch.reminders.active()) == 1

    await deliver(app, tg(OWNER, OWNER, msg_id=3))  # Dom answers
    requests = turn_requests(app)
    first, answered = requests[0], requests[-1]
    assert "reminder: Check the kettle" in first["messages"][-1]["content"]
    assert "reminder: Check the kettle" in answered["messages"][-1]["content"]
    assert await app.dispatch.reminders.active() == []


@pytest.mark.parametrize("repeat,due,next_due", [
    ("weekly", "2027-03-21T01:30", "2027-03-28T01:00:00+00:00"),  # Sunday 01:30 does not exist: 02:00 BST is the first valid time
    ("daily", "2027-03-27T01:30", "2027-03-28T01:00:00+00:00"),
    ("weekdays", "2027-03-26T01:30", "2027-03-29T00:30:00+00:00"),  # Monday is a normal day again, 01:30 BST
])
async def test_repeat_into_a_spring_forward_gap_fires_at_the_first_valid_time_that_day(make_app, repeat, due, next_due):
    app = make_app()
    app.config = app.config.model_copy(update={"timezone": "Europe/London"})
    await add(app, "critical", when=due, repeat=repeat)
    app.dispatch.now.at = datetime.fromisoformat(due).replace(tzinfo=timezone.utc)
    await app.dispatch.tick()
    (row,) = await app.dispatch.reminders.active()
    assert row["due_at"] == next_due
