"""Task mode: in the owner's DM only, Dom wears the assistant preset for an important task."""

import json
from datetime import timedelta

from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg, turn_requests

from dom.presence import parse_habits
from dom.telegram import handlers
from dom.tools import SELF_INSPECT_TOOL, SWITCH_VIBE_TOOL, TASK_MODE_TOOL, TurnCtx, enabled_tools
from dom.traits import (
    AUTO_PICKS_KEY, TASK_QUIET, end_task_mode, for_chat, start_task_mode, task_mode, vibe,
)
from test_vibe import auto, ctx, install, pick
from test_web_pages import browser  # noqa: F401  (fixture)
from test_owner_controls import approve


async def go(app, reason="plan the move"):
    return json.loads(await TASK_MODE_TOOL.handler({"action": "start", "reason": reason}, ctx(app)))


async def test_the_tool_is_for_the_owner_dm_alone(make_app):
    app = make_app()
    install(app)
    for scope in ("friend_dm", "group"):
        assert "task_mode" not in [tool.name for tool in await enabled_tools(app, scope)]
    assert "task_mode" in [tool.name for tool in await enabled_tools(app, "owner_dm")]
    # Not even a hand-built call works from another chat or from someone else in the owner's DM.
    for chat, scope in ((FRIEND, "friend_dm"), (GROUP, "group")):
        assert (await TASK_MODE_TOOL.handler({"action": "start", "reason": "x"}, ctx(app, chat, scope))).startswith("error:")
    stranger = TurnCtx("owner_dm", OWNER, FRIEND, app.home, app.store, OWNER, app=app)
    assert (await TASK_MODE_TOOL.handler({"action": "start", "reason": "x"}, stranger)).startswith("error:")
    assert await task_mode(app.store, OWNER, app.dispatch.now()) is None


async def test_stored_state_never_changes_another_chat(make_app):
    app = make_app()
    install(app)
    await approve(app, GROUP)
    await approve(app, FRIEND)
    await pick_vibe(app)
    assert await go(app) == {"task_mode": "on", "applies": "from your next reply", "ends_after_quiet_minutes": 30}
    now = app.dispatch.now()
    assert (await task_mode(app.store, OWNER, now)).reason == "plan the move"
    for chat in (GROUP, FRIEND):
        assert await task_mode(app.store, chat, now) is None
        _, preset = await for_chat(app.home, app.store, chat, now)
        assert preset.name == "classic"
    _, preset = await for_chat(app.home, app.store, OWNER, now)
    assert preset.name == "assistant"


async def pick_vibe(app, name="classic"):
    await auto(app)
    await pick(app, name)


async def test_a_pinned_preset_on_the_owner_dm_wins(make_app):
    app = make_app()
    install(app)
    await approve(app, OWNER)
    await pick_vibe(app)
    await app.store.set_chat_preset(OWNER, "fboy")
    assert "own preset" in await TASK_MODE_TOOL.handler({"action": "start", "reason": "x"}, ctx(app))
    # A pin made while task mode is on takes over from then on, sliders included.
    await app.store.set_chat_preset(OWNER, "")
    await go(app)
    assert (await for_chat(app.home, app.store, OWNER, app.dispatch.now()))[1].name == "assistant"
    await app.store.set_chat_preset(OWNER, "fboy")
    sliders, preset = await for_chat(app.home, app.store, OWNER, app.dispatch.now())
    assert preset.name == "fboy"
    app.home.write_preset("fboy", "---\ntitle: Fboy\n---\nSmooth.")
    app.home.write_preset("assistant", "---\ntitle: Assistant\ntraits:\n  humor: 0.05\n---\nBe efficient.")
    await app.store.set_chat_preset(OWNER, "")
    assert (await for_chat(app.home, app.store, OWNER, app.dispatch.now()))[0]["humor"] == 0.05
    await app.store.set_chat_preset(OWNER, "fboy")
    assert (await for_chat(app.home, app.store, OWNER, app.dispatch.now()))[0].get("humor") is None  # nothing of the assistant preset is left under the pin


async def test_it_ends_when_dom_says_so_or_after_a_quiet_period(make_app):
    app = make_app()
    install(app)
    await pick_vibe(app)
    now = app.dispatch.now()
    assert (await TASK_MODE_TOOL.handler({"action": "end"}, ctx(app))).startswith("error: task mode is not on")
    await go(app)
    assert (await for_chat(app.home, app.store, OWNER, now + timedelta(minutes=29)))[1].name == "assistant"
    assert (await for_chat(app.home, app.store, OWNER, now + TASK_QUIET + timedelta(seconds=1)))[1].name == "classic"
    await go(app)
    assert json.loads(await TASK_MODE_TOOL.handler({"action": "end"}, ctx(app)))["task_mode"] == "off"
    assert (await for_chat(app.home, app.store, OWNER, now))[1].name == "classic"
    events = [m.text for m in await app.store.get_recent_chat_window(OWNER) if m.role == "event"]
    assert [e for e in events if "assistant mode" in e] == [
        "Dom went into assistant mode for a task: plan the move", "Dom left assistant mode: the task is done",
    ]
    assert not await end_task_mode(app.store, OWNER, now)


