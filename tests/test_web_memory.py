import hashlib
import json

import pytest

from dom.home import MAX_SOUL, MAX_SOUL_EDIT, limit_for

from test_web_auth import POST_HEADERS
from test_web_pages import browser  # noqa: F401 (shared authenticated ASGI fixture)
from tghelpers import FRIEND, OWNER


def version(content):
    return hashlib.sha256(content.encode()).hexdigest()


async def test_editor_limits_and_stale_drafts(make_app, browser):
    app = make_app()
    memory = "memory/MEMORY.md"
    async with browser(app) as client:
        page = await client.get("/memory")
        assert page.status_code == 200
        assert "2,200 characters" in page.text and 'class="fill"' in page.text
        for file, limit in ((memory, limit_for(memory)), ("SOUL.md", MAX_SOUL)):
            data = {"file": file, "version": version(""), "content": "x" * (limit + 1)}
            response = await client.post("/memory/save", headers=POST_HEADERS, data=data)
            assert "over limit" in response.text and app.home.read(file) == ""
            data["content"] = "x" * limit
            response = await client.post("/memory/save", headers=POST_HEADERS, data=data)
            assert response.status_code == 200 and "Saved" in response.text
            assert app.home.read(file) == data["content"]
            response = await client.post("/memory/save", headers=POST_HEADERS, data=data | {"content": "stale edit"})
            assert "changed since" in response.text and app.home.read(file) == data["content"]
        events = await app.store.get_recent_chat_window(OWNER)
        assert len(events) == 2 and all(event.role == "event" for event in events)


async def test_private_files_only_listed_under_private_section_and_alias_rejected(make_app, browser):
    app = make_app()
    private = app.home.scoped(allow_private=True)
    private._write_regular("private/USER.md", "Owner only")
    private.append_journal(app.dispatch.now().date(), "Private entry", private=True)
    app.home._write_regular(f"memory/chats/{FRIEND}.md", "Friend note")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Reader")
    (app.home.root / "memory" / "chats" / "alias.md").symlink_to(app.home.root / "private" / "USER.md")

    async with browser(app) as client:
        page = await client.get("/memory")
        shared, own = page.text.split("Only in your DM", 1)
        assert "USER.md" not in shared and "Private journal" not in shared
        assert "Friend DM" in shared and "Reader" in shared
        assert "USER.md" in own and "NOTES.md" in own and "Private journal" in own
        assert "Owner only" not in page.text
        assert "Owner only" in (await client.get("/memory", params={"file": "private/USER.md"})).text
        assert "Private entry" in (await client.get("/memory", params={"file": "private/JOURNAL.md"})).text
        for file in ("memory/chats/alias.md", "memory/../private/USER.md", "config.toml"):
            assert (await client.get("/memory", params={"file": file})).status_code == 404
            response = await client.post(
                "/memory/save", headers=POST_HEADERS,
                data={"file": file, "version": version("Owner only"), "content": "overwritten"},
            )
            assert response.status_code == 404
        response = await client.post(
            "/memory/save", headers=POST_HEADERS,
            data={"file": "private/USER.md", "version": version("Owner only"), "content": "Updated owner note"},
        )
        assert "Saved" in response.text
        assert private.read("private/USER.md") == "Updated owner note"


async def test_habits_are_read_only(make_app, browser):
    app = make_app()
    app.home._write_regular("memory/HABITS.md", "sleep: 23:00-07:00")
    async with browser(app) as client:
        page = await client.get("/memory", params={"file": "memory/HABITS.md"})
        assert page.status_code == 200
        assert "readonly" in page.text and "message counts" in page.text and "Save changes" not in page.text
        response = await client.post(
            "/memory/save", headers=POST_HEADERS, data={
                "file": "memory/HABITS.md", "version": version("sleep: 23:00-07:00"), "content": "sleep: 19:00-06:00",
            },
        )
        assert response.status_code == 403 and app.home.read("memory/HABITS.md") == "sleep: 23:00-07:00"


@pytest.mark.parametrize("new,creates_proposal", [("A new line", True), ("x" * (MAX_SOUL_EDIT + 1), False)])
async def test_soul_chat_calls_once_and_never_writes_soul(make_app, browser, new, creates_proposal):
    app = make_app(contents=[json.dumps({"old": "An old line", "new": new})])
    app.home._write_regular("SOUL.md", "An old line")
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "Owner only")
    async with browser(app) as client:
        response = await client.post("/memory/soul", headers=POST_HEADERS, data={"message": "Rewrite this line"})
        assert response.status_code == 200
        assert len(app.script.requests) == 1
        request = app.script.requests[0]
        assert "tools" not in request
        payload = json.dumps(request)
        assert "An old line" in payload and "Rewrite this line" in payload and "Owner only" not in payload
        assert app.home.read("SOUL.md") == "An old line"
        proposals = await app.proposals.pending()
        if creates_proposal:
            assert len(proposals) == 1 and proposals[0]["kind"] == "soul_edit"
            assert json.loads(proposals[0]["body_json"]) == {"old": "An old line", "new": new}
            assert "<del>- An old line</del>" in response.text and "<ins>+ A new line</ins>" in response.text
            assert any(event.role == "event" for event in await app.store.get_recent_chat_window(OWNER))
        else:
            assert proposals == [] and "at most" in response.text
