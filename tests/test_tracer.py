"""The debug tracer (E102): what a turn's trace holds, redaction, the size cap and retention, the writer staying off
the reply path, and the dashboard's Talk and trace routes."""

import asyncio
import json
import os
import sqlite3
import stat
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from dom import talk, tracer, turn_log
from dom.presence import parse_habits
from dom.telegram.handlers import handle_message
from dom.tracer import TraceStore, TraceWriter
from test_web_auth import ORIGIN
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg

SECRET = "sk-or-v1-" + "s3cr3t" * 6  # an OpenRouter-shaped key the instance holds
PLAIN_SECRET = "hunter2-but-longer-than-eight"  # a value with no known shape: only the instance's list catches it
JSON = {"Origin": ORIGIN, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
SAME_SITE = {"Sec-Fetch-Site": "same-origin"}


def writer_for(app, secrets=(SECRET, PLAIN_SECRET), cap=256_000_000, days=7) -> TraceWriter:
    writer = TraceWriter(TraceStore(app.store.home / "traces.db"), lambda: (days, cap), lambda: secrets)
    app.traces = writer
    return writer


@pytest.fixture
def traced(make_app):
    """An app whose turns are traced; written synchronously by `flush` so tests see each trace."""
    made = []

    def make(*contents, **options):
        app = make_app(contents=contents)
        writer = writer_for(app, **options)
        turn_log.set_sink(writer.submit)
        made.append(writer)
        return app, writer

    yield make
    turn_log.set_sink(None)
    for writer in made:
        writer.store.close()


async def online(app, *chats):
    for chat in chats or (OWNER,):
        await app.dispatch.presence.go_online(chat, parse_habits(""))


async def flush(writer: TraceWriter) -> list[int]:
    ids = []
    while not writer._queue.empty():
        ids.append(await writer.write(writer._queue.get_nowait()))
        writer._queue.task_done()
    return ids


def rows(writer: TraceWriter) -> list[dict]:
    conn = sqlite3.connect(writer.store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [{**dict(row), "body": json.loads(zlib.decompress(row["body"]))} for row in conn.execute("SELECT * FROM traces ORDER BY id")]
    finally:
        conn.close()


# --- encoding and redaction ---------------------------------------------------------------------------------------


def test_encode_redacts_secret_values_and_shapes_and_strips_media():
    trace = turn_log.TurnTrace(chat_id=OWNER, scope="owner_dm")
    token = ":".join(("9" * 9, "A" * 35))  # built here so the public-data scan does not read it as a real token
    bearer = " ".join(("Bearer", "abc.def-123"))
    picture = "data:image/jpeg;base64," + "QUJD" * 5000
    trace.add_event("tool", {
        "name": "web_search", "result": f"key {SECRET} and {PLAIN_SECRET}, bot {token}, header {bearer}",
        "arguments": json.dumps({"q": PLAIN_SECRET}),
    })
    trace.add_event("model_call", {"call": turn_log.ModelCall("media", "m", False, "", "", None, None), "url": "",
                                   "request": {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": picture}}]}]},
                                   "error": None})
    body = tracer.encode(trace, [SECRET, PLAIN_SECRET])
    text = json.dumps(body)
    for leaked in (SECRET, PLAIN_SECRET, token, "abc.def-123", "QUJDQUJD"):
        assert leaked not in text
    assert text.count("[redacted]") >= 5
    assert body["redactions"] >= 5
    assert body["media_stripped"] == 1
    assert "[media image/jpeg, 15 kB, sha256 " in text


def test_a_cut_string_never_leaves_half_a_secret():
    trace = turn_log.TurnTrace()
    trace.add_event("note", {"text": "x" * 90 + PLAIN_SECRET + "y" * 200})
    body = tracer.encode(trace, [PLAIN_SECRET], cut=100)
    note = body["events"][0]["text"]
    assert body["truncated"] and "hunter2" not in note and "[cut " in note


def test_an_oversized_trace_is_cut_then_stripped_of_bodies(monkeypatch):
    monkeypatch.setattr(tracer, "MAX_TRACE_BYTES", 2_000)
    trace = turn_log.TurnTrace()
    noise = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(30_000))  # does not compress away
    trace.add_event("model_call", {"call": turn_log.ModelCall("chat", "m", False, "", "", None, None), "url": "",
                                   "request": {"messages": [{"role": "user", "content": noise}]}, "error": None})
    body, blob = tracer.pack(trace, [])
    assert body["truncated"]
    assert len(blob) <= 2_000


def test_a_trace_still_too_big_without_bodies_is_stored_as_a_bounded_stub(monkeypatch):
    monkeypatch.setattr(tracer, "MAX_TRACE_BYTES", 3_000)
    trace = turn_log.TurnTrace()
    for index in range(40):  # many events whose own text, not a model body, is what is big
        noise = "".join(chr(0x4E00 + (i * 7919 + index) % 20000) for i in range(2_000))
        trace.add_event("tool", {"name": "web_search", "result": noise})
    body, blob = tracer.pack(trace, [])
    assert len(blob) <= 3_000
    assert body["truncated"] and all(set(event) == {"type", "at", "t_ms"} for event in body["events"])


def test_events_after_submit_are_ignored_and_the_cap_counts_drops(monkeypatch):
    monkeypatch.setattr(turn_log, "MAX_EVENTS", 2)
    trace = turn_log.TurnTrace()
    for index in range(3):
        trace.add_event("note", {"text": str(index)})
    assert len(trace.events) == 2 and trace.dropped_events == 1
    turn_log.submit(trace)
    trace.add_event("note", {"text": "late"})
    assert len(trace.events) == 2


def test_model_call_keeps_a_frozen_copy_of_the_request():
    trace = turn_log.TurnTrace()
    messages = [{"role": "user", "content": "hi"}]
    body = {"model": "m", "messages": messages}
    with turn_log.track(trace):
        with turn_log.model_call("m", "chat", messages, [], body=body, url="https://llm.test/v1/chat/completions"):
            pass
        with turn_log.model_call("m", "memory", messages, [], body=body):  # traced, but not a Turn log call
            pass
    messages.append({"role": "tool", "content": "later"})  # the agent loop appends after the call
    first, memory = [event for event in trace.events if event["type"] == "model_call"]
    assert first["request"]["messages"] == [{"role": "user", "content": "hi"}]
    assert memory["call"].kind == "memory"
    assert [call.kind for call in trace.model_calls] == ["chat"]


# --- storage, the cap and retention -------------------------------------------------------------------------------


def _part(at: datetime, chat="1", turn_id=None) -> tuple[dict, bytes]:
    body = {"turn_id": turn_id, "chat_id": chat, "scope": "owner_dm", "kind": "turn", "label": "turn",
            "started_at": at.isoformat(), "outcome": "Replied", "events": []}
    return body, zlib.compress(json.dumps(body).encode() + b" " * 0)


async def test_the_size_cap_evicts_oldest_first(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    sizes = []
    for index in range(5):
        body, blob = _part(start + timedelta(minutes=index), turn_id=index)
        sizes.append(len(blob))
        store.insert(body, blob, max_bytes=sum(sizes[-3:]))
    left = await store.traced(range(5))
    assert left == {2, 3, 4}
    store.close()


async def test_retention_prunes_old_traces_and_a_forgotten_chat_loses_its_traces(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    for index, (age, chat) in enumerate(((10, "1"), (1, "1"), (1, "2"))):
        store.insert(*_part(now - timedelta(days=age), chat=chat, turn_id=index), max_bytes=10**9)
    writer = TraceWriter(store, lambda: (7, 10**9), lambda: (), now=lambda: now)
    await writer.prune()
    assert await store.traced(range(3)) == {1, 2}
    await store.delete_chat("2")
    assert await store.traced(range(3)) == {1}
    store.close()


# --- a real turn, end to end --------------------------------------------------------------------------------------


async def test_an_owner_dm_turn_is_traced_in_full(traced):
    reply = json.dumps({"no_response": False, "react": None, "bubbles": [{"text": f"the key is {SECRET}"}]})
    app, writer = traced(reply)
    await online(app)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="what's my key?"))
    await drain_dispatch(app.dispatch)
    await flush(writer)
    [row] = rows(writer)
    [turn] = await app.store.recent_turns()
    body = row["body"]
    assert row["turn_id"] == turn["id"] == body["turn_id"] and body["kind"] == "turn" and body["scope"] == "owner_dm"
    types = [event["type"] for event in body["events"]]
    for expected in ("arrival", "prompt", "model_call", "actions", "delivery"):
        assert expected in types
    call = next(event for event in body["events"] if event["type"] == "model_call")
    assert call["request"]["messages"][0]["role"] == "system"
    assert call["request"]["tools"]  # the full schemas offered
    assert call["response"]["choices"][0]["message"]["content"]
    assert call["status"] == 200 and call["duration_ms"] >= 0
    text = json.dumps(body)
    assert SECRET not in text and "[redacted]" in text
    delivery = next(event for event in body["events"] if event["type"] == "delivery")
    assert delivery["kind"] == "bubble" and delivery["msg_id"]


async def test_a_friends_turn_is_traced_in_full_too(traced):
    app, writer = traced()
    await app.store.set_chat_auth_status(FRIEND, "approved")
    await online(app, FRIEND)
    await handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="hi dom"))
    await drain_dispatch(app.dispatch)
    await flush(writer)
    [row] = rows(writer)
    assert row["scope"] == "friend_dm"
    assert any(event["type"] == "model_call" and event["request"] for event in row["body"]["events"])


