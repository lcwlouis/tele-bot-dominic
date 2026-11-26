import asyncio
import json

import httpx

from dom import global_search, notes
from dom.config import ProviderEndpoint
from dom.embedding import EmbeddingProvider, EmbeddingRouter
from dom.knowledge import KnowledgeIndex, PROBE_TEXT
from dom.memory_pass import MemoryPass
from dom.models import StoredMessage
from tghelpers import FRIEND, GROUP, OWNER

OTHER = -2002


async def allowed():
    return True


def embedding_router(app, http, model="embed-a"):
    endpoint = ProviderEndpoint(base_url="https://embed.test/v1", model=model)
    return EmbeddingRouter(EmbeddingProvider(endpoint, None, http, app.dispatch.usage))


def vectors(requests, *, fail=None, probe=(0.5, 0.5)):
    def respond(request):
        requests.append(request)
        if fail and fail[0]:
            fail[0] = False
            return httpx.Response(503, json={"error": "offline"})
        body = json.loads(request.content)
        inputs = body["input"]
        data = []
        for index, text in enumerate(inputs):
            if text == PROBE_TEXT:
                vector = list(probe if not callable(probe) else probe())
            elif "Japan" in text or "holiday" in text:
                vector = [1.0, 0.0]
            else:
                vector = [0.0, 1.0]
            data.append({"index": index, "embedding": vector})
        return httpx.Response(200, json={
            "data": data, "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs), "cost": 0.001},
        })
    return respond


def test_entity_metadata_is_compact_free_and_stripped_from_model_text():
    raw = "- Japan trip in April {chat -1009} {entities person:222|place:japan|goal:take-a-break}"
    line = notes.parse_line(raw)
    assert line.entities == ("person:222", "place:japan", "goal:take-a-break")
    assert notes.strip_tags(raw) == "- Japan trip in April"
    assert notes.visible_len(raw) == len("- Japan trip in April")
    assert notes.format_line(line) == raw


async def test_memory_pass_can_create_a_new_entity_note_without_an_extra_model_call(make_app):
    reply = json.dumps({"ops": [{
        "file": "projects/dom-release.md", "op": "add", "text": "Release v0.8.4 is underway",
        "entities": ["project:dom-release", "goal:ship-v084"],
    }], "summary": "release noted"})
    app = make_app(contents=(reply,))
    await app.store.set_chat_auth_status(str(GROUP), "approved")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(GROUP), msg_id=1, sender_user_id=FRIEND, role="user", text="We're shipping v0.8.4",
        visibility_scope=app.store.visibility_for(GROUP), created_at="2026-11-26T01:00:00+00:00",
    ))

    assert await MemoryPass(app).run(GROUP)
    text = app.home.read("memory/projects/dom-release.md")
    assert text == (
        "- Release v0.8.4 is underway {chat -1009} "
        "{entities project:dom-release|goal:ship-v084}"
    )
    assert len(app.script.requests) == 1


async def test_graph_lookup_and_links_filter_every_other_chat_in_code(make_app):
    app = make_app()
    app.home._write_regular("memory/projects/dom.md", "\n".join([
        "- Public release exists {any} {entities project:dom|goal:ship}",
        f"- Group A secret launch {{chat {GROUP}}} {{entities project:dom|place:alpha}}",
        f"- Group B secret launch {{chat {OTHER}}} {{entities project:dom|place:beta}}",
    ]))
    await app.knowledge.rebuild()

    group = await app.knowledge.search("dom", requester_chat_id=GROUP, mode="entity")
    other = await app.knowledge.search("dom", requester_chat_id=OTHER, mode="entity")
    friend = await app.knowledge.search("dom", requester_chat_id=FRIEND, mode="entity")
    owner = await app.knowledge.search("dom", requester_chat_id=OWNER, owner=True, mode="entity")
    assert {hit.text for hit in group} == {"- Public release exists", "- Group A secret launch"}
    assert {hit.text for hit in other} == {"- Public release exists", "- Group B secret launch"}
    assert {hit.text for hit in friend} == {"- Public release exists"}
    assert len(owner) == 3
    assert set(await app.knowledge.related("project:dom", requester_chat_id=GROUP)) == {
        "goal:ship", "place:alpha", f"chat:{GROUP}",
    }
    assert "place:beta" not in await app.knowledge.related("project:dom", requester_chat_id=GROUP)


