from dom.store.store import Store


async def test_set_chat_title_updates_known_chat_only(tmp_path):
    store = Store(tmp_path, 111)
    try:
        await store.get_chat_auth_status("-1009")  # known chat with no title, like an imported one
        await store.set_chat_title(-1009, "Book Club")
        await store.set_chat_title(-1777, "Stranger Group")  # unknown chat: no row is created
        await store.set_chat_title(-1009, "")  # empty titles never overwrite
        rows = dict(store._conn.execute("SELECT chat_id, title FROM chats").fetchall())
    finally:
        store.close()
    assert rows.get("-1009") == "Book Club"
    assert "-1777" not in rows
