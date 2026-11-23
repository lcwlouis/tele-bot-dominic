"""Plugin slash commands and dashboard panels: manifest, review, routing, rendering and limits."""

import io
import json

import pytest
from aiogram.types import User

from dom.plugins import dom_plugin, runner, surfaces
from dom.plugins.manifest import RESERVED_COMMANDS, Manifest
from dom.plugins.runner import PluginBusy, PluginError
from dom.presence import read_habits
from dom.telegram import handlers
from dom.telegram.owner import OWNER_COMMANDS
from test_owner_events import press
from test_plugin_tools import JSON, decide, draft, read_review, review_code, status
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg

BASE = """name = "convert"
description = "Convert units"
version = "1"
"""
TOOL = """[[tools]]
name = "length"
description = "Convert lengths"
parameters = {type = "object"}
"""
COMMAND = """[[commands]]
name = "weather"
description = "Weather right now"
"""
PANEL = """[[panels]]
id = "summary"
title = "Weekly summary"
"""
SURFACES = BASE + TOOL + COMMAND + PANEL
PANEL_API = "/api/system/plugins/panel"
PANEL_BODY = {"name": "convert", "panel_id": "summary"}


# ---- manifest ---------------------------------------------------------------------------

def test_commands_and_panels_parse_with_owner_dm_default():
    manifest = Manifest.parse(SURFACES)
    command, = manifest.commands
    assert (command.name, command.description, command.scopes) == ("weather", "Weather right now", ("owner_dm",))
    panel, = manifest.panels
    assert (panel.id, panel.title) == ("summary", "Weekly summary")


def test_a_plugin_may_offer_only_commands_or_only_panels():
    assert Manifest.parse(BASE + COMMAND).tools == ()
    assert Manifest.parse(BASE + PANEL).commands == ()
    with pytest.raises(ValueError, match="at least one"):
        Manifest.parse(BASE)


def test_core_commands_are_reserved():
    assert {command.command for command in OWNER_COMMANDS} <= RESERVED_COMMANDS
    assert {"claim", "desktop", "start", "help", "update"} <= RESERVED_COMMANDS


@pytest.mark.parametrize("old, new", [
    ('name = "weather"', 'name = "status"'),
    ('name = "weather"', 'name = "claim"'),
    ('name = "weather"', 'name = "Weather"'),
    ('name = "weather"', 'name = "wea-ther"'),
    ('name = "weather"', 'name = "' + "w" * 33 + '"'),
    ('name = "weather"', 'name = "for"'),
    ('name = "weather"', 'name = 3'),
    ('"Weather right now"', '"' + "a" * 101 + '"'),
    ('"Weather right now"', '"hidden\\u202e"'),
    ('"Weather right now"', '"two\\nlines"'),
    ('description = "Weather right now"', 'description = "Weather"\nscopes = ["everyone"]'),
    ('description = "Weather right now"', 'description = "Weather"\nscopes = "group"'),
    ('description = "Weather right now"', 'description = "Weather"\nscopes = []'),
    ('description = "Weather right now"', 'description = "Weather"\nnetwork = true'),
    ('id = "summary"', 'id = "Summary"'),
    ('id = "summary"', 'id = "a b"'),
    ('id = "summary"', 'id = "class"'),
    ('title = "Weekly summary"', 'title = "' + "t" * 81 + '"'),
    ('title = "Weekly summary"', 'title = "Weekly"\nheight = 3'),
    ('[[commands]]', '[commands]'),
    ('[[panels]]', '[panels]'),
])
def test_invalid_commands_and_panels(old, new):
    assert old in SURFACES
    with pytest.raises(ValueError):
        Manifest.parse(SURFACES.replace(old, new))


def test_duplicate_and_too_many_surfaces_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        Manifest.parse(SURFACES + COMMAND)
    with pytest.raises(ValueError, match="duplicate"):
        Manifest.parse(SURFACES + PANEL)
    many = "".join(COMMAND.replace("weather", f"weather{i}") for i in range(11))
    with pytest.raises(ValueError, match="10"):
        Manifest.parse(BASE + many)
    panels = "".join(PANEL.replace("summary", f"summary{i}") for i in range(6))
    with pytest.raises(ValueError, match="5"):
        Manifest.parse(BASE + panels)