async def test_a_tool_call_is_traced_with_its_arguments_and_result(traced):
    call = {"id": "c1", "type": "function", "function": {"name": "image", "arguments": "{}"}}
    answers = [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "assistant", "content": json.dumps({"bubbles": [{"text": "ok"}]})},
    ]

    def respond(request):
        message = answers.pop(0) if answers else answers_final
        return httpx.Response(200, json={"choices": [{"message": message}]})

    answers_final = answers[-1]
    app, writer = traced()
    app.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    await online(app)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="send a selfie"))
    await drain_dispatch(app.dispatch)
    await flush(writer)
    [row] = rows(writer)
    [tool] = [event for event in row["body"]["events"] if event["type"] == "tool"]
    assert tool["name"] == "image" and tool["offered"] is False and tool["error"]
    assert '"image" is not a tool' in tool["result"]


async def test_tracing_adds_no_latency_to_the_reply_path(make_app):
    """With the writer running beside the turns, a full owner-DM turn takes no measurably longer traced than
    untraced: the reply path only appends references and hands the trace over, and the writer encodes in a thread."""
    async def turns(app, first_id: int, count: int) -> float:
        started = time.perf_counter()
        for index in range(count):
            await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text=f"hello {index}", msg_id=first_id + index))
            await drain_dispatch(app.dispatch)
        return (time.perf_counter() - started) / count

    rounds = 12
    plain = make_app()
    await online(plain)
    await turns(plain, 1, 3)  # warm both paths alike
    untraced = await turns(plain, 10, rounds)

    app = make_app()
    await online(app)
    writer = writer_for(app)
    writer.start()
    try:
        await turns(app, 100, 3)  # the fixture's apps share one home: new message ids
        traced_time = await turns(app, 110, rounds)
    finally:
        await writer.close()
    assert traced_time - untraced < 0.005, (traced_time, untraced)
    assert writer.written == 15 and len(rows(writer)) == 15
    writer.store.close()


