"""The Waiting and System JSON APIs: auth, same-origin, validation and writes through to the store and the registry."""

import json
from datetime import timedelta

import pytest

from test_plugin_surfaces import SURFACES, fake_runner
from test_plugin_tools import draft, status
from test_web_api_memory_chats import JSON
from test_web_pages import browser  # noqa: F401 (fixture)
from tghelpers import GROUP, OWNER

HASH = "a" * 64
WRITES = [
    ("/api/waiting/decide", {"id": 1, "decision": "approve"}),
    ("/api/waiting/chat", {"chat_id": 5, "decision": "approve"}),
    ("/api/waiting/import", {"id": 1, "decision": "all"}),
    ("/api/waiting/delete", {"id": 1, "expected": "x"}),
    ("/api/system/plugins/reviewed", {"name": "convert", "digest": HASH}),
    ("/api/system/plugins/decide", {"name": "convert", "digest": HASH, "decision": "approve"}),
    ("/api/system/plugins/delete", {"name": "convert", "expected": HASH}),
    ("/api/system/plugins/panel", {"name": "convert", "panel_id": "summary"}),
]
READS = ["/api/waiting", "/api/system/plugins", "/api/system/usage", "/api/system/turns"]


@pytest.mark.parametrize(("path", "body"), WRITES)
async def test_every_write_needs_auth_a_same_origin_json_request(make_app, browser, path, body):
    async with browser(make_app()) as client:
        assert (await client.post(path, json=body, headers={"Authorization": "", **JSON})).status_code == 403
        assert (await client.post(path, json=body)).status_code == 403  # no Origin
        assert (await client.post(path, json=body, headers={"Origin": "https://evil.example.org"})).status_code == 403
        assert (await client.post(path, content="{}", headers={"Origin": JSON["Origin"], "Content-Type": "text/plain"})).status_code == 403


@pytest.mark.parametrize("path", READS)
async def test_reads_need_the_dashboard_auth(make_app, browser, path):
    async with browser(make_app()) as client:
        assert (await client.get(path, headers={"Authorization": ""})).status_code == 403
        assert (await client.get(path)).status_code == 200


# --- waiting for you ----------------------------------------------------------------------------------------------


async def test_waiting_lists_every_kind_with_only_what_the_old_page_showed(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "One old line")
    await app.store.note_pending_chat(str(-4009), "Book swap", "Riley")
    soul = await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "<b>A new line</b>"}, "reflection")
    await app.store.create_proposal(
        "cross_chat", "Tell the crew", {"text": "Hi crew", "reason": "Owner asked", "leak_score": None}, "chat", app.dispatch.now().isoformat(),
    )
    await app.proposals.create("lan_address", "Printer moved", {"old": "10.0.0.5", "new": "10.0.0.9", "name": "printer"}, "chat", notify=False)
    await app.proposals.create("memory_fact", "Likes tea", {"file": "USER.md", "text": "Likes tea"}, "import:chatgpt", notify=False)
    async with browser(app) as client:
        data = (await client.get("/api/waiting")).json()
        assert data["count"] == 5 and [chat["id"] for chat in data["chats"]] == [-4009]
        assert data["chats"][0]["label"] == "Group approval" and data["chats"][0]["title"] == "Book swap"
        by_kind = {card["kind"] if card["type"] == "proposal" else "import": card for card in data["cards"]}
        assert by_kind["soul_edit"]["diff"] == ["- One old line", "+ <b>A new line</b>"]  # data, never markup
        assert by_kind["cross_chat"]["leak_score"] is None and by_kind["cross_chat"]["text"] == "Hi crew"
        assert by_kind["lan_address"]["old"] == "10.0.0.5" and by_kind["lan_address"]["address_name"] == "printer"
        assert by_kind["import"]["facts"][0]["text"] == "Likes tea" and by_kind["import"]["service"] == "ChatGPT"
        assert "body" not in by_kind["soul_edit"] and "body_json" not in by_kind["soul_edit"]
        only = (await client.get("/api/waiting", params={"kind": "soul_edit"})).json()
        assert [card["id"] for card in only["cards"]] == [soul] and only["chats"] == []