def test_network_plugin_commands_stay_in_the_owner_dm():
    online = SURFACES.replace('version = "1"', 'version = "1"\nnetwork = true')
    assert Manifest.parse(online).network
    with pytest.raises(ValueError, match="owner_dm"):
        Manifest.parse(online.replace('description = "Weather right now"', 'description = "W"\nscopes = ["group"]'))


# ---- review ---------------------------------------------------------------------------------

async def approve(app, browser, manifest=SURFACES, monkeypatch=None):
    proposal = await draft(app, manifest=manifest)
    row = await status(app)
    async with browser(app) as client:
        await review_code(client, row)
        assert (await decide(client, row)).status_code == 200
    if monkeypatch is not None:
        monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)
    return proposal


async def test_review_dm_and_dashboard_show_declared_surfaces(make_app, browser):
    app = make_app()
    manifest = SURFACES.replace('description = "Weather right now"', 'description = "Weather right now"\nscopes = ["owner_dm", "group"]')
    await draft(app, manifest=manifest)
    sent, = app.bot.of("send", OWNER)
    assert "Commands: /weather (owner_dm, group)" in sent[2]
    assert "Panels: summary (Weekly summary)" in sent[2]
    async with browser(app) as client:
        review = (await read_review(client, await status(app))).json()
    assert review["commands"] == [{"name": "weather", "description": "Weather right now", "scopes": ["owner_dm", "group"]}]
    assert review["panels"] == [{"id": "summary", "title": "Weekly summary"}]


async def test_changing_declared_surfaces_needs_fresh_approval(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    assert [c.name for _, c in await app.plugins.command_entries("owner_dm")] == ["weather"]
    wider = SURFACES.replace('description = "Weather right now"', 'description = "Weather right now"\nscopes = ["group"]')
    await draft(app, manifest=wider)
    row = await status(app)
    assert row["status"] == "pending" and row["hash"] != row["approved_hash"]
    assert await app.plugins.command_entries("owner_dm") == []
    assert await app.plugins.command_entries("group") == []
    assert await app.plugins.panel_entries() == []
    async with browser(app) as client:
        review = (await read_review(client, row)).json()
    assert review["surfaces_changed"] is True


async def test_command_names_cannot_collide_between_plugins(make_app, browser):
    app = make_app()
    await draft(app, manifest=SURFACES)
    other = BASE.replace('"convert"', '"other"') + COMMAND
    with pytest.raises(ValueError, match="/weather"):
        await app.proposals.create("plugin", "", {
            "name": "other", "description": "Convert units", "files": {"main.py": "pass\n"}, "manifest_toml": other,
        }, "draft_plugin")


async def test_colliding_commands_on_disk_are_offered_by_neither(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    folder = app.home.root / "plugins" / "other"
    folder.mkdir()
    (folder / "plugin.toml").write_text(BASE.replace('"convert"', '"other"') + COMMAND)
    (folder / "main.py").write_text("pass\n")
    other = (await app.plugins.scan())["other"]
    await app.store.decide_plugin("other", other.hash, "approve", {})
    assert await app.plugins.command_entries("owner_dm") == []


# ---- commands -------------------------------------------------------------------------------

def fake_runner(app, monkeypatch, result="sunny"):
    calls = []

    def run_request(folder, request, manifest, *, approved_hash):
        calls.append(request)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app.plugin_runner, "run_request", run_request)
    return calls


async def command(app, text, chat=OWNER, sender=OWNER, msg_id=40, bot=False):
    message = tg(chat_id=chat, sender=sender, text=text, msg_id=msg_id)
    if bot:
        message = message.model_copy(update={"from_user": User(id=sender, is_bot=True, first_name="Bot")})
    await handlers.handle_message(app, message)
    await drain_dispatch(app.dispatch)


async def test_owner_dm_command_runs_the_plugin_with_only_its_arguments(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch, "Sunny‮, 21 °C\x07")
    before = len(app.bot.of("send", OWNER))
    await command(app, "/weather@dombot  Berlin   tomorrow ")
    assert calls == [{"command": "weather", "args": "Berlin   tomorrow", "scope": "owner_dm"}]
    reply = app.bot.of("send", OWNER)[before:]
    assert [call[2] for call in reply] == ["Sunny, 21 °C"]
    assert reply[0][3]["link_preview_options"].is_disabled and "parse_mode" not in reply[0][3]
    assert not await app.store.has_message(OWNER, 40)  # neither the command nor the reply enters chat history
    assert app.script.requests == []


async def test_commands_run_only_where_declared_and_approved(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch)
    await app.store.set_chat_auth_status(GROUP, "approved", title="Club")
    await command(app, "/weather", chat=GROUP, sender=OWNER, msg_id=41)  # declared for owner_dm only
    await command(app, "/weather", chat=FRIEND, sender=FRIEND, msg_id=42)
    await command(app, "/weather@otherbot", msg_id=43)
    assert calls == []


async def test_group_command_needs_an_approved_chat_and_a_human_sender(make_app, browser, monkeypatch):
    app = make_app()
    manifest = SURFACES.replace('description = "Weather right now"', 'description = "W"\nscopes = ["group"]')
    await approve(app, browser, manifest=manifest, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch)
    await app.store.set_chat_auth_status(GROUP, "pending_approval", title="Club")
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=44)
    assert calls == []
    await app.store.set_chat_auth_status(GROUP, "approved", title="Club")
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=45, bot=True)
    assert calls == []
    await command(app, "/weather now", chat=GROUP, sender=FRIEND, msg_id=46)
    assert calls == [{"command": "weather", "args": "now", "scope": "group"}]
    assert app.bot.of("send", GROUP)[-1][2] == "sunny"