async def test_submit_hands_over_without_encoding():
    writer = TraceWriter(None, lambda: (7, 10**9), lambda: pytest.fail("secrets read on submit"))
    trace = turn_log.TurnTrace()
    trace.add_event("note", {"text": "hi"})
    writer.submit(trace)
    assert writer._queue.qsize() == 1


async def test_the_writer_reads_secrets_off_the_event_loop(make_app):
    app = make_app()
    loop_thread = threading.get_ident()
    seen = []
    writer = writer_for(app)
    writer.secrets = lambda: seen.append(threading.get_ident()) or (SECRET,)
    trace = turn_log.TurnTrace(chat_id=OWNER)
    trace.add_event("note", {"text": SECRET})
    await writer.write(trace)
    assert seen and seen[0] != loop_thread
    writer.store.close()


async def test_a_full_queue_drops_instead_of_waiting():
    writer = TraceWriter.__new__(TraceWriter)
    writer.dropped = 0
    writer._queue = asyncio.Queue(1)
    writer.submit(turn_log.TurnTrace())
    writer.submit(turn_log.TurnTrace(label="second"))
    assert writer.dropped == 1 and writer._queue.qsize() == 1


async def test_the_writer_task_drains_and_closes(make_app):
    app = make_app()
    writer = writer_for(app)
    writer.start()
    try:
        trace = turn_log.TurnTrace(chat_id=OWNER, scope="owner_dm")
        trace.add_event("note", {"text": "hello"})
        turn_log.submit(trace)
    finally:
        await writer.close()
    assert writer.written == 1 and turn_log._sink is None
    writer.store.close()


