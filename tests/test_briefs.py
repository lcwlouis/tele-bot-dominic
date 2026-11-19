import json
from datetime import date
from unittest.mock import AsyncMock

import pytest

from dom.briefs import LIMITS, write_brief
from dom.tools import SELF_INSPECT_TOOL, WRITE_BRIEF_TOOL, TurnCtx, enabled_tools
from test_owner_events import events
from test_web_pages import browser
from tghelpers import GROUP, OWNER


FIELDS = {
    "title": "Improve Café / tests",
    "problem": "The <script> tag is shown incorrectly.",
    "proposal": "Escape the content.",
    "acceptance": "- Display the tag as text.\n- Keep other tools working.",
}


def ctx(app, scope="owner_dm", chat=OWNER):
    return TurnCtx(scope, chat, chat, app.home, app.store, OWNER, app=app)


def test_brief_content_and_slug(make_app):
    app = make_app()
    brief = write_brief(app.home, date(2026, 11, 19), **FIELDS)
    assert brief.filename == "2026-11-19-improve-cafe-tests.md"
    assert app.home.read(f"briefs/{brief.filename}") == (
        "# Improve Café / tests\n\n"
        "## Problem\n\nThe <script> tag is shown incorrectly.\n\n"
        "## Proposed change\n\nEscape the content.\n\n"
        "## Acceptance criteria\n\n- Display the tag as text.\n- Keep other tools working.\n\n"
        "written by Dom on 2026-11-19\n"
    )
    duplicate = write_brief(app.home, date(2026, 11, 19), **FIELDS)
    assert duplicate.filename == "2026-11-19-improve-cafe-tests-2.md"


@pytest.mark.parametrize("field", FIELDS)
def test_brief_field_limits(make_app, field):
    app = make_app()
    with pytest.raises(ValueError, match=field):
        write_brief(app.home, date(2026, 11, 19), **(FIELDS | {field: "x" * (LIMITS[field] + 1)}))
    assert not (app.home.root / "briefs").exists()


async def test_write_brief_dms_document_and_renders_dashboard(make_app, browser):
    app = make_app()
    app.bot.send_document = AsyncMock()
    app.home.scoped(allow_private=True)._write_regular("private/NOTES.md", "- Confidential sample phrase\n")
    result = await WRITE_BRIEF_TOOL.handler(FIELDS, ctx(app))
    expected = "2026-01-01-improve-cafe-tests.md"
    assert json.loads(result) == {"filename": expected}
    send = app.bot.send_document.await_args
    assert send.args[0] == OWNER and send.args[1].filename == expected
    assert send.args[1].data.decode() == app.home.read(f"briefs/{expected}")
    assert send.kwargs["caption"] == "Core-change brief: Improve Café / tests"
    assert "Confidential sample phrase" not in send.args[1].data.decode()
    assert any(expected in row["text"] for row in await events(app))

    async with browser(app) as client:
        assert (await client.get("/briefs", headers={"Authorization": ""})).status_code == 403
        html = (await client.get("/briefs")).text
    assert "Briefs" in html and "Improve Café / tests" in html and "Copy" in html
    assert "<h4>Problem</h4>" in html and "&lt;script&gt;" in html and "<script>" not in html
    assert "<li>Display the tag as text.</li>" in html
    assert "Confidential sample phrase" not in html


async def test_brief_excludes_private_content_and_requires_owner(make_app):
    app = make_app()
    app.bot.send_document = AsyncMock()
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "- Confidential sample phrase\n")
    fields = FIELDS | {"proposal": "Include Confidential sample phrase."}
    assert "private notes" in await WRITE_BRIEF_TOOL.handler(fields, ctx(app))
    assert "owner" in await WRITE_BRIEF_TOOL.handler(FIELDS, ctx(app, "group", GROUP))
    app.bot.send_document.assert_not_awaited()
    assert not (app.home.root / "briefs").exists()


async def test_self_inspect_shape_and_owner_only_tools(make_app):
    app = make_app()
    now = app.dispatch.now()
    for n in range(22):
        await app.store.record_turn(now, GROUP, "group", 1, [], "m", False, n, f"Turn failed {n}")
    await app.store.record_turn(now, GROUP, "group", 1, [], "m", False, 22, "Replied")
    found = json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))
    assert found.keys() == {
        "version", "changelog_latest", "tools", "approved_plugins", "plugin_sandbox_failure", "desktop", "models",
        "last_errors",
    }
    assert found["plugin_sandbox_failure"] is None
    app.plugin_runner.last_failure = "network probe: bind allowed"
    assert json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))["plugin_sandbox_failure"] == (
        "network probe: bind allowed"
    )
    assert isinstance(found["version"], str) and found["version"]
    assert found["tools"].keys() == {"owner_dm", "friend_dm", "group"}
    assert {"draft_plugin", "write_brief", "self_inspect"} <= set(found["tools"]["owner_dm"])
    for scope in ("owner_dm", "group", "friend_dm"):
        assert found["tools"][scope] == [tool.name for tool in await enabled_tools(app, scope)]
    assert "self_inspect" not in found["tools"]["group"]
    assert found["approved_plugins"] == [] and found["desktop"] == "off" and found["models"] == {"chat": "m"}
    assert [row["outcome"] for row in found["last_errors"]] == [f"Turn failed {n}" for n in range(21, 1, -1)]
    assert all("chat_id" not in row for row in found["last_errors"])
    assert "owner" in await SELF_INSPECT_TOOL.handler({}, ctx(app, "group", GROUP))