async def test_group_commands_have_a_per_chat_cooldown(make_app, browser, monkeypatch):
    app = make_app()
    manifest = SURFACES.replace('description = "Weather right now"', 'description = "W"\nscopes = ["owner_dm", "group"]')
    await approve(app, browser, manifest=manifest, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch)
    await app.store.set_chat_auth_status(GROUP, "approved", title="Club")
    clock = [100.0]
    monkeypatch.setattr(surfaces, "monotonic", lambda: clock[0])
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=50)
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=51)
    assert len(calls) == 1
    clock[0] += surfaces.COMMAND_COOLDOWN_SECONDS
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=52)
    await command(app, "/weather", msg_id=53)
    await command(app, "/weather", msg_id=54)  # the owner's DM has no cooldown
    assert len(calls) == 4


def asleep(app):
    app.dispatch.now.at = app.dispatch.now.at.replace(hour=2)  # inside the default sleep window


async def test_asleep_dom_ignores_plugin_commands_outside_the_owner_dm(make_app, browser, monkeypatch):
    app = make_app()
    manifest = SURFACES.replace('description = "Weather right now"', 'description = "W"\nscopes = ["group", "friend_dm"]')
    await approve(app, browser, manifest=manifest, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch)
    await app.store.set_chat_auth_status(GROUP, "approved", title="Club")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Fran")
    asleep(app)
    sent = len(app.bot.calls)
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=70)
    await command(app, "/weather", chat=FRIEND, sender=FRIEND, msg_id=71)
    assert calls == [] and len(app.bot.calls) == sent
    assert await app.store.has_message(GROUP, 70)  # kept like any message, for when he wakes up


async def test_asleep_dom_runs_an_owner_command_then_grumbles_in_character(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch)
    asleep(app)
    app.script.contents[:] = [json.dumps({"bubbles": [{"text": "u woke me up for the weather??"}]})]
    before = len(app.bot.of("send", OWNER))
    await command(app, "/weather", msg_id=72)
    assert calls == [{"command": "weather", "args": "", "scope": "owner_dm"}]
    assert [call[2] for call in app.bot.of("send", OWNER)[before:]] == ["sunny", "u woke me up for the weather??"]
    note = app.script.requests[-1]["messages"][-1]["content"]
    assert "/weather" in note and "asleep" in note and "sunny" not in note  # plugin output never reaches the model
    assert (await app.dispatch.presence.state(OWNER, read_habits(app.home)))[0] == "asleep"


