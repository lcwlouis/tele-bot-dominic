"""The Memory and Chats JSON APIs: auth, same-origin, validation and writes through to the store and the home."""

import base64
import hashlib
import json

import pytest

from dom import traits
from dom.home import MAX_SOUL
from dom.memory_import import MAX_DOCUMENT_BYTES

from test_stickers import SET, sticker_bot
from test_web_auth import ORIGIN
from test_web_pages import browser  # noqa: F401 (fixture)
from tghelpers import FRIEND, GROUP, OWNER

JSON = {"Origin": ORIGIN, "Content-Type": "application/json"}


def version(content):
    return hashlib.sha256(content.encode()).hexdigest()


def group(data):
    return next(chat for chat in data["chats"] if chat["id"] == GROUP)


def encoded(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


WRITES = [
    ("/api/memory/save", {"file": "SOUL.md", "version": "", "content": "x"}),
    ("/api/memory/soul", {"message": "Be kinder"}),
    ("/api/memory/import", {"service": "claude", "filename": "a.md", "data": ""}),
    ("/api/memory/import-review", {"id": 1, "decision": "all"}),
    ("/api/memory/proposal", {"id": 1, "decision": "approve"}),
    (f"/api/chats/action", {"chat_id": GROUP, "action": "sleep"}),
    ("/api/chats/preset", {"chat_id": "all", "preset": ""}),
    ("/api/chats/traits", {"chat_id": GROUP, "traits": {"humor": 0.7}}),
    ("/api/chats/traits/reset", {"chat_id": GROUP}),
    ("/api/stickers", {"action": "add", "name": SET}),
    ("/api/stickers/refresh", {}),
]


@pytest.mark.parametrize(("path", "body"), WRITES)
async def test_every_write_needs_auth_a_same_origin_json_request(make_app, browser, path, body):
    app = make_app()
    async with browser(app) as client:
        assert (await client.post(path, json=body, headers={"Authorization": "", **JSON})).status_code == 403
        assert (await client.post(path, json=body)).status_code == 403  # no Origin
        assert (await client.post(path, json=body, headers={"Origin": "https://evil.example.org"})).status_code == 403
        assert (await client.post(path, content=json.dumps(body), headers={"Origin": ORIGIN})).status_code in (403, 415)
        assert (await client.post(path, content="{}", headers={"Origin": ORIGIN, "Content-Type": "text/plain"})).status_code == 403


@pytest.mark.parametrize("path", ["/api/memory", "/api/memory/file?file=SOUL.md", "/api/chats"])
async def test_reads_need_the_dashboard_auth(make_app, browser, path):
    async with browser(make_app()) as client:
        assert (await client.get(path, headers={"Authorization": ""})).status_code == 403
        assert (await client.get(path)).status_code == 200


async def test_memory_lists_files_and_keeps_private_ones_in_their_own_section(make_app, browser):
    app = make_app()
    private = app.home.scoped(allow_private=True)
    private._write_regular("private/USER.md", "Owner only")
    app.home._write_regular(f"memory/chats/{FRIEND}.md", "Friend note")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Reader")
    (app.home.root / "memory" / "chats" / "alias.md").symlink_to(app.home.root / "private" / "USER.md")
    async with browser(app) as client:
        index = (await client.get("/api/memory")).json()
        sections = {section["heading"]: [(f["path"], f["label"]) for f in section["files"]] for section in index["sections"]}
        assert ("memory/MEMORY.md", "MEMORY.md") in sections["Shared memory"]
        assert any("Reader" in label for _, label in sections["Chats"])
        assert ("private/USER.md", "USER.md") in sections["Only in your DM"]
        assert all(not path.startswith("private/") for heading, files in sections.items() if heading != "Only in your DM" for path, _ in files)
        assert "Owner only" not in json.dumps(index)
        assert [service["id"] for service in index["services"]] == ["chatgpt", "claude", "muse"]

        file = (await client.get("/api/memory/file", params={"file": "private/USER.md"})).json()
        assert file["content"] == "Owner only" and file["version"] == version("Owner only") and not file["readonly"]
        assert (await client.get("/api/memory/file", params={"file": "memory/HABITS.md"})).json()["readonly"] is True
        for bad in ("memory/chats/alias.md", "memory/../private/USER.md", "config.toml"):
            assert (await client.get("/api/memory/file", params={"file": bad})).status_code == 404
            response = await client.post("/api/memory/save", headers=JSON, json={"file": bad, "version": "", "content": "x"})
            assert response.status_code == 404
        assert private.read("private/USER.md") == "Owner only"


async def test_save_checks_limits_versions_and_habits(make_app, browser):
    app = make_app()
    memory = "memory/MEMORY.md"
    async with browser(app) as client:
        save = lambda **body: client.post("/api/memory/save", headers=JSON, json=body)  # noqa: E731
        over = await save(file="SOUL.md", version=version(""), content="x" * (MAX_SOUL + 1))
        assert over.status_code == 422 and "over limit" in over.json()["detail"] and app.home.read("SOUL.md") == ""
        ok = await save(file=memory, version=version(""), content="- Likes tea\r\n")
        assert ok.status_code == 200 and ok.json()["message"] == "Saved MEMORY.md."
        assert app.home.read(memory) == "- Likes tea\n" and ok.json()["version"] == version("- Likes tea\n")
        stale = await save(file=memory, version=version(""), content="late edit")
        assert stale.status_code == 409 and "changed since" in stale.json()["detail"]
        assert (await save(file="memory/HABITS.md", version="", content="x")).status_code == 403
        assert (await client.post("/api/memory/save", headers=JSON, json={"file": memory})).status_code == 422
        assert (await client.post("/api/memory/save", headers=JSON, json={"file": memory, "version": "", "content": "", "x": 1})).status_code == 422
        events = await app.store.get_recent_chat_window(OWNER)
        assert [e.text for e in events if e.role == "event"] == ["Saved memory/MEMORY.md in the dashboard"]
        private = await save(file="private/USER.md", version=version(""), content="Owner note")
        assert private.status_code == 200 and app.home.scoped(allow_private=True).read("private/USER.md") == "Owner note"


@pytest.mark.parametrize("new,files", [("A new line", True), ("x" * 5000, False)])
async def test_soul_request_files_a_proposal_and_never_writes_soul(make_app, browser, new, files):
    app = make_app(contents=[json.dumps({"old": "An old line", "new": new})])
    app.home._write_regular("SOUL.md", "An old line")
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "Owner only")
    async with browser(app) as client:
        response = await client.post("/api/memory/soul", headers=JSON, json={"message": "Rewrite this line"})
        assert len(app.script.requests) == 1
        payload = json.dumps(app.script.requests[0])
        assert "Rewrite this line" in payload and "Owner only" not in payload
        assert app.home.read("SOUL.md") == "An old line"
        pending = await app.proposals.pending()
        if files:
            assert response.status_code == 200 and response.json()["diff"] == ["- An old line", "+ A new line"]
            assert [row["kind"] for row in pending] == ["soul_edit"]
            waiting = (await client.get("/api/memory")).json()["edits"]
            assert waiting[0]["diff"] == ["- An old line", "+ A new line"] and waiting[0]["id"] == response.json()["id"]
        else:
            assert response.status_code == 422 and pending == []
        for bad in ("", "  ", "x" * 2001):
            assert (await client.post("/api/memory/soul", headers=JSON, json={"message": bad})).status_code == 422
        assert len(app.script.requests) == 1