async def test_deciding_a_proposal_writes_through_and_keeps_the_filter(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "One old line")
    first = await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "New line"}, "reflection")
    await app.proposals.create("lan_address", "Printer", {"old": "10.0.0.1", "new": "10.0.0.2"}, "chat", notify=False)
    async with browser(app) as client:
        for bad in ({"id": 0, "decision": "approve"}, {"id": 2**63, "decision": "approve"}, {"id": 1, "decision": "maybe"}, {"id": 1, "decision": "approve", "x": 1}):
            assert (await client.post("/api/waiting/decide", headers=JSON, json=bad)).status_code == 422
        done = (await client.post("/api/waiting/decide", headers=JSON, json={"id": first, "decision": "approve", "kind": "lan_address"})).json()
        assert done["ok"] is True and done["message"] == "Approved" and done["kind"] == "lan_address"
        assert [card["kind"] for card in done["cards"]] == ["lan_address"]
        assert [row["id"] for row in await app.proposals.pending()] != [first]
        assert app.home.read_memory_file("SOUL.md") == "New line"
        again = (await client.post("/api/waiting/decide", headers=JSON, json={"id": first, "decision": "approve"})).json()
        assert again["ok"] is False and again["message"] == "already handled"


async def test_a_stranger_chat_is_approved_or_ignored_like_the_old_page(make_app, browser):
    app = make_app()
    await app.store.note_pending_chat(str(-4009), "Book swap", "Riley")
    await app.store.note_pending_chat(str(-4010), "Hiking", "Sam")
    async with browser(app) as client:
        approved = (await client.post("/api/waiting/chat", headers=JSON, json={"chat_id": -4009, "decision": "approve"})).json()
        assert approved["ok"] is True and "Book swap" in approved["message"]
        assert [chat["id"] for chat in approved["chats"]] == [-4010]
        assert any(chat["chat_id"] == "-4009" for chat in await app.store.list_approved_chats())
        ignored = (await client.post("/api/waiting/chat", headers=JSON, json={"chat_id": -4010, "decision": "ignore"})).json()
        assert ignored["ok"] is True and ignored["chats"] == []
        gone = (await client.post("/api/waiting/chat", headers=JSON, json={"chat_id": -4010, "decision": "ignore"})).json()
        assert gone["ok"] is False and gone["message"] == "Already handled."
        assert (await client.post("/api/waiting/chat", headers=JSON, json={"chat_id": 1, "decision": "later"})).status_code == 422


async def test_an_import_is_approved_by_selection_or_rejected_whole(make_app, browser):
    app = make_app()
    for text in ("Likes tea", "Walks on Saturdays", "Reads on trains"):
        await app.proposals.create("memory_fact", text, {"file": "USER.md", "text": text}, "import:chatgpt", notify=False)
    async with browser(app) as client:
        card, = (await client.get("/api/waiting")).json()["cards"]
        first, second, third = (fact["id"] for fact in card["facts"])
        part = (await client.post("/api/waiting/import", headers=JSON, json={"id": card["id"], "decision": "selected", "selected": [first]})).json()
        assert part["ok"] is True and part["message"] == "Approved 1." and len(part["cards"][0]["facts"]) == 2
        rejected = (await client.post("/api/waiting/import", headers=JSON, json={"id": part["cards"][0]["id"], "decision": "reject"})).json()
        assert rejected["ok"] is True and rejected["message"] == "Rejected 2." and rejected["cards"] == []
        gone = (await client.post("/api/waiting/import", headers=JSON, json={"id": card["id"], "decision": "all"})).json()
        assert gone["ok"] is False and gone["message"] == "Already handled."
        assert (await client.post("/api/waiting/import", headers=JSON, json={"id": 1, "decision": "some"})).status_code == 422
        assert second != third and not await app.proposals.pending()


async def test_a_settled_proposal_is_deleted_only_with_its_stamp(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "One old line")
    proposal = await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "New"}, "reflection")
    await app.proposals.decide(proposal, False)
    async with browser(app) as client:
        settled, = (await client.get("/api/waiting")).json()["settled"]
        assert settled["status"] == "rejected" and settled["label"].startswith("Soul edit")
        wrong = await client.post("/api/waiting/delete", headers=JSON, json={"id": proposal, "expected": "not the stamp"})
        assert wrong.status_code == 409
        missing = await client.post("/api/waiting/delete", headers=JSON, json={"id": proposal + 50, "expected": settled["created_at"]})
        assert missing.status_code == 404
        done = (await client.post("/api/waiting/delete", headers=JSON, json={"id": proposal, "expected": settled["created_at"]})).json()
        assert done["ok"] is True and done["settled"] == []


# --- plugins ------------------------------------------------------------------------------------------------------