async def test_incremental_refresh_replaces_changed_file_and_indexes_messages(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Old plan {any} {entities place:japan}")
    await app.knowledge.refresh()
    assert [hit.text for hit in await app.knowledge.search("Old", requester_chat_id=OWNER, owner=True)] == ["- Old plan"]

    app.home._write_regular("memory/places/japan.md", "- New plan {any} {entities place:japan}")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(GROUP), msg_id=1, role="user", text="message only phrase", visibility_scope=f"chat:{GROUP}",
        created_at="2026-11-26T01:00:00+00:00",
    ))
    await app.knowledge.refresh()
    assert not await app.knowledge.search("Old", requester_chat_id=OWNER, owner=True)
    assert [hit.text for hit in await app.knowledge.search("New", requester_chat_id=OWNER, owner=True)] == ["- New plan"]
    assert [hit.text for hit in await app.knowledge.search("message only", requester_chat_id=GROUP)] == ["message only phrase"]


async def test_embedding_pause_resume_and_usage_attribution(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    requests, fail = [], [True]
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors(requests, fail=fail))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        paused = await index.progress()
        assert paused["state"] == "paused" and paused["last_error"] == "HTTPStatusError"
        await index.run_pending()
        done = await index.progress()
        assert done["state"] == "idle" and done["left"] == 0
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert hits[0].text == "- Japan holiday"
    rows = [row for row in app.store._conn.execute("SELECT kind, model, chat_id FROM usage ORDER BY id")]
    assert rows and all(row["kind"] == "embedding" and row["model"] == "embed-a" for row in rows)
    assert all(row["chat_id"] == OWNER for row in rows)


async def test_embedding_index_pauses_before_network_when_spend_is_blocked(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    requests = []

    async def blocked():
        return False

    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors(requests))) as http:
        index = KnowledgeIndex(
            app.store, app.home, embedding_router(app, http), allowed=blocked, chat_id=OWNER,
        )
        await index.run_pending()
        progress = await index.progress()

    assert requests == []
    assert progress["state"] == "paused" and progress["last_error"] == "spend cap reached"