async def test_deciding_a_soul_edit_goes_through_the_proposals_store(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "An old line")
    edit = await app.proposals.create("soul_edit", "Tea", {"old": "An old line", "new": "Tea"}, "reflection", notify=False)
    async with browser(app) as client:
        assert (await client.post("/api/memory/proposal", headers=JSON, json={"id": 999, "decision": "approve"})).status_code == 404
        response = await client.post("/api/memory/proposal", headers=JSON, json={"id": edit, "decision": "approve"})
        assert response.status_code == 200 and response.json()["message"] == "Approved" and response.json()["edits"] == []
        assert app.home.read("SOUL.md") == "Tea"
        assert (await client.post("/api/memory/proposal", headers=JSON, json={"id": edit, "decision": "reject"})).status_code == 404


async def test_import_uses_the_shared_pipeline_and_limits_then_review_decides_in_batches(make_app, browser):
    app = make_app()
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "- Prefers tea\n")
    async with browser(app) as client:
        send = lambda service, name, raw: client.post(  # noqa: E731
            "/api/memory/import", headers=JSON, json={"service": service, "filename": name, "data": encoded(raw)},
        )
        response = await send("claude", "export.md", b"\xef\xbb\xbf- Prefers tea\n- [home] Owns a kettle\n- Likes book clubs\n")
        assert response.status_code == 200
        body = response.json()
        assert body["message"].startswith("Imported 2 facts from claude. Skipped 1 duplicate.") and body["count"] == 2
        assert [fact["text"] for fact in body["imports"][0]["facts"]] == ["Owns a kettle", "Likes book clubs"]
        assert body["imports"][0]["service"] == "Claude"
        events = await app.store.get_recent_chat_window(OWNER)
        assert any(e.role == "event" and "Imported 2 facts from claude" in e.text for e in events)
        for service, name, raw, expected in (
            ("siri", "export.md", b"- Fact\n", "ChatGPT, Claude or Muse"),
            ("muse", "export.json", b"- Fact\n", ".txt or .md"),
            ("muse", "export.txt", b"\xff\xfe- Fact\n", "UTF-8"),
            ("muse", "export.txt", b"x" * (MAX_DOCUMENT_BYTES + 1), "1 MB"),
        ):
            bad = await send(service, name, raw)
            assert bad.status_code == 422 and expected in bad.json()["detail"]
        assert (await client.post("/api/memory/import", headers=JSON, json={"service": "claude", "filename": "a.md", "data": "%%"})).status_code == 422
        too_big = await client.post(
            "/api/memory/import", headers=JSON, json={"service": "muse", "filename": "a.txt", "data": encoded(b"x" * (MAX_DOCUMENT_BYTES * 2))},
        )
        assert too_big.status_code == 413 or too_big.status_code == 422
        pending = await app.proposals.pending()
        assert len(pending) == 2

        first = body["imports"][0]
        chosen = first["facts"][0]["id"]
        part = await client.post("/api/memory/import-review", headers=JSON, json={"id": first["id"], "decision": "selected", "selected": [chosen]})
        assert part.json()["message"] == "Approved 1." and part.json()["settled"] is False
        assert [fact["text"] for fact in part.json()["imports"][0]["facts"]] == ["Likes book clubs"]
        assert "Owns a kettle" in app.home.scoped(allow_private=True).read("private/USER.md")
        rest = await client.post("/api/memory/import-review", headers=JSON, json={"id": part.json()["imports"][0]["id"], "decision": "reject"})
        assert rest.json()["message"] == "Rejected 1." and rest.json()["settled"] is True and rest.json()["imports"] == []
        again = await client.post("/api/memory/import-review", headers=JSON, json={"id": first["id"], "decision": "all"})
        assert again.json()["message"] == "Already handled."
        assert (await client.post("/api/memory/import-review", headers=JSON, json={"id": 1, "decision": "bogus"})).status_code == 422


