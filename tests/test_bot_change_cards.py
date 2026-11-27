"""After a bot change, requests and cards the old bot sent are never edited: the new bot's own messages may carry the
same numbers (v0.8.6 W8)."""
from datetime import datetime, timezone

from dom.models import StoredMessage
from tghelpers import OWNER


async def test_settling_a_request_after_a_bot_change_never_edits_a_new_bot_message(make_app):
    app = make_app()
    store = app.store
    await store.adopt_bot(1001)
    now = datetime(2026, 11, 20, tzinfo=timezone.utc).isoformat()
    for msg_id in range(1, 40):
        await store.record_new_message(StoredMessage(
            chat_id=str(OWNER), msg_id=msg_id, text=f"old {msg_id}", visibility_scope="owner_private", role="user",
            created_at=now,
        ))
    request = await store.create_desktop_permission_request(
        action="visit_site", site="example.org", detail="d", token_hash="a" * 8, created_at=now,
    )
    await store.set_desktop_permission_card(request, 12)  # the old bot's card, numbered like a new message
    secret = await store.create_secret_request(
        label="l", reason="r", target="t", target_key="k", token_hash="b" * 8, created_at=now, expires_at=now,
    )
    await store.set_secret_request_card(secret, 13)
    opener = store._conn.execute(
        "INSERT INTO noticings (day, kind, title, body, source_kind, opener_msg_id, created_at) "
        "VALUES ('d', 'idea', 't', 'b', 'owner_dm', 14, ?)", (now,)).lastrowid

    await store.adopt_bot(2002)  # a new token; the owner DM moves when the new bot first writes there
    await store.record_new_message(StoredMessage(
        chat_id=str(OWNER), msg_id=12, text="new 12", visibility_scope="owner_private", role="user", created_at=now,
    ))
    before = len(app.bot.calls)

    await app.desktop_permissions.expire_pending()
    await app.secret_requests.expire_pending()
    await app.desktop_permissions._settle_card(-12, "Expired")  # a pre-repair id can't be edited either
    await app.secret_requests._settle_card(-13, "Expired")
    await app.noticings._settle_card(-14, "Not now")
    await app.noticings._settle_card(store._conn.execute("SELECT opener_msg_id FROM noticings WHERE id = ?", (opener,)).fetchone()[0], "Not now")

    assert app.bot.calls[before:] == []  # nothing was sent to Telegram, so no new-bot message was touched
    assert store._conn.execute("SELECT status, card_message_id FROM desktop_permission_history").fetchone()[:] == ("expired", None)
    assert store._conn.execute("SELECT status, card_message_id FROM secret_requests").fetchone()[:] == ("expired", None)
