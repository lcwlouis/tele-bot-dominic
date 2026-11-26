"""Assistant mode (v0.8.5 W6): the switch, the schedule, the content, the caps and owner-DM-only enforcement."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from dom import assistant as assistant_module
from dom.config import AssistantConfig
from dom.google import GoogleError
from dom.presence import parse_habits
from dom.telegram import owner as owner_commands
from test_usage import online, usage_rows
from tghelpers import FRIEND, GROUP, OWNER, turn_requests

PLAN = json.dumps({"no_response": False, "react": None, "bubbles": [{"text": "morning! here's your day"}]})
NO_RESPONSE = '{"no_response": true}'
MORNING = datetime(2026, 1, 1, 8, 5, tzinfo=timezone.utc)  # five minutes past the default 08:00


async def ready(make_app, *contents, at=MORNING, **settings):
    """An owner app with assistant mode on, Dom online, and the clock at `at`."""
    app = make_app(contents=contents)
    app.config = app.config.model_copy(update={"assistant": AssistantConfig(enabled=True, **settings)})
    app.dispatch.now.at = at
    await online(app, OWNER)
    return app


async def remind(app, text="Pay the rent", hours=2):
    return await app.dispatch.reminders.create(
        (app.dispatch.now() + timedelta(hours=hours)).isoformat(), text, "normal", "",
    )


async def last(app, kind="morning"):
    return json.loads(await app.store.kv_get(f"assistant:{kind}:last"))


async def claim(app, kind="morning", day="2026-01-01"):
    return await app.store.kv_get(f"assistant:{kind}:{day}")


def sent(app):
    return app.bot.of("send", OWNER)


def plan_request(app):
    return app.script.requests[-1]["messages"][-1]["content"]


# ---- the usage migration ----------------------------------------------------------------------------------------------


def test_the_usage_migration_keeps_every_row_and_accepts_the_new_kind(tmp_path):
    import sqlite3

    from dom.store.store import MIGRATIONS, MIGRATIONS_DIR, TARGET_VERSION, migrate_database

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        for version, name in MIGRATIONS:
            if version <= 35:
                conn.executescript((MIGRATIONS_DIR / name).read_text())
        conn.execute(
            "INSERT INTO usage (at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback, settled) "
            "VALUES ('t', 'embedding', 'm', 'h', 111, 1, 2, 0.5, 0, 1)"
        )
    assert migrate_database(db) == TARGET_VERSION >= 36
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT kind, cost_usd, settled FROM usage").fetchall() == [("embedding", 0.5, 1)]
        conn.execute(
            "INSERT INTO usage (at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback) "
            "VALUES ('t', 'assistant', 'm', 'h', 111, 1, 2, 0.1, 0)"
        )
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 2
        assert ("idx_usage_at",) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()


# ---- the switch --------------------------------------------------------------------------------------------------------


def test_assistant_mode_is_off_by_default_and_in_the_starter_config():
    from dom.config import starter_config

    assert AssistantConfig().enabled is False
    assert starter_config()["assistant"] == {"enabled": False, "morning": "08:00", "evening": "21:30"}


@pytest.mark.parametrize("value", ["8:00", "24:00", "08:60", "noon", "08:00:00"])
def test_a_bad_time_is_refused(value):
    with pytest.raises(ValueError):
        AssistantConfig(morning=value)


async def test_nothing_is_sent_while_the_mode_is_off(make_app):
    app = await ready(make_app, PLAN)
    app.config = app.config.model_copy(update={"assistant": AssistantConfig(enabled=False)})
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and app.script.requests == []
    assert await claim(app) is None


async def test_the_command_switches_the_mode_live_and_saves_it(make_app):
    app = make_app()
    (app.home.root / "config.toml").write_text('owner_id = 111\n[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "m"\n')
    assert "off" in (await owner_commands.assistant_command(app, ["status"])).splitlines()[0]
    assert "is on" in await owner_commands.assistant_command(app, ["on"])
    assert app.config.assistant.enabled is True  # no restart
    assert "enabled = true" in (app.home.root / "config.toml").read_text()
    status = await owner_commands.assistant_command(app, [])
    assert status.splitlines()[0] == "Assistant mode is on." and "Morning plan: at 08:00" in status
    assert "is off" in await owner_commands.assistant_command(app, ["off"])
    assert app.config.assistant.enabled is False
    assert "Use /assistant" in await owner_commands.assistant_command(app, ["maybe"])


async def test_the_command_and_the_switch_reach_only_the_owner_dm(make_app):
    from dom.telegram import handlers
    from tghelpers import tg

    app = make_app()
    for chat_id, sender in ((FRIEND, FRIEND), (GROUP, FRIEND)):
        await handlers.handle_message(app, tg(chat_id, sender, "/assistant on", msg_id=5))
    assert app.config.assistant.enabled is False


async def test_the_dashboard_switch_and_schedule_apply_live(make_app, browser):
    app = make_app()
    (app.home.root / "config.toml").write_text('owner_id = 111\n[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "m"\n')
    async with browser(app) as client:
        headers = {"Origin": "https://dom.test"}
        from test_web_auth import ORIGIN

        headers = {"Origin": ORIGIN}
        permissions = (await client.get("/api/permissions")).json()
        assert {item["id"]: item["enabled"] for item in permissions["items"]}["assistant"] is False
        on = await client.post("/api/permissions", headers=headers, json={
            "revision": permissions["revision"], "id": "assistant", "enabled": True,
        })
        assert on.status_code == 200 and app.config.assistant.enabled is True
        view = (await client.get("/api/assistant")).json()
        assert view["enabled"] is True and [m["time"] for m in view["messages"]] == ["08:00", "21:30"]
        saved = await client.post("/api/assistant/schedule", headers=headers, json={
            "revision": view["revision"], "morning": "07:15", "evening": "",
        })
        assert saved.status_code == 200, saved.text
        assert (app.config.assistant.morning, app.config.assistant.evening) == ("07:15", "")
        assert [m["time"] for m in saved.json()["messages"]] == ["07:15", ""]
        bad = await client.post("/api/assistant/schedule", headers=headers, json={
            "revision": saved.json()["revision"], "morning": "7pm", "evening": "",
        })
        assert bad.status_code == 422 and app.config.assistant.morning == "07:15"
        stale = await client.post("/api/assistant/schedule", headers=headers, json={
            "revision": view["revision"], "morning": "06:00", "evening": "",
        })
        assert stale.status_code == 409


# ---- the schedule ------------------------------------------------------------------------------------------------------


async def test_the_morning_plan_is_sent_once_a_day_in_the_owner_dm(make_app):
    app = await ready(make_app, PLAN)
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert [m for m in sent(app)] != [] and len(sent(app)) == 1
    assert "Pay the rent" in plan_request(app) and "morning plan" in plan_request(app)
    window = await app.store.get_recent_chat_window(OWNER)
    assert window[-1].role == "assistant" and "your day" in window[-1].text  # recorded, so the next turn sees it
    assert (await last(app))["state"] == "sent"

    for _ in range(3):  # later ticks the same day send nothing more
        app.dispatch.now.advance(minutes=30)
        await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1 and len(app.script.requests) == 1


async def test_the_plan_is_attributed_to_its_own_usage_kind(make_app):
    import httpx

    from test_usage import chat, completion

    app = await ready(make_app)
    usage = {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.25}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: completion(PLAN, usage))) as http:
        app.client = chat(http, app)
        await remind(app)
        await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1
    assert [(row[0], row[3]) for row in usage_rows(app)] == [("assistant", OWNER)]


async def test_a_restart_does_not_send_the_plan_again(make_app):
    app = await ready(make_app, PLAN, PLAN)
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    app.assistant = assistant_module.Assistant(app)  # a new process: only the store remains
    app.dispatch.now.advance(minutes=20)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1


async def test_a_failed_send_keeps_the_claim_and_does_not_retry(make_app):
    app = await ready(make_app, PLAN, PLAN)
    await remind(app)

    async def boom(*args, **kwargs):
        raise RuntimeError("telegram is down")

    app.bot.send_message = boom
    await app.assistant.tick(app.dispatch.now())
    assert (await last(app))["state"] == "failed"
    assert await claim(app) == "failed"
    app.dispatch.now.advance(minutes=10)
    await app.assistant.tick(app.dispatch.now())
    assert len(app.script.requests) == 1  # no retry loop, no second model call


async def test_a_late_start_sends_within_the_window(make_app):
    app = await ready(make_app, PLAN, at=datetime(2026, 1, 1, 10, 55, tzinfo=timezone.utc))
    await remind(app, hours=5)
    await app.assistant.tick(app.dispatch.now())  # 2h55 after 08:00: still inside the window
    assert len(sent(app)) == 1


async def test_a_start_after_the_window_skips_the_day(make_app):
    app = await ready(make_app, PLAN, at=datetime(2026, 1, 1, 11, 5, tzinfo=timezone.utc))
    await remind(app, hours=5)
    await app.assistant.tick(app.dispatch.now())  # 3h05 after 08:00: too late
    assert sent(app) == [] and app.script.requests == []
    assert (await last(app))["state"] == "late"
    app.dispatch.now.advance(hours=1)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == []


async def test_nothing_is_sent_before_the_time(make_app):
    app = await ready(make_app, PLAN, at=datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc))
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and await claim(app) is None


async def test_each_message_has_its_own_toggle_and_claim(make_app):
    app = await ready(make_app, PLAN, PLAN, morning="", at=datetime(2026, 1, 1, 21, 40, tzinfo=timezone.utc))
    await remind(app, hours=12)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1 and "wrap-up" in plan_request(app)
    assert await claim(app) is None  # empty: off, never claimed
    assert (await last(app, "evening"))["state"] == "sent"

    none = await ready(make_app, PLAN, morning="", evening="")
    await remind(none)
    await none.assistant.tick(none.dispatch.now())
    assert sent(none) == []


async def test_a_message_that_straddles_midnight_in_the_owner_timezone(make_app):
    # 23:30 Singapore is 15:30 UTC; the owner's day is the Singapore date, not the UTC one.
    app = await ready(make_app, PLAN, PLAN, morning="", evening="23:30", at=datetime(2026, 1, 1, 15, 45, tzinfo=timezone.utc))
    app.config = app.config.model_copy(update={"timezone": "Asia/Singapore"})
    await remind(app, hours=12)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1
    assert await claim(app, "evening") == "sent"

    app.dispatch.now.advance(hours=1)  # 00:45 on the 2nd in Singapore: still the first evening's day, already claimed
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1

    app.dispatch.now.advance(hours=23)  # 23:45 on the 2nd: the next evening
    await remind(app, "Water the plants", hours=6)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 2 and await claim(app, "evening", "2026-01-02") == "sent"


async def test_the_local_date_not_the_utc_date_decides_which_day_it_is(make_app):
    # 03:00 UTC on the 2nd is 19:00 on the 1st in Los Angeles: the owner's day is still the 1st, and its plan is spent.
    app = await ready(make_app, PLAN, PLAN, evening="", at=datetime(2026, 1, 1, 16, 5, tzinfo=timezone.utc))
    app.config = app.config.model_copy(update={"timezone": "America/Los_Angeles"})
    await remind(app, hours=2)
    await app.assistant.tick(app.dispatch.now())  # 08:05 on the 1st in Los Angeles
    assert len(sent(app)) == 1 and await claim(app) == "sent"
    app.dispatch.now.advance(hours=11)  # 03:05 UTC on the 2nd, 19:05 on the 1st in Los Angeles
    await online(app, OWNER)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1
    app.dispatch.now.advance(hours=13)  # 08:05 on the 2nd in Los Angeles
    await online(app, OWNER)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 2 and await claim(app, "morning", "2026-01-02") == "sent"


async def test_a_spring_forward_gap_still_has_one_send(make_app):
    # 02:30 does not exist in New York on 2026-03-08; the plan still goes out once, shortly after.
    at = datetime(2026, 3, 8, 7, 45, tzinfo=timezone.utc)  # 03:45 EDT
    app = await ready(make_app, PLAN, morning="02:30", evening="", at=at)
    app.config = app.config.model_copy(update={"timezone": "America/New_York"})
    await remind(app, hours=4)
    await app.assistant.tick(app.dispatch.now())
    app.dispatch.now.advance(minutes=5)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1


# ---- presence ----------------------------------------------------------------------------------------------------------


async def test_a_plan_waits_while_dom_is_asleep_and_goes_when_he_is_up(make_app):
    app = await ready(make_app, PLAN)
    await remind(app)
    await app.dispatch.presence.sleep(OWNER, parse_habits(""))
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and (await last(app))["state"] == "asleep"
    assert await claim(app) is None  # waiting is not a claim

    app.dispatch.now.advance(minutes=30)
    await app.dispatch.presence.go_online(OWNER, parse_habits(""))
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1 and (await last(app))["state"] == "sent"


async def test_a_plan_that_waits_past_its_window_is_skipped(make_app):
    app = await ready(make_app, PLAN)
    await remind(app, hours=20)
    await app.dispatch.presence.sleep(OWNER, parse_habits(""))
    app.dispatch.presence.sleep_until = None
    await app.assistant.tick(app.dispatch.now())
    app.dispatch.now.advance(hours=3)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and (await last(app))["state"] in ("late", "asleep")


async def test_an_offline_dom_comes_online_to_send_the_plan(make_app):
    app = await ready(make_app, PLAN)
    await app.dispatch.presence.go_offline(OWNER, parse_habits(""))
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1
    state, _ = app.dispatch.presence.peek(await app.store.get_presence(OWNER), parse_habits(""))
    assert state == "online"


# ---- caps --------------------------------------------------------------------------------------------------------------


async def test_a_capped_plan_records_why_and_never_retries(make_app):
    app = await ready(make_app, PLAN, PLAN)
    app.config = app.config.model_copy(update={"monthly_spend_cap_usd": 0.01})
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1.0})
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert (await last(app))["state"] == "blocked"
    assert await claim(app) == "blocked"
    app.dispatch.now.advance(minutes=30)
    await app.assistant.tick(app.dispatch.now())
    assert app.script.requests == []  # the one thing sent is the cap's own notice, never a plan
    assert not any("your day" in str(call) for call in sent(app))


async def test_an_area_limit_blocks_the_plan_too(make_app):
    app = await ready(make_app, PLAN)
    app.config = app.config.model_copy(update={"area_spend_caps_usd": {"chat": 0.01}})
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": 1.0})
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert (await last(app))["state"] == "blocked" and app.script.requests == []


async def test_the_daily_cap_does_not_stop_the_owners_plan(make_app):
    app = await ready(make_app, PLAN)
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.0001})
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1


async def test_an_unapproved_owner_chat_blocks_the_plan_once(make_app):
    app = await ready(make_app, PLAN)
    await app.store.set_chat_auth_status(OWNER, "blocked")
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and (await last(app))["state"] == "blocked"
    app.dispatch.now.advance(minutes=30)
    await app.assistant.tick(app.dispatch.now())
    assert app.script.requests == []


# ---- content -----------------------------------------------------------------------------------------------------------


async def test_an_empty_day_sends_nothing_and_calls_no_model(make_app):
    app = await ready(make_app, PLAN)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and app.script.requests == []
    assert (await last(app))["state"] == "nothing"
    assert await claim(app) == "nothing"  # a day with nothing to say is spent too


async def test_a_model_that_has_nothing_to_say_sends_nothing(make_app):
    app = await ready(make_app, NO_RESPONSE)
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and (await last(app))["state"] == "nothing"


async def test_the_calendar_is_left_out_when_google_is_not_connected(make_app, monkeypatch):
    app = await ready(make_app, PLAN)
    await remind(app)

    async def agenda(google, day=None):
        raise AssertionError("the calendar must not be read")

    monkeypatch.setattr("dom.google_tools.agenda_text", agenda)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1 and "calendar" not in plan_request(app).split("<assistant_plan")[1]


async def test_the_calendar_is_included_only_while_calendar_read_is_on(make_app, monkeypatch):
    async def agenda(google, day=None):
        return "today 09:00-10:00 Dentist"

    monkeypatch.setattr("dom.google_tools.agenda_text", agenda)
    app = await ready(make_app, PLAN)
    available = {"calendar_read": True}

    async def switch(name):
        return available.get(name, False)

    monkeypatch.setattr(app.google, "available", switch)
    await app.assistant.tick(app.dispatch.now())  # no reminders, but a calendar entry is worth a message
    assert "Dentist" in plan_request(app)

    off = await ready(make_app, PLAN)
    available_off = {"calendar_read": False}

    async def switched_off(name):
        return available_off.get(name, False)

    monkeypatch.setattr(off.google, "available", switched_off)
    await off.assistant.tick(off.dispatch.now())
    assert sent(off) == [] and off.script.requests == []  # Calendar read off and nothing else: nothing to say


async def test_a_calendar_that_fails_is_left_out_not_fatal(make_app, monkeypatch):
    async def agenda(google, day=None):
        raise GoogleError("down")

    monkeypatch.setattr("dom.google_tools.agenda_text", agenda)
    app = await ready(make_app, PLAN)

    async def always(name):
        return True

    monkeypatch.setattr(app.google, "available", always)
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1 and "Pay the rent" in plan_request(app)


async def test_the_morning_plan_holds_reminders_goals_weather_and_holiday(make_app, monkeypatch):
    app = await ready(make_app, PLAN)
    await remind(app, "Call the dentist")
    await remind(app, "Next week's thing", hours=24 * 8)
    goal = await app.goals.create("Learn the guitar", target_date="2026-12-31")
    await app.goals.add_progress(goal["id"], "Practised scales")
    monkeypatch.setattr(app.world, "line", lambda: "Light rain, 24C.")
    monkeypatch.setattr(app.world, "holiday_line", lambda: "Today is New Year's Day.")
    await app.assistant.tick(app.dispatch.now())
    note = plan_request(app)
    for expected in ("Call the dentist", "Learn the guitar", "Practised scales", "Light rain", "New Year"):
        assert expected in note
    assert "Next week's thing" not in note


async def test_the_evening_wrap_up_holds_the_day_and_tomorrow(make_app):
    app = await ready(make_app, PLAN, morning="", at=datetime(2026, 1, 1, 21, 35, tzinfo=timezone.utc))
    for text, hours in (("Missed errand", -3), ("Tomorrow call", 12)):
        due = app.dispatch.now() + timedelta(hours=abs(hours)) if hours > 0 else app.dispatch.now() + timedelta(hours=1)
        row = await app.dispatch.reminders.create(due.isoformat(), text, "normal", "")
        if hours < 0:  # make it overdue
            await app.store._run(
                app.store._conn.execute, "UPDATE reminders SET due_at = ? WHERE id = ?",
                ((app.dispatch.now() - timedelta(hours=3)).isoformat(), row),
            )
    done = await app.dispatch.reminders.create((app.dispatch.now() + timedelta(minutes=5)).isoformat(), "Took the pill", "normal", "")
    await app.store.finish_reminder(done, (app.dispatch.now() - timedelta(hours=4)).isoformat(), None)
    goal = await app.goals.create("Run a 10k")
    await app.goals.add_progress(goal["id"], "Ran 5k today")
    await app.assistant.tick(app.dispatch.now())
    note = plan_request(app)
    assert "wrap-up" in note and "Took the pill" in note and "Missed errand" in note and "Tomorrow call" in note
    assert "Run a 10k" in note and "Ran 5k today" in note


# ---- owner-DM-only enforcement -----------------------------------------------------------------------------------------


async def test_group_and_friend_turn_notes_get_nothing(make_app):
    app = await ready(make_app)
    await remind(app, "Secret errand", hours=1)
    stale = await app.goals.create("A private goal")
    await app.store._run(
        app.store._conn.execute, "UPDATE goals SET updated_at = ? WHERE id = ?",
        ((app.dispatch.now() - timedelta(days=30)).isoformat(), stale["id"]),
    )
    assert "Secret errand" in await app.assistant.turn_note("owner_dm", OWNER)
    for scope, chat_id in (("group", GROUP), ("friend_dm", FRIEND), ("owner_dm", FRIEND), ("owner_dm", GROUP), ("group", OWNER)):
        assert await app.assistant.turn_note(scope, chat_id) == ""
    assert json.loads(await app.store.kv_get("assistant:offered"))["items"]  # only the owner's call was tracked


async def test_a_friend_or_group_prompt_never_carries_assistant_content(make_app):
    from dom.telegram import handlers
    from tghelpers import FINAL, tg

    app = await ready(make_app, FINAL, FINAL)
    await remind(app, "Secret errand", hours=1)
    for chat_id, sender in ((FRIEND, FRIEND), (GROUP, FRIEND)):
        await app.store.set_chat_auth_status(chat_id, "approved")
        await app.dispatch.presence.go_online(chat_id, parse_habits(""))
        await handlers.handle_message(app, tg(chat_id, sender, "hi dom", msg_id=40 + abs(chat_id) % 7))
    from tghelpers import drain_dispatch

    await drain_dispatch(app.dispatch)
    for request in app.script.requests:
        assert "assistant_followup" not in json.dumps(request) and "Secret errand" not in json.dumps(request)


async def test_the_plan_is_composed_and_sent_only_for_the_owner(make_app):
    app = await ready(make_app, PLAN)
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert app.bot.of("send", FRIEND) == [] and app.bot.of("send", GROUP) == []
    assert all(request["messages"][-1]["content"] for request in app.script.requests)
    assert not any("dm" in str(r).lower() and str(FRIEND) in str(r) for r in app.script.requests)


async def test_a_missing_owner_means_no_plan(make_app):
    app = make_app(owner_id=None)
    app.config = app.config.model_copy(update={"assistant": AssistantConfig(enabled=True)})
    await app.assistant.tick(app.dispatch.now())
    assert app.script.requests == []


# ---- follow-through ----------------------------------------------------------------------------------------------------


async def test_the_follow_through_block_lists_due_reminders_and_stale_goals(make_app):
    app = await ready(make_app)
    soon = await remind(app, "Soon thing", hours=5)
    await remind(app, "Far thing", hours=40)
    stale = await app.goals.create("Stale goal")
    await app.goals.create("Fresh goal")
    await app.store._run(
        app.store._conn.execute, "UPDATE goals SET updated_at = ? WHERE id = ?",
        ((app.dispatch.now() - timedelta(days=9)).isoformat(), stale["id"]),
    )
    block = await app.assistant.turn_note("owner_dm", OWNER)
    assert f"#{soon}" in block and "Soon thing" in block and "Stale goal" in block and "9 days" in block
    assert "Far thing" not in block and "Fresh goal" not in block
    assert "never a nag" in block and "twice in a day" in block


async def test_an_item_is_marked_only_once_a_delivered_reply_raises_it(make_app):
    app = await ready(make_app)
    await remind(app, "Soon thing", hours=5)
    assert "Soon thing" in await app.assistant.turn_note("owner_dm", OWNER)
    # The model was shown it but the turn failed, or the reply never mentioned it: it is still on offer.
    assert "Soon thing" in await app.assistant.turn_note("owner_dm", OWNER)
    await app.assistant.delivered(OWNER, "owner_dm", ["how's the garden doing?"])
    assert "Soon thing" in await app.assistant.turn_note("owner_dm", OWNER)
    await app.assistant.delivered(OWNER, "owner_dm", ["hey, don't forget the SOON THING later"])  # any case
    assert await app.assistant.turn_note("owner_dm", OWNER) == ""  # raised: not again today
    await remind(app, "Another thing", hours=6)
    again = await app.assistant.turn_note("owner_dm", OWNER)
    assert "Another thing" in again and "Soon thing" not in again

    app.dispatch.now.advance(days=1)  # tomorrow it may be raised again if it is still open
    await remind(app, "Soon thing", hours=5)
    assert "Soon thing" in await app.assistant.turn_note("owner_dm", OWNER)


async def test_a_reply_in_another_chat_never_marks_anything(make_app):
    app = await ready(make_app)
    await remind(app, "Soon thing", hours=5)
    await app.assistant.turn_note("owner_dm", OWNER)
    await app.assistant.delivered(FRIEND, "friend_dm", ["soon thing"])
    await app.assistant.delivered(GROUP, "group", ["soon thing"])
    assert await app.store.kv_get("assistant:nudged") is None


async def test_a_real_owner_turn_marks_what_its_delivered_reply_raised(make_app):
    from dom.telegram import handlers
    from tghelpers import drain_dispatch, tg

    raised = json.dumps({"no_response": False, "react": None, "bubbles": [{"text": "psst, soon thing is due later"}]})
    app = await ready(make_app, raised)
    await remind(app, "Soon thing", hours=5)
    await handlers.handle_message(app, tg(OWNER, OWNER, "hello", msg_id=78))
    await drain_dispatch(app.dispatch)
    assert json.loads(await app.store.kv_get("assistant:nudged"))["items"]
    assert await app.assistant.turn_note("owner_dm", OWNER) == ""


async def test_the_follow_through_block_is_off_with_the_mode(make_app):
    app = await ready(make_app)
    await remind(app, "Soon thing", hours=5)
    app.config = app.config.model_copy(update={"assistant": AssistantConfig(enabled=False)})
    assert await app.assistant.turn_note("owner_dm", OWNER) == ""
    assert await app.store.kv_get("assistant:offered") is None


async def test_the_block_reaches_the_owner_dm_turn_after_the_window(make_app):
    from dom.telegram import handlers
    from tghelpers import FINAL, drain_dispatch, tg

    app = await ready(make_app, FINAL)
    await remind(app, "Soon thing", hours=5)
    await handlers.handle_message(app, tg(OWNER, OWNER, "hello", msg_id=77))
    await drain_dispatch(app.dispatch)
    prompt = json.dumps(turn_requests(app)[0])
    assert "assistant_followup" in prompt
    messages = turn_requests(app)[0]["messages"]
    assert "assistant_followup" not in messages[0]["content"]  # never in the cached system prefix


async def test_the_status_view_says_why_a_message_was_not_sent(make_app):
    app = await ready(make_app, PLAN)
    await app.assistant.tick(app.dispatch.now())
    view = await app.assistant.view()
    assert view["enabled"] is True
    morning = view["messages"][0]
    assert morning["last"]["state"] == "nothing" and morning["last"]["label"] == assistant_module.LABELS["nothing"]
    assert view["messages"][1]["last"] is None


# ---- review fixes ------------------------------------------------------------------------------------------------------


async def test_two_ticks_at_once_send_one_message(make_app):
    import asyncio

    app = await ready(make_app, PLAN, PLAN, PLAN)
    await remind(app)
    await asyncio.gather(*(app.assistant.tick(app.dispatch.now()) for _ in range(4)))
    assert len(sent(app)) == 1 and len(app.script.requests) == 1
    assert await claim(app) == "sent"


async def test_the_claim_is_atomic_in_the_store(make_app):
    app = make_app()
    assert await app.store.kv_claim("k", "a") is True
    assert await app.store.kv_claim("k", "b") is False and await app.store.kv_get("k") == "a"
    assert await app.store.kv_swap("k", "x", "c") is False and await app.store.kv_swap("k", "a", "c") is True
    assert await app.store.kv_get("k") == "c"


async def test_a_clock_moved_back_never_sends_a_day_twice(make_app):
    app = await ready(make_app, PLAN, PLAN, PLAN, PLAN)
    await remind(app, hours=2)
    await app.assistant.tick(app.dispatch.now())
    app.dispatch.now.advance(days=1)
    await online(app, OWNER)
    await remind(app, "Second day", hours=2)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 2
    app.dispatch.now.advance(days=-1)  # the clock steps back to the first day, then forward again
    await online(app, OWNER)
    await app.assistant.tick(app.dispatch.now())
    app.dispatch.now.advance(days=1)
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 2 and len(app.script.requests) == 2
    assert await claim(app) == "sent" and await claim(app, "morning", "2026-01-02") == "sent"


async def test_claims_older_than_a_week_are_pruned(make_app):
    app = await ready(make_app, PLAN)
    for day in ("2025-12-01", "2025-12-24", "2025-12-26"):
        await app.store.kv_set(f"assistant:morning:{day}", "sent")
    await app.store.kv_set("assistant:evening:2025-12-01", "sent")
    await remind(app)
    await app.assistant.tick(app.dispatch.now())
    assert await claim(app, "morning", "2025-12-01") is None and await claim(app, "morning", "2025-12-24") is None
    assert await claim(app, "morning", "2025-12-26") == "sent"  # inside the week
    assert await claim(app, "evening", "2025-12-01") == "sent"  # the other message prunes on its own tick
    assert await app.store.kv_get("assistant:morning:last") is not None  # the status key is not a claim


def sleeps_at_noon(app):
    """Dom's sleep window moved to midday, so a test in the small hours finds him awake."""
    habits = app.home.root / "memory" / "HABITS.md"
    habits.parent.mkdir(exist_ok=True)
    habits.write_text("sleep: 12:00-13:00\n")


