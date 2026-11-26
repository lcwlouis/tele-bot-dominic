"""Memory export and the global search: the zip's contents, the search over every kind of item, the owner-DM tool and
its limits elsewhere, the dashboard routes and the /export command."""

import io
import json
import os
from pathlib import Path
import zipfile

import pytest

from dom import global_search, memory_export
from dom.models import StoredMessage
from dom.tools import SEARCH_TOOL, TurnCtx, enabled_tools
from tghelpers import FRIEND, GROUP, OWNER, tg

WORD = "zephyrword"


def names(data: bytes) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(data)).namelist())


def seed_home(root):
    for rel, text in {
        "SOUL.md": "soul", "memory/MEMORY.md": "m", "memory/HABITS.md": "h", "memory/JOURNAL.md": "j",
        "memory/people/222.md": "p", "memory/chats/-1009.md": "c", "private/USER.md": "u", "private/NOTES.md": "n",
        "skills/tea/SKILL.md": "---\nname: tea\ndescription: brew\n---\nsteep", "presets/classic.md": "preset",
        ".env": "SECRET=1", "state.db": "db", "state.db-wal": "wal", "backups/dom-1.tar.gz": "tar",
        "memory/MEMORY.md.bak": "bak", "transcripts/1.jsonl": "t",
    }.items():
        path = root / rel
        if path.exists():  # a live app's own state.db stays
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_export_holds_every_memory_file_and_nothing_secret(tmp_path):
    seed_home(tmp_path)
    (tmp_path / "memory" / "link.md").symlink_to(tmp_path / ".env")
    found = names(memory_export.build_export(tmp_path))
    assert found == {
        "SOUL.md", "memory/MEMORY.md", "memory/HABITS.md", "memory/JOURNAL.md", "memory/people/222.md",
        "memory/chats/-1009.md", "private/USER.md", "private/NOTES.md", "skills/tea/SKILL.md", "presets/classic.md",
    }


@pytest.mark.parametrize("folder", ["memory", "skills", "private"])
def test_export_leaves_secrets_databases_backups_and_links_out_wherever_they_sit(tmp_path, folder):
    seed_home(tmp_path)
    base = tmp_path / folder
    planted = [
        ".env", ".env.local", "state.db", "x.db-wal", "x.db-shm", "keep.md.bak", "keep.md.bak.1", "old.orig",
        "backups/dom.tar.gz", "sub/backups/inner.md", "sub/deep/.env", "sub/state.db",
        "state.db.copy", "snapshots/dom-old.tar.gz", "sub/notes.sqlite", "sub/key.pem", "sub/.hidden.md",
    ]
    for rel in planted:
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text("secret")
    (base / "sub" / "link.md").symlink_to(tmp_path / "SOUL.md")
    (base / "sub" / "linkdir").symlink_to(tmp_path / "private", target_is_directory=True)
    (base / "sub" / "keep.md").write_text("fine")
    os.mkfifo(base / "sub" / "pipe.md")
    found = names(memory_export.build_export(tmp_path))
    assert f"{folder}/sub/keep.md" in found
    assert not any(any(part in name for part in (".env", ".db", ".bak", ".orig", "backups", "link", "pipe")) for name in found
                   if name.startswith(folder + "/"))
    # Only text notes leave: anything else under the folder, however it is named, stays behind.
    assert all(name.endswith((".md", ".txt")) for name in found)
    assert not any(part in name for name in found for part in ("snapshots", "sqlite", ".pem", ".hidden"))


def test_export_reads_at_most_the_budget_left_plus_one_byte(tmp_path, monkeypatch):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "big.md").write_bytes(b"x" * 1000)
    sizes = []
    real = Path.open

    def spy(self, *args, **kwargs):
        handle = real(self, *args, **kwargs)
        if self.name == "big.md":
            read = handle.read
            handle.read = lambda n=-1: sizes.append(n) or read(n)
        return handle

    monkeypatch.setattr(Path, "open", spy)
    with pytest.raises(memory_export.ExportTooLarge):
        memory_export.build_export(tmp_path, max_bytes=10)
    assert sizes == [11]


def test_export_stops_at_the_size_cap(tmp_path):
    seed_home(tmp_path)
    with pytest.raises(memory_export.ExportTooLarge):
        memory_export.build_export(tmp_path, max_bytes=5)


