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


async def test_search_for_friend_stays_in_own_chat_even_with_mislabeled_rows(store):
    await store.record_new_message(row(333, 1, f"chat:{FRIEND}", "pineapple from another chat"))
    await store.record_new_message(row(333, 2, "global_public", "pineapple public label"))
    await store.record_new_message(row(FRIEND, 3, "admin_private", "pineapple owner secret"))
    await store.record_new_message(row(FRIEND, 4, f"chat:{FRIEND}", "pineapple mine"))
    hits = await store.search_chat_history("pineapple", str(FRIEND))
    assert [h["text"] for h in hits] == ["pineapple mine"]


async def test_owner_search_covers_every_chat(store):
    await store.record_new_message(row(333, 1, "chat:333", "pineapple one"))
    await store.record_new_message(row(OWNER, 2, "owner_private", "pineapple two"))
    assert len(await store.search_chat_history("pineapple", str(OWNER))) == 2


async def test_chat_participant_names_come_only_from_what_this_chat_saw(store):
    group, old_group, other = "-100200", "-200", "-300"

    def said(chat_id, msg_id, uid, name, handle, **kw):
        return StoredMessage(
            chat_id=chat_id, msg_id=msg_id, visibility_scope=f"chat:{chat_id}", text="hi", sender_user_id=uid,
            sender_name=name, sender_handle=handle, created_at=f"2026-01-01T00:00:{msg_id:02d}+00:00", **kw,
        )

    await store.record_new_message(said(old_group, 3, 333, "Olga", ""))  # before the group became a supergroup
    await store.set_chat_title(old_group, "The Group")
    assert (await store.migrate_chat(old_group, group))[0] == "migrated"
    await store.record_new_message(said(group, 1, FRIEND, "Fran", "@newbie"))
    await store.record_new_message(said(group, 2, FRIEND, "Fran", "@newbie"))
    await store.record_new_message(said(group, 4, 444, "Dana Deleted", ""))
    await store.record_message_delete(group, [4])
    await store.record_new_message(said(group, 5, 555, "Priya Private", ""))
    await store._run(lambda: store._conn.execute("UPDATE messages SET visibility_scope = 'owner_private' WHERE msg_id = 5"))
    await store.record_new_message(said(group, 6, 999, "Dom", "@dombot", is_bot=True, role="assistant"))
    await store.record_new_message(said(other, 7, 777, "Otto", ""))
    # Fran's current Telegram name is visible in the group too; an alias she used elsewhere is not looked up.
    await store.resolve_person(FRIEND, "@inviter", "Fran Q")

    people = await store.chat_participant_names(group)
    assert sorted(sorted(names) for names in people) == [["@inviter", "@newbie", "Fran", "Fran Q"], ["Olga"]]


async def test_the_window_marks_the_owners_own_messages_outside_his_dm(store):
    group = "-100"

    def said(chat_id, msg_id, uid, **kw):
        return StoredMessage(
            chat_id=chat_id, msg_id=msg_id, visibility_scope=f"chat:{chat_id}", text="hi", sender_user_id=uid,
            created_at=f"2026-01-01T00:00:{msg_id:02d}+00:00", **kw,
        )

    await store.record_new_message(said(group, 1, OWNER))
    await store.record_new_message(said(group, 2, FRIEND))
    await store.record_new_message(said(group, 3, OWNER, role="assistant", is_bot=True))
    xml = store.format_chat_window_xml(group, await store.get_recent_chat_window(group))
    lines = [line for line in xml.splitlines() if "<msg " in line]
    assert [('owner="true"' in line) for line in lines] == [True, False, False]

    await store.record_new_message(said(str(OWNER), 4, OWNER))
    assert 'owner="true"' not in store.format_chat_window_xml(str(OWNER), await store.get_recent_chat_window(str(OWNER)))