async def test_a_failing_write_is_logged_and_the_writer_goes_on(make_app, caplog, monkeypatch):
    app = make_app()
    writer = writer_for(app)
    calls = {"n": 0}
    original = tracer.pack

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(tracer, "pack", flaky)
    writer.start()
    try:
        for label in ("first", "second"):
            trace = turn_log.TurnTrace(chat_id=OWNER, label=label)
            trace.add_event("note", {"text": label})
            turn_log.submit(trace)
    finally:
        await writer.close()
    assert writer.written == 1
    assert "could not write a trace" in caplog.text
    writer.store.close()


# --- the dashboard ------------------------------------------------------------------------------------------------


async def test_a_dashboard_message_joins_the_owner_dm_and_is_answered(traced, browser):
    app, writer = traced()
    await online(app)
    async with browser(app) as client:
        response = await client.post("/api/talk/send", json={"text": "hi from the web"}, headers=JSON)
        assert response.status_code == 200, response.text
        await drain_dispatch(app.dispatch)
        mirror = next(call for call in app.bot.calls if call[0] == "send" and call[2].startswith(talk.MIRROR))
        assert mirror[1] == OWNER and mirror[2] == "🖥 hi from the web" and mirror[3]["disable_notification"]
        stored = await app.store.get_message(OWNER, response.json()["msg_id"])
        assert stored.origin == "dashboard" and stored.text == "hi from the web" and stored.sender_user_id == OWNER

        page = (await client.get("/api/talk?trace=1", headers=SAME_SITE)).json()
        whos = [(message["who"], message["origin"]) for message in page["messages"]]
        assert whos[0] == ("You", "dashboard") and whos[-1][0] == "Dom"
        after = (await client.get(f"/api/talk?after={page['messages'][-1]['id']}", headers=SAME_SITE)).json()
        assert after["messages"] == [] and after["typing"] is False

        await flush(writer)
        page = (await client.get("/api/talk?trace=1", headers=SAME_SITE)).json()
        [turn] = [item for item in page["turns"] if item["kind"] == "turn"]
        trace = (await client.get(f"/api/traces/turn/{turn['turn_id']}", headers=SAME_SITE)).json()
        assert trace["parts"][0]["turn_id"] == turn["turn_id"]
        export = await client.get(f"/api/traces/turn/{turn['turn_id']}/export", headers=SAME_SITE)
        assert export.headers["content-disposition"].startswith("attachment;")
        assert json.loads(export.text)["parts"]
        ranged = await client.get("/api/traces/export?from=2000-01-01", headers=SAME_SITE)
        assert json.loads(ranged.text)["parts"] and json.loads(ranged.text)["truncated"] is False

        turns = (await client.get("/api/system/turns")).json()
        assert turns["turns"][0]["has_trace"] is True and turns["owner_chat"] == str(OWNER)


