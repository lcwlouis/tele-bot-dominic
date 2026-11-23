import json
import random
from datetime import timedelta

from dom import reflection
from dom.prompt import build_messages
from dom.telegram import handlers, owner
from dom.tools import SELF_INSPECT_TOOL, SWITCH_VIBE_TOOL, TurnCtx, enabled_tools
from dom.traits import (
    AUTO_PICKS_KEY, ensure_pick, for_chat, load_preset, random_choice, record_pick, vibe, vibe_choices,
)
from test_owner_controls import approve, callback
from test_traits import put
from test_web_auth import POST_HEADERS
from tghelpers import FRIEND, GROUP, OWNER, tg

PUBLIC = json.dumps({"entry": "a slow day.", "mood": "playful", "nudges": {}})


def install(app, *names):
    put(app.home, "presets/classic.md", "---\ntitle: Classic\ndescription: The usual.\n---\nUsual voice.")
    put(app.home, "presets/fboy.md", "---\ntitle: Fboy\ndescription: A joke.\n---\nSmooth joke voice.")
    put(app.home, "presets/assistant.md", "---\ntitle: Assistant\n---\nBe efficient.")


async def auto(app):
    await app.store.kv_set("preset:global", "auto")


def ctx(app, chat=OWNER, scope="owner_dm"):
    return TurnCtx(scope, chat, chat, app.home, app.store, OWNER, app=app)


async def pick(app, name, reason="because", source="reflection", **kw):
    await record_pick(app.store, load_preset(app.home, name), reason, source, now=app.dispatch.now(), **kw)


def test_choices_exclude_the_assistant_and_the_fallback_favours_the_base_preset(make_app):
    app = make_app()
    install(app)
    choices = vibe_choices(app.home)
    assert [preset.name for preset in choices] == ["classic", "fboy"]
    rng = random.Random(1)
    draws = [random_choice(choices, rng).name for _ in range(700)]
    assert set(draws) == {"classic", "fboy"} and draws.count("classic") > 4 * draws.count("fboy") > 0
    assert random_choice([]) is None


async def test_a_pick_takes_effect_at_its_time_and_older_picks_are_dropped(make_app):
    app = make_app()
    install(app)
    await auto(app)
    now = app.dispatch.now()
    await pick(app, "classic")
    await pick(app, "fboy", "up late", since=now + timedelta(hours=10))
    assert (await vibe(app.home, app.store, now)).preset.name == "classic"
    later = await vibe(app.home, app.store, now + timedelta(hours=10))
    assert (later.mode, later.preset.name, later.reason, later.source) == ("auto", "fboy", "up late", "reflection")
    await pick(app, "classic", "x", since=now + timedelta(hours=20))
    assert len(json.loads(await app.store.kv_get(AUTO_PICKS_KEY))) == 3
    await pick(app, "classic", "y", source="switch")
    assert [item["reason"] for item in json.loads(await app.store.kv_get(AUTO_PICKS_KEY))] == ["y", "up late", "x"]


async def test_reflection_asks_for_a_preset_without_the_assistant_and_it_starts_at_wake_up(make_app):
    app = make_app(contents=(PUBLIC, json.dumps({"preset": "fboy", "reason": "  feeling   cheeky "})))
    install(app)
    await auto(app)
    app.dispatch.now.at = app.dispatch.now.at.replace(hour=22, minute=58)
    await reflection.run(app)
    request = app.script.requests[1]["messages"]
    assert "classic" in request[1]["content"] and "fboy" in request[1]["content"]
    assert "assistant" not in request[1]["content"] and "playful" in request[1]["content"]
    now = app.dispatch.now()
    assert (await vibe(app.home, app.store, now)).preset is None  # still the evening: nothing before wake-up
    morning = await vibe(app.home, app.store, now.replace(day=2, hour=7, minute=31))
    assert (morning.preset.name, morning.reason, morning.source) == ("fboy", "feeling cheeky", "reflection")
    assert await app.store.kv_get(reflection.VIBE_DAY_KEY) == "2026-01-01"


async def test_an_invalid_or_failed_pick_falls_back_to_a_weighted_random_one(make_app, caplog):
    for reply in (
        json.dumps({"preset": "assistant", "reason": "task mode"}), json.dumps({"preset": "nope", "reason": "x"}),
        json.dumps({"preset": "fboy", "reason": ""}), json.dumps(["fboy"]), "not json at all",
    ):
        app = make_app(contents=(PUBLIC, reply))
        install(app)
        await auto(app)
        await reflection.run(app)
        [item] = json.loads(await app.store.kv_get(AUTO_PICKS_KEY))
        assert item["name"] in ("classic", "fboy") and item["source"] == "random" and item["reason"]
    assert "invalid" in caplog.text