async def fill(app):
    owner_id = app.owner_id
    app.home._write_regular("memory/MEMORY.md", f"Dom likes {WORD} tea")
    private = app.home.scoped(allow_private=True)
    private._write_regular("private/USER.md", f"owner private {WORD}")
    app.home._write_regular("briefs/2026-11-01-a-brief.md", f"# A brief\n\n## Problem\n\n{WORD} problem\n")
    app.home._write_regular("skills/tea/SKILL.md", f"---\nname: tea\ndescription: brew\n---\nsteep with {WORD}")
    for chat_id, text in ((OWNER, f"owner said {WORD}"), (FRIEND, f"friend said {WORD}\nsecond line that is private"), (GROUP, f"group said {WORD}")):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(chat_id), msg_id=1, text=text, visibility_scope=app.store.visibility_for(chat_id),
            created_at="2026-11-18T10:00:00+00:00"))
    await app.store.create_reminder(owner_id, f"call about {WORD}", "2030-01-01T00:00:00+00:00", "normal", "", "2026-11-18T10:00:00+00:00", 50)
    await app.store.enqueue_desktop_task(owner_id, f"open the {WORD} page")
    app.store._conn.execute(
        "INSERT INTO plugins (name, status, hash, manifest_json, updated_at) VALUES ('tea', 'approved', 'h', ?, 'now')",
        (json.dumps({"description": f"brews {WORD}"}),),
    )


async def test_search_finds_a_word_in_each_kind_and_previews_other_chats(make_app):
    app = make_app()
    await fill(app)
    found = await global_search.search_all(app.store, app.home, WORD)
    for kind in global_search.KINDS:
        assert found[kind], kind
    assert {hit.ref for hit in found["memory"]} == {"memory/MEMORY.md", "private/USER.md"}
    chats = {hit.ref: hit for hit in found["chat"]}
    assert chats[str(OWNER)].full and WORD in chats[str(OWNER)].text
    for other in (FRIEND, GROUP):
        assert not chats[str(other)].full and "\n" not in chats[str(other)].text
    assert len(chats[str(FRIEND)].text) <= global_search.PREVIEW


async def test_keyword_search_merges_graph_hits_without_dropping_exact_hits(make_app):
    app = make_app()
    await fill(app)
    app.home._write_regular("memory/projects/release.md", f"- Project uses {WORD} {{any}}")
    await app.knowledge.refresh()

    found = await global_search.search_all(
        app.store, app.home, WORD, mode="keyword", knowledge=app.knowledge,
    )

    assert {hit.ref for hit in found["chat"]} == {str(OWNER), str(FRIEND), str(GROUP)}
    assert {hit.ref for hit in found["memory"]} == {
        "memory/MEMORY.md", "memory/projects/release.md", "private/USER.md",
    }


async def test_a_blank_query_finds_nothing(make_app):
    app = make_app()
    await fill(app)
    assert not any((await global_search.search_all(app.store, app.home, "   ")).values())


def owner_ctx(app, scope="owner_dm", chat_id=OWNER, sender=OWNER):
    return TurnCtx(scope=scope, chat_id=chat_id, sender_id=sender, home=app.home, store=app.store, owner_id=OWNER)


async def test_the_search_tool_covers_every_kind_in_the_owner_dm(make_app):
    app = make_app()
    await fill(app)
    hits = json.loads(await SEARCH_TOOL.handler({"query": WORD}, owner_ctx(app)))
    assert {hit["kind"] for hit in hits} == set(global_search.KINDS)


@pytest.mark.parametrize(("scope", "chat_id", "sender"), [("group", GROUP, FRIEND), ("friend_dm", FRIEND, FRIEND), ("group", GROUP, OWNER)])
async def test_elsewhere_search_is_this_chats_history_only(make_app, scope, chat_id, sender):
    """Memory files, other chats and private notes never surface outside the owner DM, even for the owner in a group."""
    app = make_app()
    await fill(app)
    ctx = owner_ctx(app, scope, chat_id, sender)
    hits = json.loads(await SEARCH_TOOL.handler({"query": WORD}, ctx))
    assert [hit["chat_id"] for hit in hits] == [str(chat_id)] and all("kind" not in hit for hit in hits)
    text = json.dumps(hits)
    assert "owner said" not in text and "Dom likes" not in text and "owner private" not in text
    for query in ("owner private", "Dom likes", "brew", "call about", "open the"):
        assert await SEARCH_TOOL.handler({"query": query}, ctx) == "[]"


async def test_one_search_tool_is_offered_in_every_scope_with_the_same_schema(make_app):
    app = make_app()
    schemas = []
    for scope in ("owner_dm", "group", "friend_dm"):
        schemas.append(next(t for t in await enabled_tools(app, scope) if t.name == "search").openai_schema())
    assert schemas[0] == schemas[1] == schemas[2]


