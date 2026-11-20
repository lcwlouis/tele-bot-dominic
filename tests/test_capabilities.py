import pytest

from dom.tools import Tool, core_tools
from test_usage import deliver, online
from tghelpers import tg


@pytest.mark.parametrize("scope", ["group", "friend_dm", "owner_dm"])
async def test_capabilities_reflect_offered_tools(make_app, monkeypatch, scope):
    from dom import capabilities

    app = make_app()
    owner_tool = Tool("example_tool", "Only here.", {}, None, {"owner_dm"})
    monkeypatch.setattr(capabilities, "core_tools", lambda search: [*core_tools(search), owner_tool])
    note = capabilities.describe(app, scope)
    assert ("example_tool" in note) == (scope == "owner_dm")
    assert all(tool.name in note for tool in core_tools() if scope in tool.scopes)
    assert "understands: text" in note
    assert all(word not in note for word in ("photos", "GIFs", "video", "voice"))
    assert "desktop: off" in note
    assert all(phrase in note for phrase in ("read receipts", "deleted messages", "chats he isn't in"))
    assert len(note) <= 600

    app.media_describer = object()
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    note = capabilities.describe(app, scope)
    assert "understands: text, photos, GIFs, video, voice" in note
    assert ("desktop: unavailable" if scope == "owner_dm" else "desktop: off") in note
    assert len(note) <= 600


async def test_capabilities_appear_in_turn_note(make_app):
    app = make_app()
    await online(app)
    await deliver(app, tg())
    messages = app.script.requests[-1]["messages"]
    assert "understands: text" in messages[1]["content"]
    assert "understands: text" not in messages[0]["content"]


async def test_capabilities_list_every_tool_name_whole(make_app, monkeypatch):
    from dom import capabilities

    app = make_app()
    extras = [Tool(f"owner_tool_{n}", "Does a thing. Then more.", {}, None, {"owner_dm"}) for n in range(10)]
    monkeypatch.setattr(capabilities, "core_tools", lambda search: [*core_tools(search), *extras])
    note = capabilities.describe(app, "owner_dm")
    assert all(tool.name in note for tool in [*core_tools(), *extras])
    assert "(" not in note.splitlines()[0]  # descriptions stay out of the line
    assert len(note) <= 600


async def test_capabilities_overflow_drops_whole_names(make_app, monkeypatch):
    from dom import capabilities

    app = make_app()
    extras = [Tool(f"long_plugin_tool_name_{n:02d}", "x", {}, None, {"owner_dm"}) for n in range(40)]
    monkeypatch.setattr(capabilities, "core_tools", lambda search: [*core_tools(search), *extras])
    first = capabilities.describe(app, "owner_dm").splitlines()[0]
    assert len(capabilities.describe(app, "owner_dm")) <= 600
    assert "more)" in first
    shown = first.removeprefix("tools: ").split(" (+")[0].split(", ")
    assert all(name in {tool.name for tool in [*core_tools(), *extras]} for name in shown)
