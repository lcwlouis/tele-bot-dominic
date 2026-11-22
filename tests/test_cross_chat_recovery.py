import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from dom.store import Store
from dom.store import store as storage
from test_attention import until
from test_cross_chat import approve_chat, draft, process_ready
from tghelpers import FRIEND, GROUP, OWNER


@pytest.mark.parametrize(("initial", "after_restart", "delivery"), [
    ("awaiting_approved", "queued", "sent"),
    ("awaiting_rejected", "dropped", "dropped"),
    ("awaiting_expired", "dropped", "dropped"),
    ("awaiting_pending", "awaiting_approval", "sent"),
    ("queued", "queued", "sent"),
    ("queued_expired", "dropped", "dropped"),
    ("sending", "dropped", "dropped"),
])
async def test_restart_reconciles_every_state(make_app, initial, after_restart, delivery):
    app = make_app()
    await approve_chat(app, GROUP, "Book Club")
    assert (await draft(app, text="The meeting is at noon.")).startswith("Waiting")
    [card] = await app.proposals.pending()
    [item] = await app.store.cross_chat_items()
    at = app.dispatch.now().isoformat()
    if initial in ("awaiting_approved", "awaiting_rejected"):
        # Simulate a crash after the durable decision, before Proposals.decide could notify the worker.
        decision = initial.removeprefix("awaiting_")
        assert await app.store.decide_proposal(card["id"], decision, at, app.proposals._apply)
        assert (await app.store.cross_chat_items())[0]["status"] == "awaiting_approval"
    if initial in ("queued", "queued_expired", "sending"):
        await app.proposals.decide(card["id"], True)
    if initial == "sending":
        assert await app.store.change_cross_chat(item["id"], "queued", "sending", at)
    if initial in ("awaiting_expired", "queued_expired"):
        app.dispatch.now.advance(hours=2)

    await app.dispatch.close()
    app.store.close()
    restarted = make_app()
    restarted.dispatch.now.at = app.dispatch.now()
    await restarted.dispatch.cross_chat.reconcile()
    [recovered] = await restarted.store.cross_chat_items()
    assert recovered["status"] == after_restart
    if initial == "awaiting_pending":
        await process_ready(restarted)
        assert not restarted.bot.of("send", GROUP)
        assert (await restarted.store.get_proposal(card["id"]))["status"] == "pending"
        await restarted.proposals.decide(card["id"], True)
    await process_ready(restarted)
    [settled] = await restarted.store.cross_chat_items()
    assert settled["status"] == delivery
    assert len(restarted.bot.of("send", GROUP)) == (1 if delivery == "sent" else 0)
    if initial == "sending":
        assert settled["drop_reason"] == "may not have been sent"
        events = await restarted.store.get_recent_chat_window(OWNER, 10)
        assert any("Book Club" in event.text and "The meeting is at noon." in event.text
                   and "may not have been sent" in event.text for event in events)
    if initial.endswith("expired"):
        assert settled["drop_reason"] == "expired"
        assert (await restarted.store.get_proposal(card["id"]))["status"] == "expired"


async def test_worker_finishes_in_flight_send_but_leaves_the_next_queued_on_shutdown(make_app, monkeypatch):
    app = make_app()
    await approve_chat(app, GROUP)
    for text in ("first", "second"):
        assert (await draft(app, text=text)).startswith("Waiting")
        [card] = await app.proposals.pending()
        await app.proposals.decide(card["id"], True)
    sending, finish = asyncio.Event(), asyncio.Event()
    send = app.bot.send_message

    async def delayed(*args, **kwargs):
        sending.set()
        await finish.wait()
        return await send(*args, **kwargs)

    monkeypatch.setattr(app.bot, "send_message", delayed)
    app.dispatch.cross_chat.start()
    await asyncio.wait_for(sending.wait(), 2)
    [second, first] = await app.store.cross_chat_items()
    assert (second["status"], first["status"]) == ("queued", "sending")
    closing = asyncio.create_task(app.dispatch.close())
    try:
        await until(lambda: app.dispatch._closing)
        assert not closing.done()
    finally:
        finish.set()
        await asyncio.wait_for(closing, 2)
    assert [delivery[2] for delivery in app.bot.of("send", GROUP)] == ["first"]
    assert [row["status"] for row in await app.store.cross_chat_items()] == ["queued", "sent"]