async def test_asking_again_keeps_the_start_and_pushes_the_end_back(make_app):
    app = make_app()
    install(app)
    first = await start_task_mode(app.home, app.store, OWNER, "plan", app.dispatch.now())
    app.dispatch.now.advance(minutes=20)
    again = await start_task_mode(app.home, app.store, OWNER, "plan more", app.dispatch.now())
    assert again.since == first.since and again.until == first.until + timedelta(minutes=20) and again.reason == "plan more"


async def test_it_does_not_touch_switch_vibe_or_the_daily_pick(make_app):
    app = make_app()
    install(app)
    await approve(app, FRIEND)
    await approve(app, OWNER)
    await pick_vibe(app)
    picks = await app.store.kv_get(AUTO_PICKS_KEY)
    await go(app)
    today = await vibe(app.home, app.store, app.dispatch.now())
    assert (today.preset.name, today.source) == ("classic", "reflection")  # still the day's pick
    assert await app.store.kv_get(AUTO_PICKS_KEY) == picks
    friend = ctx(app, FRIEND, "friend_dm")
    assert json.loads(await SWITCH_VIBE_TOOL.handler({"preset": "fboy", "reason": "she flirted"}, friend))["preset"] == "fboy"
    assert "already switched" in await SWITCH_VIBE_TOOL.handler({"preset": "classic", "reason": "again"}, friend)
    await TASK_MODE_TOOL.handler({"action": "end"}, ctx(app))
    assert (await for_chat(app.home, app.store, OWNER, app.dispatch.now()))[1].name == "fboy"  # back to the day's vibe, as switched
    # Task mode used none of the allowance: with a fresh day's pick it is still one switch.
    await pick(app, "classic", "new day", since=app.dispatch.now() + timedelta(hours=8))
    app.dispatch.now.advance(hours=9)
    await go(app)
    assert json.loads(await SWITCH_VIBE_TOOL.handler({"preset": "fboy", "reason": "again"}, friend))["preset"] == "fboy"


async def test_the_model_sees_the_assistant_preset_and_a_task_note_then_the_quiet_period_restarts(make_app):
    app = make_app(contents=(
        json.dumps({"no_response": False, "bubbles": [{"text": "on it"}]}),
    ) * 2)
    install(app)
    await pick_vibe(app)
    await approve(app, OWNER)
    await go(app, "plan <the> move")
    app.dispatch.now.advance(minutes=20)
    await app.dispatch.presence.wake(OWNER, parse_habits(""))
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, msg_id=1, text="ok, step one?"))
    await drain_dispatch(app.dispatch)
    [request] = turn_requests(app)
    text = request["messages"][-1]["content"]
    assert '<preset name="assistant">Be efficient.</preset>' in text and "Usual voice." not in text
    assert "task mode: on since" in text and "plan &lt;the&gt; move" in text
    task = await task_mode(app.store, OWNER, app.dispatch.now())
    assert task.until == app.dispatch.now() + TASK_QUIET  # the owner talked, so the 30 quiet minutes start over
    assert "task_mode" in str(request["tools"])


async def test_self_inspect_and_the_dashboard_show_task_mode(make_app, browser):  # noqa: F811
    app = make_app()
    install(app)
    await approve(app, OWNER)
    await approve(app, GROUP, "Book Club")
    await pick_vibe(app)
    assert json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))["preset"]["task_mode"] == {"on": False}
    async with browser(app) as client:
        assert "Task mode" not in (await client.get("/chats")).text
        await go(app, "plan <b>the</b> move")
        text = (await client.get("/chats")).text
        assert 'id="task-mode"' in text and "plan &lt;b&gt;the&lt;/b&gt; move" in text and text.count("Task mode:") == 1
        assert "if the chat goes quiet" in text and "Assistant, " in text
    found = json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))["preset"]["task_mode"]
    assert found["on"] and found["reason"] == "plan <b>the</b> move"


async def test_switch_vibe_is_refused_in_the_chat_that_is_in_task_mode(make_app):
    app = make_app()
    install(app)
    await approve(app, OWNER)
    await pick_vibe(app)
    await go(app)
    refusal = await SWITCH_VIBE_TOOL.handler({"preset": "fboy", "reason": "bored"}, ctx(app))
    assert "task mode" in refusal  # refused, so the day's one switch is not spent
    assert (await vibe(app.home, app.store, app.dispatch.now())).source == "reflection"