async def test_the_plugin_list_and_review_show_the_files_hashes_notices_and_test_run(make_app, browser, monkeypatch):
    app = make_app()
    monkeypatch.setattr(app.plugin_runner, "trial", lambda *_: {"output": "sample passed", "self_test": True})
    network = SURFACES.replace('version = "1"', 'version = "1"\nnetwork = true')
    await draft(app, manifest=network, source="print('<script>alert(1)</script>')\n")
    row = await status(app)
    async with browser(app) as client:
        listing = (await client.get("/api/system/plugins")).json()
        plugin, = listing["plugins"]
        assert (plugin["name"], plugin["status"], plugin["hash"], plugin["network"], plugin["available"]) == ("convert", "pending", row["hash"], True, True)
        assert plugin["test_run"]["failed"] is False and "sample passed" in plugin["test_run"]["detail"]
        assert "internet" in listing["notices"]["network"] and listing["panels"] == []  # pending panels are not shown
        review = (await client.get(f"/api/system/plugins/convert/{row['hash']}")).json()
        assert review["hash"] == row["hash"] and review["reviewed"] is False and review["network"] is True
        assert [d["path"] for d in review["documents"]] == ["main.py", "plugin.toml"]
        assert review["documents"][0]["text"] == "print('<script>alert(1)</script>')\n"  # text for the page to print
        assert [tool["name"] for tool in review["tools"]] and review["commands"][0]["name"] == "weather"
        assert review["panels"] == [{"id": "summary", "title": "Weekly summary"}]
        assert (await client.get(f"/api/system/plugins/convert/{'b' * 64}")).status_code == 404  # a different digest
        assert (await client.get(f"/api/system/plugins/convert/{row['hash']}x")).status_code == 404
        assert (await client.get(f"/api/system/plugins/Bad!/{row['hash']}")).status_code == 404


async def test_a_plugin_is_approved_only_after_the_read_button_and_its_digest_still_matches(make_app, browser, monkeypatch):
    app = make_app()
    monkeypatch.setattr(app.plugin_runner, "trial", lambda *_: {"output": "ok", "self_test": True})
    await draft(app, manifest=SURFACES)
    row = await status(app)
    body = {"name": "convert", "digest": row["hash"]}
    async with browser(app) as client:
        assert (await client.get(f"/api/system/plugins/convert/{row['hash']}")).json()["reviewed"] is False
        early = await client.post("/api/system/plugins/decide", headers=JSON, json={**body, "decision": "approve"})
        assert early.status_code == 409 and "review" in early.json()["detail"]
        assert (await app.store.list_plugins())[0]["status"] == "pending"
        stale = await client.post("/api/system/plugins/reviewed", headers=JSON, json={**body, "digest": "c" * 64})
        assert stale.status_code == 404
        read = (await client.post("/api/system/plugins/reviewed", headers=JSON, json=body)).json()
        assert read["ok"] is True and read["review"]["reviewed"] is True
        wrong = await client.post("/api/system/plugins/decide", headers=JSON, json={**body, "digest": "c" * 64, "decision": "approve"})
        assert wrong.status_code == 409 and "changed" in wrong.json()["detail"]
        approved = (await client.post("/api/system/plugins/decide", headers=JSON, json={**body, "decision": "approve"})).json()
        assert approved["ok"] is True and approved["message"] == "Approved" and approved["plugins"][0]["status"] == "approved"
        assert approved["panels"] == [{"plugin": "convert", "id": "summary", "title": "Weekly summary"}]
        for bad in ({**body, "decision": "retire"}, {**body, "decision": "approve", "extra": 1}, {"name": "convert"}):
            assert (await client.post("/api/system/plugins/decide", headers=JSON, json=bad)).status_code == 422
        retired = (await client.post("/api/system/plugins/decide", headers=JSON, json={**body, "decision": "disable"})).json()
        assert retired["message"] == "Disabled" and retired["plugins"][0]["status"] == "disabled" and retired["panels"] == []


async def test_a_plugin_is_deleted_only_with_its_hash(make_app, browser, monkeypatch):
    app = make_app()
    monkeypatch.setattr(app.plugin_runner, "trial", lambda *_: {"output": "ok", "self_test": True})
    await draft(app, manifest=SURFACES)
    row = await status(app)
    async with browser(app) as client:
        assert (await client.post("/api/system/plugins/delete", headers=JSON, json={"name": "nothere", "expected": row["hash"]})).status_code == 404
        assert (await client.post("/api/system/plugins/delete", headers=JSON, json={"name": "Bad!", "expected": row["hash"]})).status_code == 404
        stale = await client.post("/api/system/plugins/delete", headers=JSON, json={"name": "convert", "expected": "c" * 64})
        assert stale.status_code == 409
        assert await app.store.list_plugins()
        done = (await client.post("/api/system/plugins/delete", headers=JSON, json={"name": "convert", "expected": row["hash"]})).json()
        assert done["ok"] is True and done["message"] == "Deleted" and done["plugins"] == []
        assert not await app.store.list_plugins()