async def test_probe_drift_marks_index_stale_and_pauses_meaning(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    probe = [[1.0, 0.0]]
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([], probe=lambda: probe[0]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        probe[0] = [0.0, 1.0]
        app.home._write_regular(
            "memory/places/japan.md",
            "- Japan holiday {any} {entities place:japan}\n- A new Japan plan {any} {entities place:japan}",
        )
        await index.run_pending()
        progress = await index.progress()
        await index.run_pending()
        still_stale = await index.progress()
    assert progress["state"] == "paused" and progress["stale"] is True
    assert progress["last_error"] == "probe drift"
    assert still_stale["state"] == "paused" and still_stale["stale"] is True
    assert still_stale["last_error"] == "probe drift"


async def test_staged_model_keeps_old_search_until_side_by_side_reindex_finishes(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as old_http,
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as new_http,
    ):
        old = embedding_router(app, old_http, "embed-old")
        new = embedding_router(app, new_http, "embed-new")
        index = KnowledgeIndex(app.store, app.home, old, allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        assert index.stage(new) is True
        before = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
        assert before and index.router.providers[0].endpoint.model == "embed-old"
        assert await index.reindex() is True
        after = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert after and index.router.providers[0].endpoint.model == "embed-new"
    models = {row[0] for row in app.store._conn.execute("SELECT DISTINCT model_id FROM knowledge_vectors")}
    assert models == {"embed-new"}  # the old model's vectors go once the switch is confirmed


async def test_backup_provider_keeps_its_own_index_and_serves_when_primary_is_down(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")

    def offline(request):
        return httpx.Response(503, json={"error": "offline"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(offline)) as primary_http,
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as backup_http,
    ):
        first = EmbeddingProvider(
            ProviderEndpoint(base_url="https://primary.test/v1", model="primary"), None, primary_http,
            app.dispatch.usage,
        )
        backup = EmbeddingProvider(
            ProviderEndpoint(base_url="https://backup.test/v1", model="backup"), None, backup_http,
            app.dispatch.usage,
        )
        index = KnowledgeIndex(
            app.store, app.home, EmbeddingRouter(first, backup), allowed=allowed, chat_id=OWNER,
        )
        await index.run_pending()
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert hits and hits[0].text == "- Japan holiday"
    rows = app.store._conn.execute(
        "SELECT model_id, state FROM embedding_indexes ORDER BY model_id"
    ).fetchall()
    assert [(row["model_id"], row["state"]) for row in rows] == [("backup", "idle"), ("primary", "paused")]
    assert app.store._conn.execute(
        "SELECT COUNT(*) FROM knowledge_vectors WHERE model_id='backup'"
    ).fetchone()[0] > 0


async def test_meaning_search_uses_backup_index_when_primary_index_is_stale(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    primary_requests, backup_requests = [], []
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(vectors(primary_requests))) as primary_http,
        httpx.AsyncClient(transport=httpx.MockTransport(vectors(backup_requests))) as backup_http,
    ):
        primary = EmbeddingProvider(
            ProviderEndpoint(base_url="https://primary.test/v1", model="primary"), None, primary_http,
            app.dispatch.usage,
        )
        backup = EmbeddingProvider(
            ProviderEndpoint(base_url="https://backup.test/v1", model="backup"), None, backup_http,
            app.dispatch.usage,
        )
        index = KnowledgeIndex(
            app.store, app.home, EmbeddingRouter(primary, backup), allowed=allowed, chat_id=OWNER,
        )
        await index.run_pending()
        app.store._conn.execute(
            "UPDATE embedding_indexes SET stale=1 WHERE model_key=?", (primary.model_key,),
        )
        primary_requests.clear()
        backup_requests.clear()
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")

    assert hits and hits[0].text == "- Japan holiday"
    assert primary_requests == [] and len(backup_requests) == 1  # a stale index is skipped before any paid call
    usage = app.store._conn.execute("SELECT model, fallback FROM usage ORDER BY id DESC LIMIT 1").fetchall()
    assert [(row["model"], row["fallback"]) for row in usage] == [("backup", 1)]


async def test_cancelled_reindex_never_switches_away_from_completed_old_index(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        started.set()
        await release.wait()
        return vectors([])(request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as old_http,
        httpx.AsyncClient(transport=httpx.MockTransport(slow)) as new_http,
    ):
        old = embedding_router(app, old_http, "embed-old")
        new = embedding_router(app, new_http, "embed-new")
        index = KnowledgeIndex(app.store, app.home, old, allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        index.stage(new)
        task = asyncio.create_task(index.reindex())
        await started.wait()
        await index.cancel()
        release.set()
        assert await task is False
    assert index.router.providers[0].endpoint.model == "embed-old"
    assert index.staged_router is None  # a cancelled model is dropped, not left staged


# ---- review rework (PR #81) -------------------------------------------------------------------

import sqlite3

from dom.store import store as store_module
from dom.tools import TurnCtx, _search


def tool_ctx(app, chat_id, scope, sender, participants=()):
    return TurnCtx(
        scope, chat_id, sender, app.home, app.store, OWNER, app=app, participant_ids=tuple(participants),
    )


async def tool_search(app, ctx, query, mode="keyword"):
    return json.loads(await _search({"query": query, "mode": mode}, ctx))


def seed_leak_notes(app):
    """One note of every kind: what a group may and may not reach."""
    app.home._write_regular(f"memory/chats/{OTHER}.md", "- Japan holiday plan of the other group")
    app.home._write_regular(f"memory/chats/{GROUP}.md", "- Japan holiday plan of this group")
    app.home._write_regular(f"memory/people/{FRIEND}.md", f"- Japan holiday wish of a member {{chat {GROUP}}}")
    app.home._write_regular("memory/people/777.md", "- Japan holiday wish of a stranger")
    app.home._write_regular("memory/projects/shared.md", "- Japan holiday shared project {any}")
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "- Japan holiday owner secret")


async def test_untagged_chat_note_lines_never_reach_another_chat_in_any_mode(make_app):
    app = make_app()
    seed_leak_notes(app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        app.knowledge = KnowledgeIndex(
            app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER,
        )
        await app.knowledge.run_pending()
        group = tool_ctx(app, GROUP, "group", FRIEND, participants=(FRIEND,))
        other = tool_ctx(app, OTHER, "group", FRIEND, participants=())
        for mode, query in (("keyword", "Japan"), ("meaning", "holiday"), ("entity", f"{OTHER}")):
            texts = {hit["text"] for hit in await tool_search(app, group, query, mode)}
            assert not any("other group" in text for text in texts), mode
            assert not any("stranger" in text or "owner secret" in text for text in texts), mode
        keyword = {hit["text"] for hit in await tool_search(app, group, "Japan")}
        assert "- Japan holiday plan of this group" in keyword
        assert "- Japan holiday shared project" in keyword
        meaning = {hit["text"] for hit in await tool_search(app, group, "holiday", "meaning")}
        assert "- Japan holiday plan of this group" in meaning
        assert {hit["text"] for hit in await tool_search(app, other, f"{OTHER}", "entity")} == {
            "- Japan holiday plan of the other group",
        }
        # a member's people note is searchable only in chats where that person is present, with its tag rules
        assert "- Japan holiday wish of a member" in keyword
        assert "- Japan holiday wish of a member" not in {
            hit["text"] for hit in await tool_search(app, other, "Japan")
        }
        friend_dm = tool_ctx(app, FRIEND, "friend_dm", FRIEND, participants=(FRIEND,))
        assert "- Japan holiday wish of a member" not in {hit["text"] for hit in await tool_search(app, friend_dm, "Japan")}


async def test_owner_global_search_sees_every_public_line_and_an_owner_in_a_group_does_not(make_app):
    app = make_app()
    seed_leak_notes(app)
    await app.knowledge.refresh()
    owner_dm = tool_ctx(app, OWNER, "owner_dm", OWNER)
    everything = " ".join(hit["text"] for hit in await tool_search(app, owner_dm, "Japan"))
    assert "other group" in everything and "stranger" in everything and "owner secret" in everything
    owner_in_group = tool_ctx(app, GROUP, "group", OWNER, participants=(OWNER,))
    texts = " ".join(hit["text"] for hit in await tool_search(app, owner_in_group, "Japan"))
    assert "other group" not in texts and "owner secret" not in texts and "stranger" not in texts
    found = await global_search.search_all(app.store, app.home, "Japan", knowledge=app.knowledge)
    assert {hit.ref for hit in found["memory"]} >= {f"memory/chats/{OTHER}.md", "memory/people/777.md"}


# ---- 2. sqlite-vec is optional ----------------------------------------------------------------

async def test_store_opens_and_meaning_search_falls_back_when_sqlite_vec_will_not_load(make_app, monkeypatch):
    def broken(conn):
        raise sqlite3.OperationalError("extension unavailable")

    monkeypatch.setattr(store_module.sqlite_vec, "load", broken)
    app = make_app()
    assert app.store.vec_ok is False
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors(requests))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert [hit.text for hit in hits] == ["- Japan holiday"]  # the keyword fallback
    assert requests == []  # no paid calls for an index that cannot be searched


async def test_vector_query_errors_fall_back_to_keywords(make_app, monkeypatch):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("no such function: vec_distance_cosine")

        monkeypatch.setattr(index, "_vector_rows", broken)
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert [hit.text for hit in hits] == ["- Japan holiday"]


# ---- 3. cancel ---------------------------------------------------------------------------------

def provider_for(app, http, model, url="https://embed.test/v1"):
    return EmbeddingProvider(ProviderEndpoint(base_url=url, model=model), None, http, app.dispatch.usage)


async def test_cancel_before_the_reindex_starts_discards_the_staged_model(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as old_http,
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as new_http,
    ):
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, old_http, "embed-old"), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        index.stage(embedding_router(app, new_http, "embed-new"))
        task = asyncio.create_task(index.reindex())  # confirmed, but the task has not run yet
        await index.cancel()
        assert await task is False
    assert index.staged_router is None
    assert index.router.providers[0].endpoint.model == "embed-old"
    assert {row[0] for row in app.store._conn.execute("SELECT DISTINCT model_id FROM knowledge_vectors")} == {"embed-old"}


async def test_cancel_during_the_primary_stops_before_the_fallback_and_deletes_partial_vectors(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    started, release, fallback_requests = asyncio.Event(), asyncio.Event(), []

    async def slow(request):
        started.set()
        await release.wait()
        return vectors([])(request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as old_http,
        httpx.AsyncClient(transport=httpx.MockTransport(slow)) as slow_http,
        httpx.AsyncClient(transport=httpx.MockTransport(vectors(fallback_requests))) as backup_http,
    ):
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, old_http, "embed-old"), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        index.stage(EmbeddingRouter(
            provider_for(app, slow_http, "new-primary", "https://new.test/v1"),
            provider_for(app, backup_http, "new-backup", "https://backup.test/v1"),
        ))
        task = asyncio.create_task(index.reindex())
        await started.wait()
        await index.cancel()
        release.set()
        assert await task is False
    assert fallback_requests == []
    assert index.staged_router is None and index.router.providers[0].endpoint.model == "embed-old"
    assert {row[0] for row in app.store._conn.execute("SELECT DISTINCT model_id FROM knowledge_vectors")} == {"embed-old"}
    assert {row[0] for row in app.store._conn.execute("SELECT model_id FROM embedding_indexes")} == {"embed-old"}


# ---- 4. a refresh during an embedding call -------------------------------------------------------

async def test_a_refresh_during_an_embedding_call_does_not_break_the_run(make_app):
    app = make_app()
    path = "memory/places/japan.md"
    app.home._write_regular(path, "- Japan holiday {any} {entities place:japan}")
    app.home._write_regular("memory/projects/zeta.md", "- Later fact {any}")  # keeps the old row ids from being reused
    holder = {}

    async def edit_mid_call(request):
        if not holder.get("done"):
            holder["done"] = True
            app.home._write_regular(path, "- Japan holiday trip {any} {entities place:japan}")
            await holder["index"].refresh()
        return vectors([])(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(edit_mid_call)) as http:
        index = holder["index"] = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        progress = await index.progress()
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert progress["state"] == "idle" and progress["left"] == 0
    assert hits[0].text == "- Japan holiday trip"
    assert app.store._conn.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0] == 2  # no orphan vectors


async def test_a_database_error_pauses_the_index_instead_of_leaving_it_running(make_app, monkeypatch):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)

        def broken(*args, **kwargs):
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

        monkeypatch.setattr(index, "_save_vectors", broken)
        await index.run_pending()
        progress = await index.progress()
    assert progress["state"] == "paused" and progress["last_error"] == "IntegrityError"


async def test_memory_pass_survives_an_index_failure_and_does_not_wait_for_embeddings(make_app):
    reply = json.dumps({"ops": [{"file": "chats/-1009.md", "op": "add", "text": "Likes tea"}], "summary": "tea"})
    app = make_app(contents=(reply,))
    await app.store.set_chat_auth_status(str(GROUP), "approved")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(GROUP), msg_id=1, sender_user_id=FRIEND, role="user", text="I like tea",
        visibility_scope=app.store.visibility_for(GROUP), created_at="2026-11-26T01:00:00+00:00",
    ))
    gate = asyncio.Event()

    async def blocked():
        await gate.wait()
        raise RuntimeError("index exploded")

    app.knowledge.run_pending = blocked
    assert await asyncio.wait_for(MemoryPass(app).run(GROUP), 2)  # returns while the index is still blocked
    gate.set()
    await asyncio.sleep(0.05)  # the spawned task logs the failure instead of raising it