async def test_the_talk_routes_refuse_what_they_should(traced, browser):
    app, _ = traced()
    async with browser(app) as client:
        refused = await client.post("/api/talk/send", json={"text": "/status"}, headers=JSON)
        assert refused.status_code == 400 and "Telegram" in refused.json()["detail"]
        assert (await client.post("/api/talk/send", json={"text": "  "}, headers=JSON)).status_code == 400
        bad_photo = await client.post("/api/talk/send", json={"text": "", "photo": "!!", "mime": "image/png"}, headers=JSON)
        assert bad_photo.status_code == 400
        assert (await client.post("/api/talk/send", json={"text": "hi"}, headers={"Content-Type": "application/json"})).status_code == 403
        # Another site can't read the conversation or a trace.
        assert (await client.get("/api/talk", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await client.get("/api/traces/turn/1", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await client.get("/api/traces/turn/99", headers=SAME_SITE)).status_code == 404
        assert (await client.get("/api/traces/export?from=yesterday", headers=SAME_SITE)).status_code == 400
    assert not [call for call in app.bot.calls if call[0] == "send"]


async def test_telegram_refusing_the_mirror_is_a_plain_error(traced, browser):
    app, _ = traced()

    async def refuse(*args, **kwargs):
        raise RuntimeError("telegram down")

    app.bot.send_message = refuse
    async with browser(app) as client:
        response = await client.post("/api/talk/send", json={"text": "hello"}, headers=JSON)
    assert response.status_code == 502 and "Telegram" in response.json()["detail"]
    assert await app.store.get_recent_chat_messages(OWNER) == ([], None)


async def test_without_a_trace_store_the_trace_routes_say_so(make_app, browser):
    app = make_app()
    async with browser(app) as client:
        assert (await client.get("/api/traces/turn/1", headers=SAME_SITE)).status_code == 503
        assert (await client.get("/api/talk?trace=1", headers=SAME_SITE)).json()["traced"] is False


async def test_forgetting_a_chat_with_its_history_deletes_its_traces(traced, browser):
    app, writer = traced()
    writer.store.insert(*_part(datetime.now(timezone.utc), chat=str(GROUP), turn_id=7), max_bytes=10**9)
    await app.store.set_chat_auth_status(GROUP, "approved")
    await app.store.set_chat_reachable(GROUP, False, "kicked")
    async with browser(app) as client:
        response = await client.post(
            "/api/chats/forget", json={"chat_id": GROUP, "delete_history": True, "confirm": True}, headers=JSON,
        )
    assert response.status_code == 200, response.text
    assert await writer.store.traced([7]) == set()


# --- review fixes (PR #99) ------------------------------------------------------------------------------------------


def _escaped(text: str) -> str:
    """Every character as a JSON \\uXXXX escape, as a model may write a tool argument."""
    return "".join(f"\\u{ord(char):04x}" for char in text)


async def test_every_env_value_is_redacted_through_the_writer_the_store_and_the_export(make_app, browser, monkeypatch):
    app = make_app()
    weird, accented, short = 'pa"ss\\wd-9', "pässwörd-42", "zq7k"
    token = ":".join(("7" * 9, "B" * 35))
    (app.store.home / ".env").write_text(
        f"WEIRD_VALUE={weird}\nACCENTED=\"{accented}\"\nSHORT_ONE={short}\nTELEGRAM_BOT_TOKEN={token}\nTINY=ab\n"
    )
    monkeypatch.setenv("JEV_API_KEY", "only-in-the-environment-77")
    writer = tracer.open_writer(app.store.home, lambda: app.config)
    app.traces = writer
    trace = turn_log.TurnTrace(chat_id=OWNER, scope="owner_dm")
    trace.add_event("tool", {
        "name": "read_file",
        "arguments": json.dumps({"path": weird, "also": accented}),  # JSON-escaped forms
        "result": f"x{short}y {token} only-in-the-environment-77",
        "raw": '{"q": "' + _escaped(accented) + '", "w": "' + _escaped(weird) + '"}',  # \uXXXX forms
        "tiny": "ab stays: too short to redact",
    })
    await writer.write(trace)
    stored = "".join(json.dumps(row["body"], ensure_ascii=False) for row in rows(writer))
    async with browser(app) as client:
        exported = (await client.get("/api/traces/export", headers=SAME_SITE)).text
    for text in (stored, exported):
        for secret in (weird, accented, short, token, "only-in-the-environment-77"):
            for form in {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=True)[1:-1], _escaped(secret)}:
                assert form not in text, form
        assert "ab stays" in text
    writer.store.close()


async def test_forgetting_a_chat_removes_its_aliases_queued_and_in_flight_traces(traced, browser):
    app, writer = traced()
    old, new = -1001, GROUP
    await app.store.set_chat_auth_status(old, "approved", title="Old group")
    await app.store.migrate_chat(old, new, title="The Group")
    await app.store.set_chat_reachable(new, False, "kicked")
    writer.store.insert(*_part(datetime.now(timezone.utc), chat=str(old), turn_id=1), max_bytes=10**9)
    writer.store.insert(*_part(datetime.now(timezone.utc), chat=str(new), turn_id=2), max_bytes=10**9)
    queued = turn_log.TurnTrace(chat_id=new)
    queued.add_event("note", {"text": "queued"})
    writer.submit(queued)
    in_flight = turn_log.TurnTrace(chat_id=old)  # started before the forget, written after it
    in_flight.add_event("note", {"text": "in flight"})
    other = turn_log.TurnTrace(chat_id=OWNER)
    other.add_event("note", {"text": "someone else's"})
    writer.submit(other)
    async with browser(app) as client:
        response = await client.post(
            "/api/chats/forget", json={"chat_id": new, "delete_history": True, "confirm": True}, headers=JSON,
        )
    assert response.status_code == 200, response.text
    assert await writer.write(in_flight) is None
    assert await writer.store.traced([1, 2]) == set()
    assert writer._queue.qsize() == 1  # only the other chat's trace is left to write
    await flush(writer)
    assert {row["chat_id"] for row in rows(writer)} == {str(OWNER)}
    later = turn_log.TurnTrace(chat_id=new)  # the chat comes back later: its new traces are kept
    later.add_event("note", {"text": "back again"})
    assert await writer.write(later) is not None


async def test_a_failed_trace_deletion_is_reported_not_a_plain_success(traced, browser):
    app, writer = traced()
    await app.store.set_chat_auth_status(GROUP, "approved")
    await app.store.set_chat_reachable(GROUP, False, "kicked")

    async def broken(chat_ids):
        raise sqlite3.OperationalError("disk I/O error")

    writer.forget = broken
    async with browser(app) as client:
        response = await client.post(
            "/api/chats/forget", json={"chat_id": GROUP, "delete_history": True, "confirm": True}, headers=JSON,
        )
    assert response.status_code == 500 and "debug traces could not be deleted" in response.json()["detail"]


async def test_the_trace_files_are_owner_only(tmp_path):
    path = tmp_path / "traces.db"
    path.write_bytes(b"")
    os.chmod(path, 0o644)  # an existing file, made before this release
    store = TraceStore(path)
    store.insert(*_part(datetime.now(timezone.utc)), max_bytes=10**9)
    for name in ("traces.db", "traces.db-wal", "traces.db-shm"):
        item = tmp_path / name
        assert item.exists() and stat.S_IMODE(item.stat().st_mode) == 0o600, name
    store.close()


def test_work_after_a_turn_that_finishes_first_still_joins_its_turn():
    handed = []
    turn_log.set_sink(handed.append)
    try:
        parent = turn_log.TurnTrace(chat_id=OWNER, scope="owner_dm")
        with turn_log.track(parent):
            with turn_log.follow_up("studio", OWNER, "owner_dm"):
                turn_log.event("studio", requested={"image": "a park"})
        assert handed == []  # the studio part waits for its turn's row
        parent.row_id = 41
        parent.add_event("note", {"text": "turn"})
        turn_log.submit(parent)
    finally:
        turn_log.set_sink(None)
    assert [trace.label for trace in handed] == ["turn", "studio"]
    assert tracer.encode(handed[1], [])["turn_id"] == 41 and tracer.encode(handed[1], [])["kind"] == "after"


async def test_talk_polls_traces_with_their_own_cursor(traced, browser):
    app, writer = traced()
    old = turn_log.TurnTrace(chat_id=OWNER, scope="owner_dm", started=time.time() - 3600)  # a long, slow turn
    old.row_id = 5
    old.add_event("note", {"text": "finished late"})
    async with browser(app) as client:
        first = (await client.get("/api/talk?trace=1&after=0&trace_after=0", headers=SAME_SITE)).json()
        assert first["turns"] == []
        await writer.write(old)
        polled = (await client.get("/api/talk?trace=1&after=0&trace_after=0", headers=SAME_SITE)).json()
        assert [part["turn_id"] for part in polled["turns"]] == [5]
        again = (await client.get(f"/api/talk?trace=1&after=0&trace_after={polled['turns'][0]['id']}", headers=SAME_SITE)).json()
        assert again["turns"] == []