async def test_awake_owner_command_gets_no_grumble(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    fake_runner(app, monkeypatch)
    await command(app, "/weather", msg_id=73)
    assert app.script.requests == []


async def test_long_replies_are_cut_and_long_arguments_refused(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch, "x" * 5000)
    await command(app, "/weather " + "a" * (surfaces.MAX_COMMAND_ARGS + 1), msg_id=60)
    assert calls == []
    assert "too long" in app.bot.of("send", OWNER)[-1][2]
    await command(app, "/weather", msg_id=61)
    reply = app.bot.of("send", OWNER)[-1][2]
    assert len(reply) == surfaces.MAX_REPLY and reply.endswith("…")


async def test_private_notes_never_reach_a_command(make_app, browser, monkeypatch):
    app = make_app()
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "Allergic to hazelnuts and pollen\n")
    await approve(app, browser, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch)
    await command(app, "/weather allergic to hazelnuts and pollen", msg_id=62)
    assert calls == []
    assert "private notes" in app.bot.of("send", OWNER)[-1][2]


@pytest.mark.parametrize("error, owner_sees", [
    (PluginError("boom <b>"), "boom <b>"),
    (PluginBusy("plugin busy, try again"), "busy"),
])
async def test_command_errors(make_app, browser, monkeypatch, error, owner_sees):
    app = make_app()
    manifest = SURFACES.replace('description = "Weather right now"', 'description = "W"\nscopes = ["owner_dm", "group"]')
    await approve(app, browser, manifest=manifest, monkeypatch=monkeypatch)
    fake_runner(app, monkeypatch, error)
    await command(app, "/weather", msg_id=63)
    assert owner_sees in app.bot.of("send", OWNER)[-1][2]
    await app.store.set_chat_auth_status(GROUP, "approved", title="Club")
    await command(app, "/weather", chat=GROUP, sender=FRIEND, msg_id=64)
    assert "boom" not in app.bot.of("send", GROUP)[-1][2]


async def test_a_failed_command_tells_dom_through_an_event_without_the_plugin_output(make_app, browser, monkeypatch):
    # /km on the Pi tried to save its history and failed; Dom never learnt that his plugin was broken.
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    fake_runner(app, monkeypatch, PluginError("[Errno 30] Read-only file system: '~' secret-output"))
    await command(app, "/weather", msg_id=65)
    assert "plugins cannot save files between runs" in app.bot.of("send", OWNER)[-1][2]
    rows = app.store._conn.execute("SELECT text FROM messages WHERE role = 'event' ORDER BY id").fetchall()
    note = rows[-1]["text"]
    assert note.startswith("/weather from plugin") and note.endswith("failed when it ran, because it tried to save a file")
    assert "secret-output" not in note


async def test_unapproved_or_unavailable_plugins_do_not_capture_commands(make_app, browser, monkeypatch):
    app = make_app()
    await draft(app, manifest=SURFACES)
    calls = fake_runner(app, monkeypatch)
    monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)
    await command(app, "/weather", msg_id=65)
    assert calls == []


async def test_owner_menu_lists_approved_owner_dm_commands(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser)
    commands, scope = app.bot.of("set_commands")[-1][1:]
    assert scope.chat_id == OWNER
    assert commands[:len(OWNER_COMMANDS)] == OWNER_COMMANDS
    assert [(c.command, c.description) for c in commands[len(OWNER_COMMANDS):]] == [("weather", "Weather right now")]
    row = await status(app)
    await press(app, f"plugin:disable:{row['name']}")
    assert app.bot.of("set_commands")[-1][1] == OWNER_COMMANDS


# ---- panels ---------------------------------------------------------------------------------

def test_valid_panel_data():
    data = surfaces.validate_panel({
        "title": "This week", "paragraphs": ["Line one\nline two"], "items": [["Runs", 3], ["Pace", 5.5]],
        "table": {"columns": ["Day", "Km"], "rows": [["Mon", 5], ["Tue", "6"]]},
    })
    assert data.title == "This week"
    assert data.pairs == (("Runs", "3"), ("Pace", "5.5"))
    assert data.columns == ("Day", "Km") and data.rows == (("Mon", "5"), ("Tue", "6"))
    assert surfaces.validate_panel({}).paragraphs == ()