# ---- 5. a changed model across a restart, drift and same-model rebuilds ---------------------------

async def test_boot_with_a_changed_config_model_serves_nothing_new_until_confirmed(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors(requests))) as http:
        first = KnowledgeIndex(app.store, app.home, embedding_router(app, http, "embed-old"), allowed=allowed, chat_id=OWNER)
        await first.run_pending()
        requests.clear()
        # the process restarts with a new model in the config
        again = KnowledgeIndex(app.store, app.home, embedding_router(app, http, "embed-new"), allowed=allowed, chat_id=OWNER)
        await again.run_pending()
        assert requests == []  # no spend on a partial index before the owner confirms
        assert again.staged_router is not None and (await again.estimate())["staged"] is True
        assert await again.reindex() is True
    assert again.router.providers[0].endpoint.model == "embed-new"
    assert {row[0] for row in app.store._conn.execute("SELECT DISTINCT model_id FROM knowledge_vectors")} == {"embed-new"}


async def test_confirm_can_rebuild_the_same_model_after_drift_and_clears_stale(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}")
    probe = [[1.0, 0.0]]
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([], probe=lambda: probe[0]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        probe[0] = [0.0, 1.0]
        app.home._write_regular("memory/places/japan.md", "- Japan holiday {any}\n- More Japan {any}")
        await index.run_pending()
        assert (await index.progress())["stale"] is True
        assert (await index.estimate())["staged"] is True
        assert await index.reindex() is True
        done = await index.progress()
    assert done["stale"] is False and done["state"] == "idle" and done["left"] == 0


async def test_the_first_probe_is_kept_so_gradual_drift_is_caught(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any}")
    probe = [[1.0, 0.0]]
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([], probe=lambda: probe[0]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        probe[0] = [0.96, 0.28]  # within the threshold of the first probe
        app.home._write_regular("memory/places/japan.md", "- Japan holiday {any}\n- Step two {any}")
        await index.run_pending()
        assert (await index.progress())["stale"] is False
        probe[0] = [0.85, 0.53]  # close to the second probe, far from the first
        app.home._write_regular("memory/places/japan.md", "- Japan holiday {any}\n- Step two {any}\n- Step three {any}")
        await index.run_pending()
        assert (await index.progress())["stale"] is True


# ---- 6. deleted and edited messages --------------------------------------------------------------

async def add_message(app, msg_id, text, chat=GROUP):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat), msg_id=msg_id, sender_user_id=FRIEND, role="user", text=text,
        visibility_scope=app.store.visibility_for(chat), created_at=f"2026-11-26T01:0{msg_id}:00+00:00",
    ))


