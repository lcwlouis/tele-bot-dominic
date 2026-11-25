"""Tests for v0.8.4 W3: chat reachability, progressive recent messages, and Telegram card cleanup."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import GetChat, GetChatMember

from aiogram.types import CallbackQuery, Chat, Message, User

from dom.models import StoredMessage
from dom.reachability import check_reachability
from dom.telegram import owner
from dom.telegram.handlers import handle_callback, handle_message
from test_web_auth import ORIGIN
from tghelpers import NOW, OWNER, tg

FRIEND_DM = 2002
GROUP_OK = -5001
GROUP_KICKED = -5002
PENDING_STALE = 2003
IGNORED_GONE = 2004

JSON = {"Origin": ORIGIN, "Content-Type": "application/json"}


def _callback(chat_id: int, user_id: int, data: str, message_id: int = 50) -> CallbackQuery:
    return CallbackQuery(
        id=f"cb-{message_id}-{data}",
        from_user=User(id=user_id, is_bot=False, first_name="Owner" if user_id == OWNER else "Friend"),
        chat_instance="inst",
        data=data,
        message=Message(
            message_id=message_id,
            date=NOW,
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "group"),
        ),
    )


async def _record(app, chat_id: int, text: str, msg_id: int, role: str = "user") -> None:
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), role=role, text=text, msg_id=msg_id,
        is_bot=(role == "assistant"), sender_name="Alex",
        created_at=app.dispatch.now().isoformat(),
        visibility_scope=app.store.visibility_for(chat_id),
    ))


# ---- 1. Reachability check & API ----------------------------------------------------

async def test_reachability_check_classifies_chats_and_preserves_history(make_app):
    app = make_app()
    app.bot_id = 777
    await app.store.set_chat_auth_status(OWNER, "approved", title="Owner")
    await app.store.set_chat_auth_status(FRIEND_DM, "approved", title="Alex")
    await app.store.set_chat_auth_status(GROUP_OK, "approved", title="Active Group")
    await app.store.set_chat_auth_status(GROUP_KICKED, "approved", title="Old Group")
    await app.store.set_chat_auth_status(PENDING_STALE, "pending_approval", title="Stale Stranger")
    await app.store.set_chat_auth_status(IGNORED_GONE, "ignored", title="Gone Ignored")
    await _record(app, GROUP_KICKED, "Remember the mountain hike", 10)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    app.sleep = fake_sleep

    async def fake_get_chat(chat_id: int):
        if chat_id == PENDING_STALE:
            raise TelegramBadRequest(GetChat(chat_id=chat_id), "Bad Request: chat not found")
        if chat_id == IGNORED_GONE:
            raise TelegramForbiddenError(GetChat(chat_id=chat_id), "Forbidden: bot was blocked by the user")
        return SimpleNamespace(id=chat_id)

    async def fake_get_chat_member(chat_id: int, user_id: int):
        assert user_id == 777
        if chat_id == GROUP_KICKED:
            return SimpleNamespace(status="kicked")
        return SimpleNamespace(status="member")

    app.bot.get_chat = fake_get_chat
    app.bot.get_chat_member = fake_get_chat_member

    summary = await check_reachability(app, gap=0.25)
    assert summary == {"checked": 6, "unreachable": 3, "restored": 0, "errors": 0}
    assert len(sleeps) == 5  # spread out between calls

    approved_ids = {int(c["chat_id"]) for c in await app.store.list_approved_chats()}
    assert approved_ids == {OWNER, FRIEND_DM, GROUP_OK}
    assert await app.store.list_pending_chats() == []  # stale pending moved out

    unreachable = {int(c["chat_id"]): c for c in await app.store.list_unreachable_chats()}
    assert set(unreachable) == {GROUP_KICKED, PENDING_STALE, IGNORED_GONE}
    assert unreachable[GROUP_KICKED]["authorization_status"] == "approved"
    assert unreachable[GROUP_KICKED]["unreachable_reason"] == "kicked"
    assert unreachable[PENDING_STALE]["authorization_status"] == "pending_approval"
    assert unreachable[IGNORED_GONE]["authorization_status"] == "ignored"

    # History is never deleted and remains searchable by the owner.
    hits = await app.store.search_chat_history("mountain hike", str(OWNER))
    assert len(hits) == 1 and hits[0]["chat_id"] == str(GROUP_KICKED)


async def test_reachability_transient_error_leaves_chat_unchanged_and_retry_after_retries(make_app):
    app = make_app()
    app.bot_id = 777
    await app.store.set_chat_auth_status(FRIEND_DM, "approved", title="Alex")
    await app.store.set_chat_auth_status(GROUP_OK, "approved", title="Team")

    attempts = {"dm": 0}

    async def fake_get_chat(chat_id: int):
        attempts["dm"] += 1
        if attempts["dm"] == 1:
            raise TelegramRetryAfter(GetChat(chat_id=chat_id), "Flood", retry_after=1)
        return SimpleNamespace(id=chat_id)

    async def fake_get_chat_member(chat_id: int, user_id: int):
        raise RuntimeError("transient network drop")

    app.bot.get_chat = fake_get_chat
    app.bot.get_chat_member = fake_get_chat_member

    summary = await check_reachability(app, gap=0)
    assert summary == {"checked": 1, "unreachable": 0, "restored": 0, "errors": 1}
    assert attempts["dm"] == 2
    assert await app.store.list_unreachable_chats() == []


async def test_incoming_message_restores_unreachable_chat_with_prior_status(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(FRIEND_DM, "approved", title="Alex")
    await app.store.set_chat_reachable(FRIEND_DM, False, "chat not found")
    assert FRIEND_DM not in {int(c["chat_id"]) for c in await app.store.list_approved_chats()}

    msg = tg(chat_id=FRIEND_DM, sender=FRIEND_DM, text="Hey Dom!", msg_id=11, chat_type="private")
    await handle_message(app, msg)

    assert FRIEND_DM in {int(c["chat_id"]) for c in await app.store.list_approved_chats()}
    assert await app.store.list_unreachable_chats() == []


async def test_reachability_api_hide_and_forget_with_confirm(make_app, browser):
    app = make_app()
    app.bot_id = 777
    await app.store.set_chat_auth_status(OWNER, "approved", title="Owner")
    await app.store.set_chat_auth_status(GROUP_KICKED, "approved", title="Old Club")
    await app.store.set_chat_auth_status(PENDING_STALE, "pending_approval", title="Ghost")
    await _record(app, GROUP_KICKED, "Secret pasta recipe", 20)
    await _record(app, PENDING_STALE, "Hello from ghost", 21)

    async def fake_get_chat(chat_id: int):
        if chat_id == PENDING_STALE:
            raise TelegramBadRequest(GetChat(chat_id=chat_id), "Bad Request: chat not found")
        return SimpleNamespace(id=chat_id)

    async def fake_get_chat_member(chat_id: int, user_id: int):
        return SimpleNamespace(status="left")

    app.bot.get_chat = fake_get_chat
    app.bot.get_chat_member = fake_get_chat_member

    async with browser(app) as client:
        # Unauthenticated is rejected
        assert (await client.get("/api/chats/unreachable", headers={"Authorization": ""})).status_code == 403

        # On-demand check
        ran = await client.post("/api/chats/reachability", headers=JSON, json={})
        assert ran.status_code == 200
        body = ran.json()
        assert body["summary"]["unreachable"] == 2
        assert {c["id"] for c in body["unreachable"]} == {GROUP_KICKED, PENDING_STALE}

        listed = (await client.get("/api/chats/unreachable")).json()
        assert {c["id"] for c in listed["unreachable"]} == {GROUP_KICKED, PENDING_STALE}

        # Hide removes from default list, keeps history searchable
        hid = await client.post("/api/chats/hide", headers=JSON, json={"chat_id": PENDING_STALE, "hidden": True})
        assert hid.status_code == 200
        assert {c["id"] for c in hid.json()["unreachable"]} == {GROUP_KICKED}
        with_hidden = (await client.get("/api/chats/unreachable?include_hidden=true")).json()
        assert {c["id"] for c in with_hidden["unreachable"]} == {GROUP_KICKED, PENDING_STALE}

        # Cannot hide or forget owner DM
        assert (await client.post("/api/chats/hide", headers=JSON, json={"chat_id": OWNER})).status_code == 409
        assert (await client.post("/api/chats/forget", headers=JSON, json={"chat_id": OWNER})).status_code == 409

        # Forget with delete_history=True requires confirm=True
        bad = await client.post("/api/chats/forget", headers=JSON, json={"chat_id": GROUP_KICKED, "delete_history": True})
        assert bad.status_code == 422

        # Forget without delete_history removes chat row but keeps history searchable by owner
        forgot = await client.post("/api/chats/forget", headers=JSON, json={"chat_id": GROUP_KICKED})
        assert forgot.status_code == 200
        assert forgot.json()["unreachable"] == []
        search_hits = await app.store.search_chat_history("pasta recipe", str(OWNER))
        assert len(search_hits) == 1

        # Forget WITH delete_history=True and confirm=True deletes history too
        forgot_all = await client.post(
            "/api/chats/forget", headers=JSON,
            json={"chat_id": PENDING_STALE, "delete_history": True, "confirm": True},
        )
        assert forgot_all.status_code == 200
        assert await app.store.search_chat_history("Hello from ghost", str(OWNER)) == []


# ---- 2. Recent messages API (newest first + cursor pagination) ----------------------

async def test_recent_messages_newest_first_with_cursor_pagination(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(OWNER, "approved", title="Owner")
    for idx in range(1, 6):
        await _record(app, OWNER, f"Message {idx}", idx, role="user" if idx % 2 else "assistant")

    async with browser(app) as client:
        page1 = (await client.get(f"/api/chats/{OWNER}/messages?limit=2")).json()
        assert [m["text"] for m in page1["messages"]] == ["Message 5", "Message 4"]
        assert page1["has_more"] is True
        assert page1["next_before"] == page1["messages"][-1]["id"]

        page2 = (await client.get(f"/api/chats/{OWNER}/messages?limit=2&before={page1['next_before']}")).json()
        assert [m["text"] for m in page2["messages"]] == ["Message 3", "Message 2"]
        assert page2["has_more"] is True

        page3 = (await client.get(f"/api/chats/{OWNER}/messages?limit=2&before={page2['next_before']}")).json()
        assert [m["text"] for m in page3["messages"]] == ["Message 1"]
        assert page3["has_more"] is False and page3["next_before"] is None


# ---- 3. Telegram cards: Close button + buttons cleared when used or stale -----------

async def test_telegram_cards_have_close_button_and_clear_on_close_or_use(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(OWNER, "approved", title="Owner")
    await app.store.set_chat_auth_status(GROUP_OK, "approved", title="Book Club")

    status_view = await owner.render_status(app)
    assert status_view.markup.inline_keyboard[-1][0].callback_data == "card:close"

    chats_view = await owner.render_chats(app)
    assert chats_view.markup.inline_keyboard[-1][0].callback_data == "card:close"

    # Non-owner cannot close
    stranger_close = _callback(OWNER, FRIEND_DM, "card:close", message_id=50)
    await handle_callback(app, stranger_close)
    assert app.bot.of("markup") == []

    # Owner tapping Close clears buttons
    owner_close = _callback(OWNER, OWNER, "card:close", message_id=50)
    await handle_callback(app, owner_close)
    edits = app.bot.of("markup")
    assert edits and edits[-1][3] is None

    # Stale override button clears keyboard
    app.bot.calls.clear()
    stale_wake = _callback(OWNER, OWNER, "wake:999999", message_id=51)
    await handle_callback(app, stale_wake)
    edits = app.bot.of("markup")
    assert edits and edits[-1][3] is None

    # Using a /preset card clears its buttons
    app.bot.calls.clear()
    await owner.show_presets(app)
    preset_msg = app.bot.calls[-1][3]["reply_markup"]
    assert preset_msg.inline_keyboard[-1][0].callback_data == "card:close"

    app.bot.calls.clear()
    pick = _callback(OWNER, OWNER, "preset:all", message_id=52)
    await handle_callback(app, pick)
    edits = app.bot.of("markup")
    assert edits and edits[-1][3] is None

    # Stale approve button clears keyboard
    app.bot.calls.clear()
    stale_approve = _callback(OWNER, OWNER, "approve:999999", message_id=53)
    await handle_callback(app, stale_approve)
    edits = app.bot.of("markup")
    assert edits and edits[-1][3] is None
