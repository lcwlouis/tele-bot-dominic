"""A new bot token restarts Telegram's message ids, so the read markers have to start over (v0.8.6 W8)."""
import tempfile
import unittest
from pathlib import Path

from dom.models import StoredMessage
from dom.store.store import Store

OWNER = 111
CHAT = str(OWNER)


def msg(msg_id: int, **kw) -> StoredMessage:
    return StoredMessage(
        chat_id=kw.pop("chat_id", CHAT), msg_id=msg_id, text=f"m{msg_id}", visibility_scope="owner_private",
        sender_handle="owner", sender_name="Owner", role="user", created_at="2026-11-27T10:00:00+00:00", **kw,
    )


class BotChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name), owner_id=OWNER)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    async def _old_bot(self):
        assert await self.store.adopt_bot(1001) is False
        for msg_id in (3490, 3497):
            await self.store.record_new_message(msg(msg_id))
        await self.store.mark_chat_read(CHAT, 3497)
        await self.store.mark_chat_read(CHAT, 3497, thread_id=5)

    async def test_first_start_only_stores_the_id(self):
        await self.store.record_new_message(msg(3497))
        await self.store.mark_chat_read(CHAT, 3497)
        self.assertFalse(await self.store.adopt_bot(1001))
        self.assertEqual(await self.store.kv_get("bot_id"), "1001")
        self.assertEqual(await self.store.get_unread(CHAT), (0, None))
        self.assertEqual(await self.store.kv_get("bot_switch_row"), None)

    async def test_same_bot_on_restart_changes_nothing(self):
        await self._old_bot()
        self.assertFalse(await self.store.adopt_bot(1001))
        await self.store.record_new_message(msg(64))
        self.assertEqual(await self.store.get_unread(CHAT), (0, None))  # 64 is below the marker, as before
        rows = self.store._conn.execute("SELECT last_read_msg_id FROM chats WHERE chat_id = ?", (CHAT,)).fetchone()
        self.assertEqual(rows[0], 3497)

    async def test_new_bot_messages_count_as_unread_after_a_change(self):
        await self._old_bot()
        self.assertTrue(await self.store.adopt_bot(2002))
        self.assertEqual(await self.store.kv_get("bot_id"), "2002")
        self.assertEqual(await self.store.get_unread(CHAT), (0, None))  # the old history is not suddenly unread
        for msg_id in (64, 65, 70):
            await self.store.record_new_message(msg(msg_id))
        count, newest = await self.store.get_unread(CHAT)
        self.assertEqual((count, newest.msg_id), (3, 70))
        self.assertTrue(await self.store.has_unread_human(CHAT))
        # Marking read afterwards works again from the new numbering.
        await self.store.mark_chat_read(CHAT, 70)
        self.assertEqual(await self.store.get_unread(CHAT), (0, None))
        await self.store.record_new_message(msg(71))
        self.assertEqual((await self.store.get_unread(CHAT))[0], 1)

    async def test_every_chat_and_both_tables_reset(self):
        await self._old_bot()
        await self.store.record_new_message(msg(900, chat_id="-100"))
        await self.store.mark_chat_read("-100", 900)
        await self.store.adopt_bot(2002)
        chats = dict(self.store._conn.execute("SELECT chat_id, last_read_msg_id FROM chats").fetchall())
        self.assertEqual(chats[CHAT], 0)
        self.assertEqual(chats["-100"], 0)
        topics = self.store._conn.execute("SELECT last_read_msg_id FROM chat_topic_reads").fetchall()
        self.assertTrue(topics)
        self.assertTrue(all(row[0] == 0 for row in topics))

    async def test_change_is_noted_in_the_owner_dm(self):
        await self._old_bot()
        await self.store.adopt_bot(2002)
        row = self.store._conn.execute(
            "SELECT text FROM messages WHERE chat_id = ? AND role = 'event'", (CHAT,)
        ).fetchone()
        self.assertIn("bot changed", row[0])
        self.assertTrue((Path(self._tmp.name) / "transcripts" / f"{CHAT}.jsonl").exists())
