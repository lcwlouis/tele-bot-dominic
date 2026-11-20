import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from dom.home import MAX_SOUL, MAX_SOUL_EDIT, limit_for
from dom.memory_import import MAX_DOCUMENT_BYTES

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


async def test_memory_import_upload_uses_shared_pipeline_and_limits(make_app, browser, monkeypatch):
    app = make_app()
    private = app.home.scoped(allow_private=True)
    private._write_regular("private/USER.md", "- Prefers tea\n")
    run_import = AsyncMock(wraps=app.memory_import.run)
    monkeypatch.setattr(app.memory_import, "run", run_import)

    async with browser(app) as client:
        page = await client.get("/memory")
        assert 'action="/memory/import"' in page.text
        assert 'hx-encoding="multipart/form-data"' in page.text
        assert all(label in page.text for label in ("ChatGPT", "Claude", "Muse"))

        # CSRF / htmx and Content-Type checks
        assert (await client.post(
            "/memory/import", data={"service": "chatgpt"},
            files={"file": ("export.md", b"- Likes coffee\n", "text/markdown")},
        )).status_code in (401, 403)
        assert (await client.post(
            "/memory/import", headers=POST_HEADERS, data={"service": "chatgpt"},
        )).status_code == 415

        # Valid UTF-8 BOM .md upload runs MemoryImport.run and records an owner event
        payload = b"\xef\xbb\xbf- Prefers tea\n- [home] Owns a kettle\n- Likes book clubs\n"
        response = await client.post(
            "/memory/import", headers=POST_HEADERS, data={"service": "claude"},
            files={"file": ("export.md", payload, "text/markdown")},
        )
        assert response.status_code == 200
        run_import.assert_awaited_once_with("claude", "- Prefers tea\n- [home] Owns a kettle\n- Likes book clubs\n")
        assert "Imported 2 facts from claude. Skipped 1 duplicate." in response.text
        assert '/proposals?kind=memory_fact' in response.text
        pending = await app.proposals.pending()
        assert [(row["source"], json.loads(row["body_json"])["text"]) for row in pending] == [
            ("import:claude", "Owns a kettle"),
            ("import:claude", "Likes book clubs"),
        ]
        events = await app.store.get_recent_chat_window(OWNER)
        assert any(m.role == "event" and "Imported 2 facts from claude" in m.text for m in events)

        # Rejects wrong extension, non-UTF-8, unsupported service, and >1 MB file
        for bad_service, name, raw, expected in (
            ("siri", "export.md", b"- Fact\n", "ChatGPT, Claude or Muse"),
            ("muse", "export.json", b"- Fact\n", ".txt or .md"),
            ("muse", "export.txt", b"\xff\xfe- Fact\n", "UTF-8"),
            ("muse", "export.txt", b"x" * (MAX_DOCUMENT_BYTES + 1), "1 MB"),
            ("muse", "export.txt", b"x" * (MAX_DOCUMENT_BYTES * 2), "1 MB"),  # refused from its declared length
        ):
            bad = await client.post(
                "/memory/import", headers=POST_HEADERS, data={"service": bad_service},
                files={"file": (name, raw, "application/octet-stream")},
            )
            assert bad.status_code == 200 and expected in bad.text
        assert len(await app.proposals.pending()) == 2


async def test_memory_import_stops_reading_an_oversized_upload(make_app, browser):
    app = make_app()
    boundary = "domtestboundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="service"\r\n\r\nchatgpt\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    chunk, sent = b"a" * 65536, 0

    async def body():  # no Content-Length: the cap must hold while the body streams in
        nonlocal sent
        yield head
        for _ in range(40):  # about 2.6 MB
            sent += 1
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    async with browser(app) as client:
        response = await client.post(
            "/memory/import", headers=POST_HEADERS | {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            content=body(),
        )
    assert response.status_code == 200 and "1 MB or smaller" in response.text
    assert sent <= 17 and await app.proposals.pending() == []