async def london(make_app, *, at, morning, hours=3):
    app = await ready(make_app, PLAN, morning=morning, evening="", at=at)
    app.config = app.config.model_copy(update={"timezone": "Europe/London"})
    await remind(app, hours=hours)
    sleeps_at_noon(app)
    await online(app, OWNER)
    return app


async def test_a_spring_forward_morning_counts_its_window_in_real_time(make_app):
    # 01:30 does not exist in London on 2026-03-29 (the clocks go 01:00 -> 02:00). It counts from 01:30 GMT = 02:30 BST,
    # so 04:25 UTC (05:25 BST) is 2h55 in. By wall-clock subtraction it would be 3h55 and wrongly skipped.
    app = await london(make_app, at=datetime(2026, 3, 29, 4, 25, tzinfo=timezone.utc), morning="01:30")
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1

    late = await london(make_app, at=datetime(2026, 3, 29, 4, 35, tzinfo=timezone.utc), morning="01:30")
    late.dispatch.now.at = datetime(2026, 3, 29, 4, 35, tzinfo=timezone.utc)
    await late.store.kv_delete("assistant:morning:2026-03-29")
    before = len(sent(late))
    await late.assistant.tick(late.dispatch.now())
    assert len(sent(late)) == before and (await last(late))["state"] == "late"


