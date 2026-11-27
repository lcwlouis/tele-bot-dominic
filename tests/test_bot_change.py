"""A new bot token restarts Telegram's message ids, so read markers and memory cursors start over (v0.8.6 W8)."""
import tempfile
import unittest
from pathlib import Path

from dom.models import StoredMessage
from dom.store.store import Store

OWNER = 111
CHAT = str(OWNER)
DM = "222"
GROUP = "-100"


def msg(chat_id: str, msg_id: int) -> StoredMessage:
    return StoredMessage(
        chat_id=chat_id, msg_id=msg_id, text=f"m{msg_id}", visibility_scope=f"chat:{chat_id}" if chat_id != CHAT else "owner_private",
        sender_handle="friend", sender_name="Friend", role="user", created_at="2026-11-27T10:00:00+00:00",
    )


class BotChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name), owner_id=OWNER)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def rows(self, chat_id: str, ids):
        """Save rows exactly as an old database holds them, with no era noticing the jump between them."""
        conn = self.store._conn
        conn.execute("INSERT INTO chats (chat_id, chat_type, visibility_scope, authorization_status, updated_at) "
                     "VALUES (?, 'private', ?, 'approved', 'x') ON CONFLICT(chat_id) DO NOTHING",
                     (chat_id, "owner_private" if chat_id == CHAT else f"chat:{chat_id}"))
        for msg_id in ids:
            conn.execute(
                "INSERT INTO messages (chat_id, msg_id, role, visibility_scope, text, is_bot, created_at, updated_at) "
                "VALUES (?, ?, 'user', ?, ?, 0, 'x', 'x')",
                (chat_id, msg_id, "owner_private" if chat_id == CHAT else f"chat:{chat_id}", f"m{msg_id}"),
            )

    def marks(self, chat_id: str, read: int, cursor: int, topic: int | None = None):
        conn = self.store._conn
        conn.execute("UPDATE chats SET last_read_msg_id = ?, memory_cursor_msg_id = ? WHERE chat_id = ?", (read, cursor, chat_id))
        if topic is not None:
            conn.execute("INSERT INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) VALUES (?, 0, ?, 'x')",
                         (chat_id, topic))

    def state(self, chat_id: str):
        return tuple(self.store._conn.execute(
            "SELECT last_read_msg_id, memory_cursor_msg_id, era_row FROM chats WHERE chat_id = ?", (chat_id,)).fetchone())

    async def unread(self, chat_id: str) -> int:
        return (await self.store.get_unread(chat_id))[0]

    async def memory_ids(self, chat_id: str) -> list[int]:
        return [m.msg_id for m in await self.store.get_memory_batch(chat_id, 1000)]

    def real_shape(self):
        """An instance that switched bots before it stored a bot id: the owner DM holds the old bot's rows then the
        new bot's, a group has only old rows, and a friend's DM has both."""
        self.rows(CHAT, [*range(3000, 3503), *range(1, 75)])
        self.marks(CHAT, 63, 3502, topic=63)  # the read marker was already fixed by hand; the cursor was not
        self.rows(GROUP, range(2200, 2308))
        self.marks(GROUP, 2307, 2300)
        self.rows(DM, [*range(3300, 3498), *range(1, 6)])
        self.marks(DM, 3497, 3434, topic=3497)

    async def test_a_switch_made_before_any_bot_id_was_stored_is_repaired_from_the_data(self):
        self.real_shape()
        self.assertEqual(await self.store.adopt_bot(2002), 2)  # the owner DM and the friend's DM
        self.assertEqual(await self.store.kv_get("bot_id"), "2002")
        # The owner DM: era from the first new row. The fixed marker 63 stays; the old cursor starts over.
        self.assertEqual(self.state(CHAT)[:2], (63, 0))
        self.assertEqual(await self.unread(CHAT), 11)  # 64..74
        self.assertEqual(await self.memory_ids(CHAT), list(range(1, 75)))  # every new row, no old one
        self.assertEqual(await self.store.count_new_for_memory(CHAT), 74)
        # The friend's DM: both its old marker and cursor are above every new id.
        self.assertEqual(self.state(DM)[:2], (0, 0))
        self.assertEqual(await self.unread(DM), 5)
        self.assertEqual(await self.memory_ids(DM), [1, 2, 3, 4, 5])
        # A chat that only has the old bot's rows keeps its history exactly as it was.
        self.assertEqual(self.state(GROUP), (2307, 2300, 0))
        self.assertEqual(await self.unread(GROUP), 0)
        self.assertEqual(await self.memory_ids(GROUP), list(range(2301, 2308)))
        topics = self.store._conn.execute("SELECT chat_id, last_read_msg_id FROM chat_topic_reads ORDER BY chat_id").fetchall()
        self.assertEqual([tuple(t) for t in topics], [(CHAT, 63), (DM, 0)])

    async def test_the_memory_cursor_moves_on_in_the_new_era_and_the_repair_is_noted_once(self):
        self.real_shape()
        await self.store.adopt_bot(2002)
        await self.store.advance_memory_cursor(CHAT, 74)
        self.assertEqual(await self.store.count_new_for_memory(CHAT), 0)
        before = [self.state(c) for c in (CHAT, DM, GROUP)]
        self.assertEqual(await self.store.adopt_bot(2002), 0)  # a second start changes nothing
        self.assertEqual([self.state(c) for c in (CHAT, DM, GROUP)], before)
        events = self.store._conn.execute("SELECT text FROM messages WHERE role = 'event'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn("2 chats", events[0][0])
        self.assertTrue((Path(self._tmp.name) / "transcripts" / f"{CHAT}.jsonl").exists())

    async def test_first_start_with_one_bot_only_stores_the_id(self):
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3434, topic=3497)
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(await self.store.kv_get("bot_id"), "1001")
        self.assertEqual(self.state(CHAT), (3497, 3434, 0))
        self.assertEqual(await self.unread(CHAT), 5)  # 3498..3502, as before
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'event'").fetchone()[0], 0)

    async def test_a_normal_restart_with_the_same_bot_changes_nothing(self):
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3434)
        await self.store.adopt_bot(1001)
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(self.state(CHAT), (3497, 3434, 0))

    async def test_a_changed_bot_id_starts_every_chat_over_before_any_message_arrives(self):
        await self.store.adopt_bot(1001)
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3502, topic=3497)
        self.rows(GROUP, range(2200, 2308))
        self.marks(GROUP, 2307, 2300)
        self.assertEqual(await self.store.adopt_bot(2002), 2)
        for chat in (CHAT, GROUP):
            self.assertEqual(self.state(chat)[:2], (0, 0))
            self.assertEqual(await self.unread(chat), 0)  # the old history is not suddenly unread
            self.assertEqual(await self.memory_ids(chat), [])
        for msg_id in (64, 65, 70):  # the real shape: what the new bot receives
            await self.store.record_new_message(msg(CHAT, msg_id))
        count, newest = await self.store.get_unread(CHAT)
        self.assertEqual((count, newest.msg_id), (3, 70))
        self.assertEqual(await self.memory_ids(CHAT), [64, 65, 70])
        await self.store.mark_chat_read(CHAT, 70)
        self.assertEqual(await self.unread(CHAT), 0)
        await self.store.record_new_message(msg(CHAT, 71))
        self.assertEqual(await self.unread(CHAT), 1)
        # An id above the old ones counts too (it is in the era by arrival, not by number).
        await self.store.record_new_message(msg(GROUP, 4000))
        self.assertEqual(await self.unread(GROUP), 1)
        self.assertEqual(await self.memory_ids(GROUP), [4000])

    async def test_the_first_message_from_a_new_bot_starts_its_chats_era_as_it_arrives(self):
        await self.store.adopt_bot(1001)
        for msg_id in range(3000, 3503):
            await self.store.record_new_message(msg(CHAT, msg_id))
        await self.store.mark_chat_read(CHAT, 3502)
        await self.store.advance_memory_cursor(CHAT, 3502)
        for msg_id in (3, 4):  # the new bot's ids restart; no restart of Dom saw the switch
            await self.store.record_new_message(msg(CHAT, msg_id))
        self.assertEqual(self.state(CHAT)[:2], (0, 0))
        self.assertEqual(await self.unread(CHAT), 2)
        self.assertEqual(await self.memory_ids(CHAT), [3, 4])
        self.assertEqual(await self.store.adopt_bot(1001), 0)  # nothing left for a restart to repair

    async def test_messages_saved_a_little_out_of_order_are_not_a_new_bot(self):
        await self.store.adopt_bot(1001)
        for msg_id in (100, 102, 101, 103):
            await self.store.record_new_message(msg(GROUP, msg_id))
        self.assertEqual(self.state(GROUP)[2], 0)
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(self.state(GROUP)[2], 0)