async def test_deleted_and_edited_messages_stop_matching_at_once(make_app):
    app = make_app()
    await add_message(app, 1, "the unicorn password is swordfish")
    await add_message(app, 2, "an ordinary remark")
    await app.knowledge.refresh()
    assert await app.knowledge.search("swordfish", requester_chat_id=GROUP)

    assert await app.store.record_message_delete(str(GROUP), [1]) == 1  # no refresh yet
    assert not await app.knowledge.search("swordfish", requester_chat_id=GROUP)
    assert not await app.knowledge.search("swordfish", requester_chat_id=OWNER, owner=True)

    await app.store.record_message_edit(str(GROUP), 2, "a corrected remark about badgers")
    assert not await app.knowledge.search("ordinary", requester_chat_id=GROUP)
    await app.knowledge.refresh()
    assert [hit.text for hit in await app.knowledge.search("badgers", requester_chat_id=GROUP)] == [
        "a corrected remark about badgers",
    ]


async def test_forgetting_a_chat_with_its_history_removes_its_messages_from_the_index(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(str(GROUP), "left")
    await add_message(app, 1, "the unicorn password is swordfish")
    await app.knowledge.refresh()
    assert await app.store.forget_chat(GROUP, delete_history=True)
    assert not await app.knowledge.search("swordfish", requester_chat_id=OWNER, owner=True)
    assert app.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0] == 0