async def test_a_fall_back_morning_uses_the_first_01_30_and_sends_once(make_app):
    # 01:30 happens twice in London on 2026-10-25. The first (BST) is 00:30 UTC, so the window ends at 03:30 UTC.
    app = await london(make_app, at=datetime(2026, 10, 25, 3, 25, tzinfo=timezone.utc), morning="01:30")
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1
    app.dispatch.now.at = datetime(2026, 10, 25, 1, 31, tzinfo=timezone.utc)  # the second 01:30, on the wall
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1  # one claim per local date, whichever 01:30 it is

    late = await london(make_app, at=datetime(2026, 10, 25, 3, 35, tzinfo=timezone.utc), morning="01:30")
    await late.store.kv_delete("assistant:morning:2026-10-25")
    before = len(sent(late))
    await late.assistant.tick(late.dispatch.now())  # 3h05 after the first 01:30; by the wall it would be only 2h05
    assert len(sent(late)) == before and (await last(late))["state"] == "late"


async def test_a_late_wrap_up_covers_the_day_it_was_claimed_for(make_app, monkeypatch):
    # The 23:30 wrap-up of the 1st goes out at 00:20 on the 2nd and must talk about the 1st, with the 2nd as tomorrow.
    app = await ready(make_app, PLAN, morning="", evening="23:30", at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    went_off = await remind(app, "Went off on the first", hours=1)
    await app.store.finish_reminder(went_off, (app.dispatch.now() + timedelta(hours=1)).isoformat(), None)
    await remind(app, "Due on the second", hours=14)  # 02:00 on the 2nd
    await remind(app, "Due on the third", hours=40)
    seen = []

    async def agenda(google, day=None):
        seen.append(day)
        return "meeting"

    async def available(name):
        return True

    monkeypatch.setattr("dom.google_tools.agenda_text", agenda)
    monkeypatch.setattr(app.google, "available", available)
    app.dispatch.now.at = datetime(2026, 1, 2, 0, 20, tzinfo=timezone.utc)
    sleeps_at_noon(app)
    await online(app, OWNER)
    await app.assistant.tick(app.dispatch.now())
    note = plan_request(app)
    assert len(sent(app)) == 1 and await claim(app, "evening", "2026-01-01") == "sent"
    assert "Went off on the first" in note and "Due on the second" in note and "Due on the third" not in note
    assert seen[0] == datetime(2026, 1, 1).date() and "Thursday 01 January" in note  # the turn note adds its own, undated, read


async def asleep_during_generation(app):
    """Dom falls asleep after the plan was composed and before it is delivered."""
    compose = app.dispatch._compose_turn

    async def compose_then_sleep(*args, **kwargs):
        actions = await compose(*args, **kwargs)
        await app.dispatch.presence.sleep(OWNER, parse_habits(""))
        return actions

    app.dispatch._compose_turn = compose_then_sleep


async def test_falling_asleep_mid_generation_defers_and_a_later_tick_sends_once(make_app):
    app = await ready(make_app, PLAN, PLAN, PLAN)
    await remind(app)
    await asleep_during_generation(app)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and await claim(app) == "deferred" and (await last(app))["state"] == "asleep"

    app.dispatch._compose_turn = type(app.dispatch)._compose_turn.__get__(app.dispatch)
    app.dispatch.now.advance(minutes=30)
    await app.dispatch.presence.go_online(OWNER, parse_habits(""))
    await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1 and await claim(app) == "sent"
    for _ in range(2):  # and never again
        app.dispatch.now.advance(minutes=10)
        await app.assistant.tick(app.dispatch.now())
    assert len(sent(app)) == 1


async def test_a_deferred_claim_is_taken_by_one_tick_only(make_app):
    import asyncio

    app = await ready(make_app, PLAN, PLAN)
    await remind(app)
    await app.store.kv_set("assistant:morning:2026-01-01", "deferred")
    await asyncio.gather(*(app.assistant.tick(app.dispatch.now()) for _ in range(3)))
    assert len(sent(app)) == 1


async def test_turning_the_mode_off_mid_call_drops_the_message(make_app):
    app = await ready(make_app, PLAN)
    await remind(app)
    compose = app.dispatch._compose_turn

    async def compose_then_switch_off(*args, **kwargs):
        actions = await compose(*args, **kwargs)
        app.config = app.config.model_copy(update={"assistant": AssistantConfig(enabled=False)})
        return actions

    app.dispatch._compose_turn = compose_then_switch_off
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == [] and await claim(app) == "off"
    assert (await last(app))["state"] == "off" and "turned off" in (await last(app)).get("label", "turned off")
    view = await app.assistant.view()
    assert view["messages"][0]["last"]["label"] == assistant_module.LABELS["off"]
    app.config = app.config.model_copy(update={"assistant": AssistantConfig(enabled=True)})
    app.dispatch.now.advance(minutes=10)
    await app.assistant.tick(app.dispatch.now())
    assert sent(app) == []  # the day is spent: switching back on does not send a stale plan


async def test_the_plan_is_one_tool_free_completion_and_no_tool_can_run(make_app):
    import httpx

    app = await ready(make_app)
    await remind(app, "Pay the rent")
    before = len(await app.dispatch.reminders.active())
    requests = []

    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:  # a model that tries to call a tool anyway
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function", "function": {"name": "remind", "arguments": json.dumps({
                    "when": "2026-01-02T09:00:00+00:00", "text": "injected",
                })},
            }]}
        else:
            message = {"role": "assistant", "content": PLAN}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    from dom.config import ModelEndpoint
    from dom.llm import ChatClient
    from tghelpers import no_sleep

    endpoint_config = ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env=None, input=("text",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        app.client = ChatClient(endpoint_config, None, http, sleep=no_sleep)
        await app.assistant.tick(app.dispatch.now())
    assert requests and all(not body.get("tools") for body in requests)  # no tool was ever offered
    assert len(await app.dispatch.reminders.active()) == before  # and the injected call created nothing
    assert len(sent(app)) == 1


async def test_an_owner_change_before_delivery_drops_the_message(make_app):
    app = await ready(make_app, PLAN)
    await remind(app)
    compose = app.dispatch._compose_turn

    async def compose_then_lose_the_owner(*args, **kwargs):
        actions = await compose(*args, **kwargs)
        app.store.owner_id = FRIEND
        return actions

    app.dispatch._compose_turn = compose_then_lose_the_owner
    await app.assistant.tick(app.dispatch.now())
    assert app.bot.of("send", OWNER) == [] and app.bot.of("send", FRIEND) == []