async def test_dispatch_tick_restarts_a_crashed_worker(make_app, monkeypatch):
    app = make_app()
    dispatch = app.dispatch
    await approve_chat(app, GROUP)
    assert (await draft(app)).startswith("Waiting")
    [card] = await app.proposals.pending()
    await app.proposals.decide(card["id"], True)
    tick = asyncio.Event()
    async def wait_for_tick(_):
        await tick.wait()
        tick.clear()
    dispatch.sleep = wait_for_tick

    process = dispatch.cross_chat.process_one
    failed = False
    async def fail_once():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("worker crashed")
        return await process()
    monkeypatch.setattr(dispatch.cross_chat, "process_one", fail_once)
    dispatch.start()
    original_worker = dispatch.cross_chat._task
    await until(original_worker.done)
    assert not app.bot.of("send")
    tick.set()
    await until(lambda: bool(app.bot.of("send", GROUP)))
    assert dispatch.cross_chat._task is not original_worker


@pytest.mark.parametrize("failure", ["forbidden", "uncertain", "flood", "connection_refused"])
async def test_send_retries_only_explicit_pre_delivery_failures(make_app, monkeypatch, failure):
    from aiohttp import ClientConnectorError
    from errno import ECONNREFUSED

    app = make_app()
    await approve_chat(app, GROUP)
    await draft(app)
    [card] = await app.proposals.pending()
    await app.proposals.decide(card["id"], True)
    method = SendMessage(chat_id=GROUP, text="Test")
    attempt = 0
    send = app.bot.send_message

    async def try_send(*args, **kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            if failure == "forbidden":
                raise TelegramForbiddenError(method=method, message="Forbidden")
            if failure == "uncertain":
                raise TelegramNetworkError(method=method, message="The connection closed")
            if failure == "flood":
                raise TelegramRetryAfter(method=method, message="Wait", retry_after=1)
            error = ClientConnectorError(None, ConnectionRefusedError(ECONNREFUSED, "Connection refused"))
            raise TelegramNetworkError(method=method, message="Could not connect") from error
        return await send(*args, **kwargs)

    monkeypatch.setattr(app.bot, "send_message", try_send)
    await process_ready(app)
    [item] = await app.store.cross_chat_items()
    if failure in ("flood", "connection_refused"):
        assert attempt == 2 and item["status"] == "sent"
    else:
        assert attempt == 1 and item["status"] == "dropped"
        assert item["drop_reason"] == ("chat gone" if failure == "forbidden" else "may not have been sent")


@pytest.mark.parametrize("migration_error", [False, True])
async def test_approved_delivery_follows_target_migration(make_app, monkeypatch, migration_error):
    app = make_app()
    migrated = GROUP - 10
    await approve_chat(app, GROUP, "Book Club")
    await draft(app, text="I am running late.")
    [card] = await app.proposals.pending()
    await app.proposals.decide(card["id"], True)
    if migration_error:
        send = app.bot.send_message
        async def send_or_migrate(chat_id, text, **kwargs):
            if chat_id == GROUP:
                raise TelegramMigrateToChat(
                    method=SendMessage(chat_id=chat_id, text=text), message="Migrated", migrate_to_chat_id=migrated,
                )
            return await send(chat_id, text, **kwargs)
        monkeypatch.setattr(app.bot, "send_message", send_or_migrate)
    else:
        assert await app.dispatch.migrate_chat(GROUP, migrated, "New book club") == "migrated"
    await process_ready(app)
    [item] = await app.store.cross_chat_items()
    assert item["target_chat_id"] == migrated and item["status"] == "sent"
    body = json.loads((await app.store.get_proposal(card["id"]))["body_json"])
    assert body["target_chat_id"] == migrated and body["text"] == "I am running late."
    assert not app.bot.of("send", GROUP)
    [delivery] = app.bot.of("send", migrated)
    assert delivery[2] == body["text"]


async def test_terminal_delivery_rows_are_pruned_after_thirty_days(make_app):
    app = make_app()
    await approve_chat(app, FRIEND)
    assert await app.store.add_owner_delivery("owner_note", FRIEND, "Old", "Test", app.dispatch.now())
    await process_ready(app)
    app.dispatch.now.advance(days=31)
    assert await app.store.add_owner_delivery("owner_note", FRIEND, "New", "Test", app.dispatch.now())
    await app.dispatch._prune_turn_log()
    [item] = await app.store.cross_chat_items()
    assert item["text"] == "New"


async def test_v22_proposals_migrate_intact_and_legacy_projection_still_reads(tmp_path, monkeypatch):
    kinds = ("soul_edit", "memory_fact", "skill", "plugin", "lan_address", "memory_edit")
    at = "2026-01-01T12:00:00+00:00"
    old_migrations = storage.MIGRATIONS[:-1]
    with monkeypatch.context() as old:
        old.setattr(storage, "MIGRATIONS", old_migrations)
        store = Store(tmp_path, owner_id=OWNER)
        for index, kind in enumerate(kinds, start=1):
            key = await store.create_proposal(kind, f"Example {kind}", {"example": index}, "test", at)
            assert key == index
            if index % 2 == 0:
                await store.decide_proposal(key, "rejected", at, lambda *_: None)
        store.close()

    path = tmp_path / "state.db"
    projection = "id, kind, title, body_json, status, created_at, decided_at, source"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 22
        before = conn.execute(f"SELECT {projection} FROM proposals ORDER BY id").fetchall()

    assert storage.migrate_database(path) == 23
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT {projection} FROM proposals ORDER BY id").fetchall() == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(tmp_path / "state.db.v22.bak") as prev:
        assert prev.execute(f"SELECT {projection} FROM proposals ORDER BY id").fetchall() == before

    current = Store(tmp_path, owner_id=OWNER)
    newest = await current.create_proposal("cross_chat", "Send a test message", {"item_id": 1}, "test", at)
    current.close()
    with monkeypatch.context() as old:
        old.setattr(storage, "MIGRATIONS", old_migrations)
        rollback = Store(tmp_path, owner_id=OWNER)
        try:
            row = (await rollback.pending_proposals())[-1]
            assert row["id"] == newest and row["title"] == "Send a test message"
            assert await rollback.create_proposal("skill", "Example skill", {"example": 7}, "test", at) > newest
        finally:
            rollback.close()


@pytest.mark.parametrize("change, reason", [("expiry", "expired"), ("ignore", "chat gone")])
async def test_delivery_rechecks_the_card_and_target_after_typing(make_app, change, reason):
    app = make_app()
    await approve_chat(app, GROUP)
    await draft(app)
    [card] = await app.proposals.pending()
    await app.proposals.decide(card["id"], True)

    async def while_typing(_):
        if change == "expiry":
            app.dispatch.now.advance(hours=2)
        else:
            await app.store.set_chat_auth_status(GROUP, "ignored")

    app.sleep = while_typing
    await process_ready(app)
    assert app.bot.of("action", GROUP)
    assert not app.bot.of("send", GROUP)
    [row] = await app.store.cross_chat_items()
    assert row["status"] == "dropped" and row["drop_reason"] == reason
    assert [step["status"] for step in json.loads(row["status_history"])] == [
        "awaiting_approval", "queued", "sending", "dropped",
    ]