async def test_deleted_message_vectors_are_not_returned_by_meaning_search(make_app):
    app = make_app()
    await add_message(app, 1, "Japan holiday secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        assert await index.search("holiday", requester_chat_id=GROUP, mode="meaning")
        app.store._conn.execute("UPDATE messages SET is_deleted = 1")  # a path that skips the hooks
        assert not await index.search("holiday", requester_chat_id=GROUP, mode="meaning")


# ---- 7. group search keeps recent messages --------------------------------------------------------

async def test_group_search_merges_recent_history_with_graph_hits_without_duplicates(make_app):
    app = make_app()
    app.home._write_regular("memory/projects/trip.md", f"- Japan trip is booked {{chat {GROUP}}} {{entities project:trip}}")
    await add_message(app, 1, "Japan flights cost too much")
    await app.knowledge.refresh()
    await add_message(app, 2, "Japan hotel found just now")  # newer than the last index refresh
    ctx = tool_ctx(app, GROUP, "group", FRIEND, participants=(FRIEND,))
    hits = await tool_search(app, ctx, "Japan")
    texts = [hit["text"] for hit in hits]
    assert "Japan hotel found just now" in texts and "Japan flights cost too much" in texts
    assert "- Japan trip is booked" in texts
    assert len(texts) == len(set(texts))  # the message in both sources appears once
    legacy = next(hit for hit in hits if hit["text"] == "Japan hotel found just now")
    assert {"chat_id", "msg_id", "sender_name", "created_at"} <= legacy.keys()


async def test_group_search_follows_a_chat_migration_alias(make_app):
    app = make_app()
    new_id = -1009001
    await add_message(app, 1, "Japan flights cost too much", chat=GROUP)
    await app.store.migrate_chat(GROUP, new_id)
    await app.knowledge.refresh()
    ctx = tool_ctx(app, new_id, "group", FRIEND, participants=(FRIEND,))
    assert "Japan flights cost too much" in {hit["text"] for hit in await tool_search(app, ctx, "Japan")}
    assert "Japan flights cost too much" in {
        hit.text for hit in await app.knowledge.search("Japan", requester_chat_id=new_id)
    }


# ---- 8. incremental message sync -------------------------------------------------------------------

async def test_message_sync_is_incremental_and_unchanged_lines_are_not_reembedded(make_app):
    app = make_app()
    for index in range(1, 6):
        await add_message(app, index, f"message number {index}")
    await app.knowledge.refresh()
    seen = []
    original = app.knowledge._insert_fact

    def counting(conn, kind, fact):
        seen.append(fact["source_key"])
        return original(conn, kind, fact)

    app.knowledge._insert_fact = counting
    await add_message(app, 6, "message number 6")
    await app.knowledge.refresh()
    assert len(seen) == 1  # only the new message was touched
    seen.clear()
    await app.knowledge.refresh()
    assert seen == []


async def test_editing_a_note_file_keeps_vectors_of_unchanged_lines(make_app):
    app = make_app()
    path = "memory/places/japan.md"
    app.home._write_regular(path, "- Japan holiday {any}\n- Kyoto temples {any}")
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors(requests))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        requests.clear()
        app.home._write_regular(path, "- Japan holiday {any}\n- Kyoto temples {any}\n- A new line {any}")
        await index.run_pending()
    embedded = [text for request in requests for text in json.loads(request.content)["input"]]
    assert [text for text in embedded if text != PROBE_TEXT] == ["- A new line"]


# ---- 9. entity file names offered to a group pass ----------------------------------------------------

async def test_a_group_pass_is_offered_only_entity_files_with_a_line_it_may_see(make_app):
    app = make_app()
    app.home._write_regular("memory/projects/secret.md", f"- Hidden launch {{chat {OTHER}}}")
    app.home._write_regular("memory/projects/open.md", f"- Open launch {{chat {GROUP}}}")
    app.home._write_regular("memory/goals/shared.md", "- Shared goal {any}")
    messages = [StoredMessage(
        chat_id=str(GROUP), msg_id=1, sender_user_id=FRIEND, role="user", text="hi",
        visibility_scope=f"chat:{GROUP}", created_at="2026-11-26T01:00:00+00:00",
    )]
    offered = MemoryPass(app)._offered("group", GROUP, messages)
    assert "projects/secret.md" not in offered
    assert {"projects/open.md", "goals/shared.md"} <= set(offered)
    assert MemoryPass._purpose("memory/projects/open.md", "group") != MemoryPass._purpose(f"memory/chats/{GROUP}.md", "group")