@pytest.mark.parametrize("data", [
    [], "text", {"html": "<b>x</b>"}, {"title": 3}, {"title": "a" * 121}, {"title": "two\nlines"},
    {"paragraphs": "one"}, {"paragraphs": ["x"] * 11}, {"paragraphs": ["a" * 1001]}, {"paragraphs": ["bidi‮"]},
    {"items": [["only key"]]}, {"items": [["k", None]]}, {"items": [["k", True]]}, {"items": [["k", float("nan")]]},
    {"items": [["k", "v"]] * 31}, {"items": [["k" * 81, "v"]]}, {"items": [["k", {"nested": 1}]]},
    {"table": {"columns": [], "rows": []}}, {"table": {"columns": ["a"] * 9, "rows": []}},
    {"table": {"columns": ["a", "b"], "rows": [["1"]]}}, {"table": {"columns": ["a"], "rows": [["1"]] * 51}},
    {"table": {"columns": ["a"], "rows": [["x" * 201]]}}, {"table": {"columns": ["a"], "rows": [], "style": "x"}},
])
def test_invalid_panel_data(data):
    with pytest.raises(ValueError):
        surfaces.validate_panel(data)


async def test_panels_render_structured_data_escaped(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    calls = fake_runner(app, monkeypatch, {
        "title": "<script>alert(1)</script>", "paragraphs": ["<img src=x onerror=alert(1)>"],
        "items": [["<b>key</b>", "value"]], "table": {"columns": ["<i>c</i>"], "rows": [["<a href='javascript:x'>"]]},
    })
    async with browser(app) as client:
        listed = (await client.get("/api/system/plugins")).json()
        assert listed["panels"] == [{"plugin": "convert", "id": "summary", "title": "Weekly summary"}]
        assert (await client.get(PANEL_API)).status_code == 405
        assert (await client.post(PANEL_API, json=PANEL_BODY)).status_code == 403  # no Origin
        rendered = await client.post(PANEL_API, json=PANEL_BODY, headers=JSON)
    assert rendered.status_code == 200
    assert calls == [{"panel": "summary"}]
    # The API returns the plugin's text as data only; the app shows it as text, never as markup.
    assert rendered.json() == {
        "plugin": "convert", "id": "summary", "error": None, "title": "<script>alert(1)</script>",
        "paragraphs": ["<img src=x onerror=alert(1)>"], "pairs": [["<b>key</b>", "value"]],
        "columns": ["<i>c</i>"], "rows": [["<a href='javascript:x'>"]],
    }


async def test_invalid_panel_data_and_errors_show_a_message(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    fake_runner(app, monkeypatch, {"html": "<b>x</b>"})
    async with browser(app) as client:
        rendered = await client.post(PANEL_API, json=PANEL_BODY, headers=JSON)
        assert rendered.status_code == 200 and "returned data Dom cannot show" in rendered.json()["error"]
        assert "<b>x</b>" not in rendered.text
        fake_runner(app, monkeypatch, PluginError("panel <broke>"))
        rendered = await client.post(PANEL_API, json=PANEL_BODY, headers=JSON)
        assert "panel <broke>" in rendered.json()["error"]
        assert (await client.post(PANEL_API, json=PANEL_BODY | {"panel_id": "missing"}, headers=JSON)).status_code == 404
        assert (await client.post(PANEL_API, json={"name": "Bad", "panel_id": "summary"}, headers=JSON)).status_code == 404


async def test_pending_plugin_panels_are_not_shown_or_run(make_app, browser, monkeypatch):
    app = make_app()
    await draft(app, manifest=SURFACES)
    calls = fake_runner(app, monkeypatch, {})
    monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)
    async with browser(app) as client:
        assert (await client.get("/api/system/plugins")).json()["panels"] == []
        assert (await client.post(PANEL_API, json=PANEL_BODY, headers=JSON)).status_code == 404
    assert calls == []


# ---- runner protocol and SDK ----------------------------------------------------------------

def test_protocol_accepts_objects_only_for_panels():
    assert runner.parse_output(b'{"ok":true,"result":{"title":"t"}}\n', structured=True) == {"title": "t"}
    with pytest.raises(PluginError, match="protocol"):
        runner.parse_output(b'{"ok":true,"result":{"title":"t"}}\n')
    with pytest.raises(PluginError, match="protocol"):
        runner.parse_output(b'{"ok":true,"result":"text"}\n', structured=True)


def test_run_wraps_tool_calls_as_requests(monkeypatch, tmp_path):
    seen = []
    plugin_runner = runner.PluginRunner(home=tmp_path)
    monkeypatch.setattr(plugin_runner, "run_request", lambda folder, request, manifest, *, approved_hash: seen.append(request) or "ok")
    assert plugin_runner.run(tmp_path, "length", {"a": 1}, None, approved_hash="h") == "ok"
    assert seen == [{"tool": "length", "args": {"a": 1}}]


def sdk_exchange(monkeypatch, request):
    monkeypatch.setattr(dom_plugin.sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(dom_plugin.sys, "stdout", out)
    dom_plugin.run()
    return json.loads(out.getvalue())


def test_sdk_dispatches_commands_and_panels(monkeypatch):
    monkeypatch.setattr(dom_plugin, "_commands", {})
    monkeypatch.setattr(dom_plugin, "_panels", {})

    @dom_plugin.command
    def weather(text, scope):
        return f"{text} in {scope}"

    @dom_plugin.panel
    def summary():
        return {"title": "t"}

    @dom_plugin.panel
    def broken():
        return {"value": object()}

    assert sdk_exchange(monkeypatch, {"command": "weather", "args": "x", "scope": "group"}) == {
        "ok": True, "result": "x in group",
    }
    assert sdk_exchange(monkeypatch, {"panel": "summary"}) == {"ok": True, "result": {"title": "t"}}
    assert sdk_exchange(monkeypatch, {"panel": "broken"})["ok"] is False


def test_the_starter_plugin_is_valid(monkeypatch):
    import sys
    import types
    from pathlib import Path

    from dom.plugins.manifest import read_plugin

    docs = Path(__file__).resolve().parents[1] / "docs"
    starter = read_plugin(docs / "examples" / "plugins" / "units")
    assert starter.manifest.tools[0].name == "length"
    assert [c.name for c in starter.manifest.commands] == ["km"] and [p.id for p in starter.manifest.panels] == ["summary"]
    assert starter.files["plugin.toml"].decode() in (docs / "plugins.md").read_text()  # the guide shows it verbatim
    monkeypatch.setattr(dom_plugin, "_commands", {})
    monkeypatch.setattr(dom_plugin, "_panels", {})
    monkeypatch.setattr(dom_plugin, "_tools", {})
    stub = types.ModuleType("_dom")
    stub.tool, stub.command, stub.panel, stub.run = dom_plugin.tool, dom_plugin.command, dom_plugin.panel, lambda: None
    monkeypatch.setitem(sys.modules, "_dom", stub)
    exec(compile(starter.files["main.py"], "main.py", "exec"), {})
    assert dom_plugin._tools["length"](value=1, unit="mi") == "1.61 km"
    assert dom_plugin._commands["km"]("3", "group") == "3.0 mi is 4.83 km"
    assert dom_plugin._commands["km"]("far", "owner_dm") == "Usage: /km <miles>"
    assert surfaces.validate_panel(dom_plugin._panels["summary"]()).rows[0] == ("Half", "21.1", "13.1")


async def test_plugin_panels_stay_after_approve_and_vanish_with_disable(make_app, browser, monkeypatch):
    app = make_app()
    await draft(app, manifest=SURFACES)
    monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)
    row = await status(app)
    async with browser(app) as client:
        await review_code(client, row)
        approved = await decide(client, row)
        assert [panel["id"] for panel in approved.json()["panels"]] == ["summary"]
        disabled = await decide(client, row, "disable")
        assert disabled.status_code == 200 and disabled.json()["panels"] == []


async def test_checking_whether_dom_is_asleep_never_writes_presence(make_app, browser, monkeypatch):
    app = make_app()
    await approve(app, browser, monkeypatch=monkeypatch)
    fake_runner(app, monkeypatch)
    asleep(app)
    app.script.contents[:] = [json.dumps({"bubbles": [{"text": "zzz"}]})]
    written = []

    async def record(*args, **kwargs):
        written.append((args, kwargs))

    monkeypatch.setattr(app.store, "set_presence", record)
    await command(app, "/weather", msg_id=74)
    assert written == []  # command handling holds no chat lock, so it must not write the presence row