async def test_a_panel_runs_only_when_approved_and_comes_back_as_data(make_app, browser, monkeypatch):
    app = make_app()
    monkeypatch.setattr(app.plugin_runner, "trial", lambda *_: {"output": "ok", "self_test": True})
    await draft(app, manifest=SURFACES)
    row = await status(app)
    body = {"name": "convert", "digest": row["hash"]}
    panel = {"title": "Week <b>1</b>", "paragraphs": ["All <i>quiet</i>"], "items": [["Rain", "2 mm"]], "table": {"columns": ["Day"], "rows": [["Mon"]]}}
    calls = fake_runner(app, monkeypatch, panel)
    monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)
    async with browser(app) as client:
        assert (await client.post("/api/system/plugins/panel", headers=JSON, json={"name": "convert", "panel_id": "summary"})).status_code == 404
        assert calls == []
        await client.post("/api/system/plugins/reviewed", headers=JSON, json=body)
        await client.post("/api/system/plugins/decide", headers=JSON, json={**body, "decision": "approve"})
        shown = (await client.post("/api/system/plugins/panel", headers=JSON, json={"name": "convert", "panel_id": "summary"})).json()
        assert shown["error"] is None and shown["title"] == "Week <b>1</b>" and shown["paragraphs"] == ["All <i>quiet</i>"]
        assert shown["pairs"] == [["Rain", "2 mm"]] and shown["columns"] == ["Day"] and shown["rows"] == [["Mon"]]
        assert calls == [{"panel": "summary"}]
        assert (await client.post("/api/system/plugins/panel", headers=JSON, json={"name": "convert", "panel_id": "nope"})).status_code == 404
        fake_runner(app, monkeypatch, {"html": "<b>x</b>"})  # anything off-schema is refused, not cleaned
        refused = (await client.post("/api/system/plugins/panel", headers=JSON, json={"name": "convert", "panel_id": "summary"})).json()
        assert refused["error"] and refused["paragraphs"] == [] and refused["title"] == "Weekly summary"


# --- usage and turns ----------------------------------------------------------------------------------------------


async def test_usage_reports_spend_by_kind_and_model(make_app, browser):
    app = make_app()
    now = app.dispatch.now()
    await app.store.record_usage(now.isoformat(), "chat", "example-model", "example.org", GROUP, 2, 3, 0.8, 1)
    await app.store.record_usage((now - timedelta(days=2)).isoformat(), "search", "search-model", "example.org", GROUP, 2, 3, 0.1, 0)
    async with browser(app) as client:
        data = (await client.get("/api/system/usage")).json()
        assert data["spent"] == pytest.approx(0.8) and data["cap"] == app.config.daily_spend_cap_usd and data["fallbacks"] == 1
        assert data["today_kinds"] == [{"name": "chat", "calls": 1, "spend": pytest.approx(0.8)}]
        assert {row["name"] for row in data["month_models"]} == {"example-model", "search-model"}
        assert len(data["bars"]) == 30 and data["bars"][-1]["fallbacks"] == 1 and data["bars"][-1]["parts"][0]["kind"] == "chat"
        assert data["kinds"]["chat"] == "Replies"


async def test_the_turn_log_filters_by_chat_and_keeps_to_what_the_old_page_showed(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    now = app.dispatch.now()
    await app.store.record_turn(
        now, GROUP, "group", 1, ["web_search", "model@1200", "typing@4200"], "example-model", True, 5000, "Replied in 1 bubble",
    )
    await app.store.record_turn(now, OWNER, "owner_dm", 0, [], "", False, 0, "Stayed quiet")
    async with browser(app) as client:
        everything = (await client.get("/api/system/turns")).json()
        assert [turn["outcome"] for turn in everything["turns"]] == ["Replied in 1 bubble", "Stayed quiet"] or {turn["outcome"] for turn in everything["turns"]} == {"Replied in 1 bubble", "Stayed quiet"}
        assert {choice["id"] for choice in everything["choices"]} >= {str(GROUP), str(OWNER)}
        shown = (await client.get("/api/system/turns", params={"chat": str(GROUP)})).json()
        turn, = shown["turns"]
        assert turn["title"] == "Book Club" and turn["fallback"] is True and turn["steps"] == ["web_search"]
        assert {"label": "Model", "ms": 1200} in turn["timing"] and {"label": "Typing", "ms": 4200} in turn["timing"]
        assert [segment["stage"] for segment in turn["segments"]] == ["model", "typing"] and shown["chat"] == str(GROUP)
        assert "trace" not in turn and "model_calls" in turn