async def test_a_group_pass_may_only_add_to_an_entity_file_it_was_not_offered(make_app):
    app = make_app()
    app.home._write_regular("memory/projects/secret.md", f"- Hidden launch {{chat {OTHER}}}")
    pass_ = MemoryPass(app)
    refused: list = []
    offered: dict = {}
    problem = pass_._write(
        app.home, "group", GROUP, offered,
        {"file": "projects/secret.md", "op": "replace", "old": "Hidden launch", "text": "Known"}, refused,
    )
    assert problem is not None and "not offered" in problem
    assert "Hidden launch" in app.home.read("memory/projects/secret.md")


# ---- nits ------------------------------------------------------------------------------------------------

async def test_query_embedding_is_attributed_to_the_requesting_chat(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        before = app.store._conn.execute("SELECT MAX(id) FROM usage").fetchone()[0]
        await index.search("holiday", requester_chat_id=GROUP, mode="meaning")
    rows = app.store._conn.execute("SELECT chat_id FROM usage WHERE id > ?", (before,)).fetchall()
    assert [row[0] for row in rows] == [GROUP]


async def test_an_empty_primary_result_does_not_call_the_backup(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", f"- Japan holiday {{chat {GROUP}}}")
    primary_requests, backup_requests = [], []
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(vectors(primary_requests))) as primary_http,
        httpx.AsyncClient(transport=httpx.MockTransport(vectors(backup_requests))) as backup_http,
    ):
        index = KnowledgeIndex(app.store, app.home, EmbeddingRouter(
            provider_for(app, primary_http, "primary", "https://primary.test/v1"),
            provider_for(app, backup_http, "backup", "https://backup.test/v1"),
        ), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        primary_requests.clear()
        backup_requests.clear()
        # a group that may see none of the notes: the primary index is fine but has nothing for it
        hits = await index.search("holiday", requester_chat_id=OTHER, mode="meaning")
    assert len(primary_requests) == 1 and backup_requests == []
    assert hits == []  # the keyword fallback finds nothing this chat may see either


async def test_keyword_scores_are_normalised_not_clamped(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "\n".join([
        "- Kyoto Kyoto Kyoto temples {any}", "- Kyoto trip {any}", "- Osaka food {any}", "- Nara deer {any}",
    ]))
    await app.knowledge.refresh()
    hits = await app.knowledge.search("Kyoto", requester_chat_id=OWNER, owner=True)
    scores = [hit.score for hit in hits]
    assert scores[0] == 1.0 and len(set(scores)) > 1 and all(0.0 < score <= 1.0 for score in scores)
    assert scores == sorted(scores, reverse=True)


async def test_message_sync_reads_in_small_chunks_and_a_sweep_drops_removals_from_other_paths(make_app, monkeypatch):
    import dom.knowledge as knowledge

    monkeypatch.setattr(knowledge, "SYNC_CHUNK", 2)
    app = make_app()
    for index in range(1, 6):
        await add_message(app, index, f"remark number {index}")
    await app.knowledge.refresh()
    assert app.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0] == 5

    app.store._conn.execute("DELETE FROM messages WHERE msg_id = 3")  # a path that skips the store hooks
    app.store._conn.execute("UPDATE knowledge_sync SET swept_at = ''")  # the rare sweep is due
    await app.knowledge.refresh()
    assert app.store._conn.execute("SELECT COUNT(*) FROM knowledge_facts").fetchone()[0] == 4
    assert not await app.knowledge.search("number 3", requester_chat_id=GROUP)


def test_migration_runs_on_a_copy_of_a_database_built_before_it(tmp_path):
    from dom.store.store import MIGRATIONS, MIGRATIONS_DIR, _split_statements, migrate_database

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        for version, filename in MIGRATIONS:
            if filename == "0030_knowledge_index.sql":
                break
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _split_statements((MIGRATIONS_DIR / filename).read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute("COMMIT")
        conn.execute(
            "INSERT INTO usage(at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback) "
            "VALUES ('2026-11-25T00:00:00Z', 'chat', 'm', 'h', 1, 3, 4, 0.01, 0)"
        )
    finally:
        conn.close()
    assert migrate_database(db_path) == store_module.TARGET_VERSION
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT kind, input_tokens FROM usage").fetchall() == [("chat", 3)]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"knowledge_facts", "knowledge_vectors", "knowledge_sync", "knowledge_dirty_messages"} <= tables
        conn.execute(
            "INSERT INTO usage(at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback) "
            "VALUES ('2026-11-26T00:00:00Z', 'embedding', 'e', 'h', 1, 1, 0, 0, 0)"
        )
    finally:
        conn.close()


async def test_two_messages_with_the_same_text_both_stay_in_group_search(make_app):
    # Re-check of #81: deduplicating by text hid a separate message that happened to say the same thing.
    app = make_app()
    await add_message(app, 1, "Japan again tonight")
    await app.knowledge.refresh()
    await add_message(app, 2, "Japan again tonight")
    ctx = tool_ctx(app, GROUP, "group", FRIEND, participants=(FRIEND,))
    texts = [hit["text"] for hit in await tool_search(app, ctx, "Japan")]
    assert texts.count("Japan again tonight") == 2


def shaped_vectors(table):
    """An embedding endpoint where each text's vector is the one in `table` whose key it contains (the query is
    "holiday"); anything else is orthogonal to it."""
    def respond(request):
        data = []
        for index, text in enumerate(json.loads(request.content)["input"]):
            vector = next((v for key, v in table.items() if key in text), [0.0, 1.0])
            data.append({"index": index, "embedding": vector})
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 1, "total_tokens": 1, "cost": 0.0}})
    return respond


