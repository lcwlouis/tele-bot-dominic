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
    assert "desktop: unavailable" in note
    assert len(note) <= 600


async def test_capabilities_appear_in_turn_note(make_app):
    app = make_app()
    await online(app)
    await deliver(app, tg())
    messages = app.script.requests[-1]["messages"]
    assert "understands: text" in messages[1]["content"]
    assert "understands: text" not in messages[0]["content"]
