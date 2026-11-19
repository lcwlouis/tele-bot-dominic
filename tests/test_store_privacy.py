import pytest

from dom.models import StoredMessage
from dom.store import Store

OWNER, FRIEND = 111, 222


def row(chat_id, msg_id, scope, text):
    return StoredMessage(
        chat_id=str(chat_id), msg_id=msg_id, visibility_scope=scope, text=text,
        created_at=f"2026-01-01T00:00:{msg_id:02d}+00:00",
    )


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path, owner_id=OWNER)
    yield s
    s.close()


@pytest.mark.parametrize("private_scope", ["admin_private", "owner_private"])
async def test_friend_window_excludes_private_rows(store, private_scope):
    await store.record_new_message(row(FRIEND, 1, private_scope, "owner secret"))
    await store.record_new_message(row(FRIEND, 2, f"chat:{FRIEND}", "friend talk"))
    texts = [m.text for m in await store.get_recent_chat_window(str(FRIEND), 10)]
    assert texts == ["friend talk"]


async def test_owner_window_keeps_private_rows(store):
    await store.record_new_message(row(OWNER, 1, "owner_private", "dm"))
    await store.record_new_message(row(OWNER, 2, "admin_private", "legacy dm"))
    texts = [m.text for m in await store.get_recent_chat_window(str(OWNER), 10)]
    assert texts == ["dm", "legacy dm"]