async def test_nothing_is_asked_or_picked_unless_everywhere_is_auto(make_app):
    app = make_app(contents=(PUBLIC,))
    install(app)
    await app.store.kv_set("preset:global", "classic")
    await reflection.run(app)
    assert len(app.script.requests) == 1 and await app.store.kv_get(AUTO_PICKS_KEY) is None


async def test_the_daily_pick_is_made_once_per_day(make_app):
    app = make_app(contents=(PUBLIC, json.dumps({"preset": "fboy", "reason": "x"})))
    install(app)
    await auto(app)
    await reflection.run(app)
    await reflection._vibe(app, app.home, OWNER, reflection_day(app), app.dispatch.now())
    assert len(app.script.requests) == 2


def reflection_day(app):
    return app.dispatch.now().date()


async def test_pins_beat_auto_and_the_prompt_says_dom_chose_his_vibe(make_app):
    app = make_app()
    install(app)
    await auto(app)
    await approve(app, GROUP)
    await approve(app, FRIEND)
    await pick(app, "fboy", "feeling cheeky")
    await app.store.set_chat_preset(GROUP, "classic")
    _, pinned = await for_chat(app.home, app.store, GROUP, app.dispatch.now())
    _, free = await for_chat(app.home, app.store, FRIEND, app.dispatch.now())
    assert pinned.name == "classic" and pinned.picked == ""
    assert free.name == "fboy" and free.picked == "feeling cheeky"
    ask = lambda preset: build_messages(app.home, "group", GROUP, [], "<w/>", "n", preset=preset)[1]["content"]  # noqa: E731
    assert "You chose this preset yourself as today's vibe: feeling cheeky" in ask(free)
    assert "chose this preset" not in ask(pinned)


async def test_switch_tool_is_offered_only_in_auto_and_works_once_a_day(make_app):
    app = make_app()
    install(app)
    await approve(app, OWNER)
    await approve(app, FRIEND)
    assert "switch_vibe" not in [t.name for t in await enabled_tools(app, "friend_dm")]
    await auto(app)
    assert "switch_vibe" in [t.name for t in await enabled_tools(app, "friend_dm")]
    await pick(app, "classic")
    tool = SWITCH_VIBE_TOOL.handler
    friend = ctx(app, FRIEND, "friend_dm")

    for bad in ({"preset": "assistant", "reason": "task"}, {"preset": "nope", "reason": "x"}, {"preset": "fboy", "reason": " "}):
        assert (await tool(bad, friend)).startswith("error:")
    assert (await tool({"preset": "classic", "reason": "same"}, friend)).startswith("error: that is already")

    assert json.loads(await tool({"preset": "fboy", "reason": "she was flirting back"}, friend))["preset"] == "fboy"
    today = await vibe(app.home, app.store, app.dispatch.now())
    assert (today.preset.name, today.source, today.reason) == ("fboy", "switch", "she was flirting back")
    events = await app.store.get_recent_chat_window(OWNER)
    assert any(m.role == "event" and "switched his vibe to Fboy: she was flirting back" in m.text for m in events)

    assert "already switched" in await tool({"preset": "classic", "reason": "again"}, friend)
    assert (await vibe(app.home, app.store, app.dispatch.now())).preset.name == "fboy"
    # the next day's pick starts a fresh allowance
    await pick(app, "classic", "new day", since=app.dispatch.now() + timedelta(hours=8))
    app.dispatch.now.advance(hours=9)
    assert json.loads(await tool({"preset": "fboy", "reason": "again"}, friend))["preset"] == "fboy"


async def test_switch_is_refused_when_locked_or_in_a_chat_with_its_own_preset(make_app):
    app = make_app()
    install(app)
    await approve(app, GROUP)
    await pick(app, "classic")
    group = ctx(app, GROUP, "group")
    await app.store.kv_set("preset:global", "classic")
    assert "fixed your preset" in await SWITCH_VIBE_TOOL.handler({"preset": "fboy", "reason": "x"}, group)
    await auto(app)
    await app.store.set_chat_preset(GROUP, "classic")
    assert "own preset" in await SWITCH_VIBE_TOOL.handler({"preset": "fboy", "reason": "x"}, group)
    assert (await vibe(app.home, app.store, app.dispatch.now())).preset.name == "classic"


