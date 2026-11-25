import asyncio
import json

import httpx

from dom import notes
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
    assert models == {"embed-old", "embed-new"}


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
    assert len(primary_requests) == len(backup_requests) == 1
    usage = app.store._conn.execute(
        "SELECT model, fallback FROM usage ORDER BY id DESC LIMIT 2"
    ).fetchall()
    assert [(row["model"], row["fallback"]) for row in reversed(usage)] == [("primary", 0), ("backup", 1)]


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
    assert index.staged_router is new