# ---- the dashboard ---------------------------------------------------------------------------

async def test_search_route_needs_auth_and_returns_grouped_hits_with_previews_only_for_other_chats(make_app, browser):
    app = make_app()
    await fill(app)
    await app.store.set_chat_auth_status(str(FRIEND), "approved", title="Fran")
    async with browser(app) as client:
        assert (await client.get("/api/search", params={"q": WORD}, headers={"Authorization": ""})).status_code == 403
        data = (await client.get("/api/search", params={"q": WORD})).json()
        groups = {group["kind"]: group for group in data["groups"]}
        assert {"chat", "memory", "brief", "skill"} <= set(groups)
        by_title = {hit["title"]: hit for hit in groups["chat"]["hits"]}
        friend = next(hit for title, hit in by_title.items() if "Fran" in title)
        assert by_title["Your DM"]["full"] and not friend["full"]
        assert "\n" not in friend["text"] and len(friend["text"]) <= global_search.PREVIEW
        assert any(hit["href"] == "/app/memory?file=private%2FUSER.md" for hit in groups["memory"]["hits"])
        assert (await client.get("/api/search", params={"q": ""})).json()["groups"] == []
        assert (await client.get("/api/search", params={"q": "x" * 201})).status_code == 422


async def test_cross_site_browsers_cannot_reach_search_or_export(make_app, browser):
    app = make_app()
    seed_home(app.home.root)
    async with browser(app) as client:
        for path in ("/api/search?q=x", "/api/memory/export"):
            assert (await client.get(path, headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
            assert (await client.get(path, headers={"Sec-Fetch-Site": "same-site"})).status_code == 403
            assert (await client.get(path, headers={"Sec-Fetch-Site": "none"})).status_code == 403
            assert (await client.get(path, headers={"Sec-Fetch-Site": "same-origin"})).status_code == 200
    events = [m.text for m in await app.store.get_recent_chat_window(OWNER, 10) if m.role == "event"]
    assert len(events) == 1  # only the same-origin download was logged


def test_an_access_session_must_send_same_origin_fetch_metadata():
    from starlette.requests import Request
    from dom.web.auth import fetch_site_ok

    def req(site):
        headers = [(b"sec-fetch-site", site.encode())] if site else []
        return Request({"type": "http", "headers": headers})

    assert fetch_site_ok(req("same-origin"), "access") and not fetch_site_ok(req(None), "access")
    assert fetch_site_ok(req(None), "local") and not fetch_site_ok(req("cross-site"), "local")


async def test_search_gives_the_owner_dm_text_whole(make_app, browser):
    app = make_app()
    long_text = f"{WORD} first line\n\nsecond   paragraph " + "word " * 80
    await app.store.record_new_message(StoredMessage(
        chat_id=str(OWNER), msg_id=1, text=long_text, visibility_scope=app.store.visibility_for(OWNER),
        created_at="2026-11-18T10:00:00+00:00"))
    async with browser(app) as client:
        data = (await client.get("/api/search", params={"q": WORD})).json()
    hit = next(g for g in data["groups"] if g["kind"] == "chat")["hits"][0]
    assert hit["full"] and hit["text"] == long_text


async def test_export_route_is_owner_only_logs_an_event_and_returns_the_zip(make_app, browser):
    app = make_app()
    seed_home(app.home.root)
    async with browser(app) as client:
        assert (await client.get("/api/memory/export", headers={"Authorization": ""})).status_code == 403
        response = await client.get("/api/memory/export")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert ".env" not in names(response.content) and "private/USER.md" in names(response.content)
    events = [m.text for m in await app.store.get_recent_chat_window(OWNER, 10) if m.role == "event"]
    assert "Downloaded a memory export from the dashboard" in events


# ---- /export ----------------------------------------------------------------------------------

async def test_export_command_sends_the_owner_a_zip_and_only_the_owner(make_app, monkeypatch):
    from dom.telegram.handlers import handle_message
    app = make_app()
    seed_home(app.home.root)
    sent = []

    async def send_document(chat_id, document, **kw):
        sent.append((chat_id, document.filename, names(document.data)))
        return type("Sent", (), {"message_id": 1})()

    monkeypatch.setattr(app.bot, "send_document", send_document)
    await handle_message(app, tg(FRIEND, FRIEND, "/export"))
    await handle_message(app, tg(GROUP, OWNER, "/export"))
    assert sent == []
    await handle_message(app, tg(OWNER, OWNER, "/export"))
    (chat_id, filename, found), = sent
    assert chat_id == OWNER and filename.startswith("dom-memory-") and ".env" not in found and "private/USER.md" in found