async def test_meaning_search_drops_weak_hits_sorts_by_score_and_labels_them(make_app):
    app = make_app()
    app.home._write_regular(
        "memory/places/japan.md",
        "- Weak: a faint link {any}\n- Other: nothing alike {any}\n- Good: a nearby thing {any}\n"
        "- Best: the holiday itself {any}",
    )
    table = {
        "holiday": [1.0, 0.0], "Best": [1.0, 0.0], "Good": [0.8, 0.6], "Weak": [0.4, 0.917], "Other": [0.1, 0.995],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(shaped_vectors(table))) as http:
        index = KnowledgeIndex(app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER)
        await index.run_pending()
        hits = await index.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
        strict = KnowledgeIndex(
            app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER,
            config=app.config.knowledge.model_copy(update={"min_similarity": 0.7, "min_score": 0.5}),
        )
        careful = await strict.search("holiday", requester_chat_id=OWNER, owner=True, mode="meaning")
    assert [hit.text for hit in hits] == ["- Best: the holiday itself", "- Good: a nearby thing", "- Weak: a faint link"]
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert [hit.match for hit in hits] == ["strong", "good", "weak"]  # strong takes the words and the meaning
    assert hits[0].score > hits[1].score > hits[2].score
    assert [hit.text for hit in careful] == ["- Best: the holiday itself"]  # the stricter config drops the rest


async def test_every_search_mode_sorts_strictly_by_score(make_app):
    app = make_app()
    app.home._write_regular(
        "memory/places/japan.md",
        "- Kyoto once\n- Kyoto Kyoto Kyoto temple and Kyoto gardens\n- Kyoto twice {entities place:japan}",
    )
    await app.knowledge.refresh()
    for mode in ("keyword", "entity"):
        hits = await app.knowledge.search("Kyoto" if mode == "keyword" else "japan", requester_chat_id=OWNER,
                                          owner=True, mode=mode)
        scores = [hit.score for hit in hits]
        assert hits and scores == sorted(scores, reverse=True)


async def test_search_tool_and_global_search_show_the_match_label(make_app):
    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}\n- Dull {any}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        app.knowledge = KnowledgeIndex(
            app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER,
            config=app.config.knowledge,
        )
        await app.knowledge.run_pending()
        found = await global_search.search_all(
            app.store, app.home, "holiday", mode="meaning", knowledge=app.knowledge)
        keyword = await global_search.search_all(app.store, app.home, "holiday", knowledge=app.knowledge)
    hit = found["memory"][0]
    assert hit.text == "- Japan holiday" and hit.score is not None and hit.match in {"strong", "good", "weak"}
    assert [h.text for h in found["memory"]] == ["- Japan holiday"]  # the dull line is under the similarity cutoff
    assert all(h.score is None and not h.match for h in keyword["memory"])


async def test_the_search_tool_tells_dom_how_strong_each_meaning_match_is(make_app):
    from dom.tools import SEARCH_TOOL, TurnCtx

    app = make_app()
    app.home._write_regular("memory/places/japan.md", "- Japan holiday {any} {entities place:japan}\n- Dull {any}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        app.knowledge = KnowledgeIndex(
            app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER, config=app.config.knowledge,
        )
        await app.knowledge.run_pending()
        ctx = TurnCtx(
            scope="owner_dm", chat_id=OWNER, sender_id=OWNER, home=app.home, store=app.store, owner_id=OWNER, app=app,
        )
        owner = json.loads(await SEARCH_TOOL.handler({"query": "holiday", "mode": "meaning"}, ctx))
        friend_ctx = TurnCtx(
            scope="group", chat_id=GROUP, sender_id=FRIEND, home=app.home, store=app.store, owner_id=OWNER, app=app,
        )
        group = json.loads(await SEARCH_TOOL.handler({"query": "holiday", "mode": "meaning"}, friend_ctx))
    assert [hit["match"] for hit in owner if hit["kind"] == "memory"] == ["strong match"]
    assert all(hit["match"] == "strong match" for hit in group if "file" in hit)