async def test_chats_list_has_state_presets_and_no_message_text(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "---\ntraits:\n  humor: 0.5\n---\n")
    app.home.write_preset("classic", "---\ntitle: Classic\ndescription: Easy and warm.\ntraits:\n  humor: 0.8\n---\n")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "pending_approval", title="Stranger")
    await app.store.set_chat_preset(GROUP, "classic")
    async with browser(app) as client:
        data = (await client.get("/api/chats")).json()
    assert data["pending"] == 1 and [chat["title"] for chat in data["chats"]] == ["Your DM", "Book Club"]
    chat = group(data)
    assert chat["id"] == GROUP and chat["scope"] == "group" and chat["own_preset"] == "classic"
    assert chat["preset"] == {"name": "classic", "title": "Classic"} and chat["task"] is None
    assert [slider["name"] for slider in chat["sliders"]] == list(traits.NAMES)
    assert next(s for s in chat["sliders"] if s["name"] == "humor") == {"name": "humor", "value": 0.8, "custom": False}
    assert data["presets"] == [{"name": "classic", "title": "Classic", "description": "Easy and warm."}]
    assert set(chat) == {"id", "title", "scope", "state", "until", "unread", "preset", "own_preset", "task", "sliders"}


async def test_chat_actions_run_the_owner_functions(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    async with browser(app) as client:
        act = lambda chat_id, action: client.post("/api/chats/action", headers=JSON, json={"chat_id": chat_id, "action": action})  # noqa: E731
        woke = await act(GROUP, "wake")
        assert woke.status_code == 200 and woke.json()["message"] == "Dom is up in Book Club"
        assert group(woke.json())["state"] == "online"
        slept = await act(GROUP, "sleep")
        assert slept.json()["message"] == "Dom is asleep in Book Club" and group(slept.json())["state"] == "asleep"
        assert (await act(-999999, "wake")).status_code == 404
        assert (await act(OWNER, "leave")).status_code == 409  # only groups can be left
        assert (await client.post("/api/chats/action", headers=JSON, json={"chat_id": GROUP, "action": "preset"})).status_code == 422
        left = await act(GROUP, "leave")
        assert left.status_code == 200 and left.json()["message"] == "Dom left Book Club" and all(chat["id"] != GROUP for chat in left.json()["chats"])
        assert app.bot.of("leave", GROUP)
    events = [e.text for e in await app.store.get_recent_chat_window(OWNER) if e.role == "event"]
    assert any("woke Dom in Book Club" in text for text in events) and any("tapped Leave" in text for text in events)


async def test_presets_everywhere_and_per_chat(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    app.home.write_preset("classic", "---\ntitle: Classic\n---\n")
    async with browser(app) as client:
        put = lambda chat_id, preset: client.post("/api/chats/preset", headers=JSON, json={"chat_id": chat_id, "preset": preset})  # noqa: E731
        everywhere = await put("all", "classic")
        assert everywhere.status_code == 200 and "everywhere to Classic" in everywhere.json()["message"]
        assert everywhere.json()["everywhere"] == "classic" and await app.store.kv_get("preset:global") == "classic"
        assert (await put("all", "nope")).status_code == 422 and await app.store.kv_get("preset:global") == "classic"
        own = await put(GROUP, "classic")
        assert group(own.json())["own_preset"] == "classic"
        assert (await app.store.list_chats("approved"))[0]["preset"] == "classic"
        assert group((await put(GROUP, "")).json())["own_preset"] == ""
        assert (await put(GROUP, "nope")).status_code == 422
        assert (await put(FRIEND, "classic")).status_code == 422  # not an approved chat
        assert (await put("all", "")).json()["everywhere"] == ""


async def test_sliders_validate_save_and_reset(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "---\ntraits:\n  humor: 0.5\n  empathy: 0.5\n---\n")
    app.home.write_preset("calm", "---\ntitle: Calm\ntraits:\n  humor: 0.3\n---\n")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "pending_approval", title="Stranger")
    async with browser(app) as client:
        put = lambda chat_id, values: client.post("/api/chats/traits", headers=JSON, json={"chat_id": chat_id, "traits": values})  # noqa: E731
        response = await put(GROUP, {"humor": 1.8, "sarcasm": -1})
        assert response.status_code == 200 and response.json()["message"].startswith("Traits for Book Club:")
        sliders = {s["name"]: s for s in group(response.json())["sliders"]}
        assert sliders["humor"] == {"name": "humor", "value": 0.95, "custom": True} and sliders["sarcasm"]["value"] == 0.05
        assert sliders["empathy"]["custom"] is False
        await put(GROUP, {"emoji": 0.8})
        await client.post("/api/chats/preset", headers=JSON, json={"chat_id": GROUP, "preset": "calm"})
        sliders, _ = await traits.for_chat(app.home, app.store, GROUP)
        assert sliders["humor"] == 0.95 and sliders["emoji"] == 0.8 and sliders["sarcasm"] == 0.05
        before = await app.store.get_chat_personality(GROUP)
        for bad in ({"unknown": 0.5}, {"humor": 0.7, "empathy": "bad"}, {}):
            assert (await put(GROUP, bad)).status_code == 422
        for other in (FRIEND, -999999):
            assert (await put(other, {"humor": 0.7})).status_code == 422
        assert await app.store.get_chat_personality(GROUP) == before
        reset = await client.post("/api/chats/traits/reset", headers=JSON, json={"chat_id": GROUP})
        assert reset.status_code == 200 and "reset to default" in reset.json()["message"]
        assert await app.store.get_chat_personality(GROUP) == ("calm", "{}")
        assert (await client.post("/api/chats/traits/reset", headers=JSON, json={"chat_id": FRIEND})).status_code == 422
    events = [e.text for e in await app.store.get_recent_chat_window(OWNER) if e.role == "event"]
    assert any("Traits for Book Club" in text for text in events)


async def test_sticker_sets_add_remove_and_refresh(make_app, browser):
    app = make_app()
    sticker_bot(app, {SET: [("📚", "file-a")]})
    async with browser(app) as client:
        send = lambda action, name: client.post("/api/stickers", headers=JSON, json={"action": action, "name": name})  # noqa: E731
        added = await send("add", "https://t" + ".me/addstickers/" + SET)
        assert added.json()["stickers"] == [SET] and added.json()["changed"] is True
        assert (await send("add", SET)).json()["changed"] is False
        assert (await send("add", "not a set!")).json()["changed"] is False
        assert (await client.post("/api/stickers", headers=JSON, json={"action": "drop", "name": SET})).status_code == 422
        assert (await client.post("/api/stickers", headers=JSON, json={"action": "add", "name": ""})).status_code == 422
        refreshed = await client.post("/api/stickers/refresh", headers=JSON, json={})
        assert refreshed.status_code == 200 and "unavailable" in refreshed.json()["message"]
        removed = await send("remove", SET)
        assert removed.json()["stickers"] == [] and removed.json()["changed"] is True


async def test_every_chats_and_memory_write_is_in_json_posts(make_app, browser):
    from dom.web import api_chats, api_memory

    declared = {route.path for module in (api_chats, api_memory) for route in module.router.routes if "POST" in route.methods}
    assert declared == api_chats.JSON_POSTS | api_memory.JSON_POSTS