async def test_preset_auto_goes_through_the_same_validation_as_other_presets(make_app):
    app = make_app()
    install(app)
    await approve(app, GROUP)
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/preset"))
    data = "presetset:all:auto"
    await handlers.handle_callback(app, callback(app, FRIEND, data))
    assert await app.store.kv_get("preset:global") is None  # only the owner
    await handlers.handle_callback(app, callback(app, OWNER, data))
    assert await app.store.kv_get("preset:global") == "auto"
    assert "Auto" in app.bot.of("send", OWNER)[-1][2]
    [item] = json.loads(await app.store.kv_get(AUTO_PICKS_KEY))
    assert item["name"] in ("classic", "fboy") and item["source"] == "random"
    # a chat cannot be pinned to auto, and a real pin still works
    assert await owner.set_preset(app, await owner.approved_chat(app, GROUP), "auto") is None
    assert await owner.set_preset(app, await owner.approved_chat(app, GROUP), "classic")
    await handlers.handle_callback(app, callback(app, OWNER, "presetset:all:"))
    assert await app.store.kv_get("preset:global") == ""


async def test_auto_needs_a_preset_to_pick_from_and_a_stale_pick_is_replaced(make_app):
    app = make_app()
    put(app.home, "presets/assistant.md", "---\ntitle: Assistant\n---\nBe efficient.")
    assert await owner.set_global_preset(app, "auto", "The owner") is None
    assert await app.store.kv_get("preset:global") is None
    install(app)
    assert await ensure_pick(app.home, app.store, app.dispatch.now())
    first = await app.store.kv_get(AUTO_PICKS_KEY)
    assert await ensure_pick(app.home, app.store, app.dispatch.now() + timedelta(hours=5))
    assert await app.store.kv_get(AUTO_PICKS_KEY) == first
    await ensure_pick(app.home, app.store, app.dispatch.now() + timedelta(days=2))
    assert await app.store.kv_get(AUTO_PICKS_KEY) != first


async def test_a_pick_for_a_removed_preset_is_ignored(make_app):
    app = make_app()
    install(app)
    await auto(app)
    await pick(app, "fboy")
    (app.home.root / "presets" / "fboy.md").unlink()
    assert (await vibe(app.home, app.store, app.dispatch.now())).preset is None
    await app.store.kv_set(AUTO_PICKS_KEY, "garbage")
    assert (await vibe(app.home, app.store, app.dispatch.now())).preset is None


async def test_self_inspect_tells_dom_his_vibe_and_that_he_picked_it(make_app):
    app = make_app()
    install(app)
    await auto(app)
    await pick(app, "fboy", "feeling cheeky")
    found = json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))["preset"]
    assert found["everywhere"] == "auto"
    assert found["today"] == {"mode": "auto", "preset": "fboy", "your_reason": "feeling cheeky", "how": "reflection"}


async def test_dashboard_shows_the_vibe_its_reason_and_whether_it_is_auto_or_locked(make_app, browser):  # noqa: F811
    app = make_app()
    install(app)
    await approve(app, GROUP, "Book Club")
    async with browser(app) as client:
        async def vibe_now():
            data = (await client.get("/api/chats")).json()
            return data["everywhere"], data["vibe"]

        async def set_preset(chat, preset):
            return await client.post("/api/chats/preset", json={"chat_id": chat, "preset": preset}, headers=POST_HEADERS)

        assert (await vibe_now())[0] in (None, "")
        response = await set_preset("all", "auto")
        assert response.json()["everywhere"] == "auto" and await app.store.kv_get("preset:global") == "auto"
        await pick(app, "fboy", "feeling <cheeky>")
        everywhere, vibe = await vibe_now()
        assert everywhere == "auto" and vibe["mode"] == "auto" and vibe["title"] == "Fboy" and vibe["reason"] == "feeling <cheeky>"
        await pick(app, "classic", "calm down", source="switch")
        everywhere, vibe = await vibe_now()
        assert vibe["title"] == "Classic" and vibe["source"] == "switch" and vibe["reason"] == "calm down"
        await set_preset("all", "fboy")
        everywhere, vibe = await vibe_now()
        assert everywhere == "fboy" and vibe["mode"] == "locked" and vibe["title"] == "Fboy"
        # per-chat editing still works under auto
        await set_preset("all", "auto")
        await set_preset(GROUP, "classic")
        assert (await app.store.list_chats("approved"))[0]["preset"] == "classic"
