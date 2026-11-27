"""A new bot token restarts Telegram's message ids: the old bot's rows are set aside under negative ids, and read markers
and memory cursors start over (v0.8.6 W8)."""
import json
import tempfile
import unittest
from pathlib import Path

from dom.models import StoredMessage
from dom.store.store import Store

OWNER = 111
CHAT = str(OWNER)
DM = "222"
GROUP = "-100"


def scope(chat_id: str) -> str:
    return "owner_private" if chat_id == CHAT else f"chat:{chat_id}"


def msg(chat_id: str, msg_id: int) -> StoredMessage:
    return StoredMessage(
        chat_id=chat_id, msg_id=msg_id, text=f"new {msg_id}", visibility_scope=scope(chat_id),
        sender_handle="friend", sender_name="Friend", role="user", created_at="2026-11-27T10:00:00+00:00",
    )


class BotChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name), owner_id=OWNER)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def rows(self, chat_id: str, ids, *, replies=False, media=()):
        """Save rows exactly as an old database holds them (no era noticing the jump between them)."""
        conn = self.store._conn
        conn.execute("INSERT INTO chats (chat_id, chat_type, visibility_scope, authorization_status, updated_at) "
                     "VALUES (?, 'private', ?, 'approved', 'x') ON CONFLICT(chat_id) DO NOTHING", (chat_id, scope(chat_id)))
        for msg_id in ids:
            conn.execute(
                "INSERT INTO messages (chat_id, msg_id, reply_to_msg_id, role, visibility_scope, text, media_json, is_bot, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'x', 'x')",
                (chat_id, msg_id, msg_id - 1 if replies and msg_id > 1 else None,
                 "assistant" if msg_id in media else "user", scope(chat_id), f"old {msg_id}",
                 json.dumps({"kind": "photo", "file": f"media/{chat_id}/2026-11/{msg_id}.png"}) if msg_id in media else None),
            )

    def marks(self, chat_id: str, read: int, cursor: int, topic: int | None = None):
        conn = self.store._conn
        conn.execute("UPDATE chats SET last_read_msg_id = ?, memory_cursor_msg_id = ? WHERE chat_id = ?", (read, cursor, chat_id))
        if topic is not None:
            conn.execute("INSERT INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) VALUES (?, 0, ?, 'x')",
                         (chat_id, topic))

    def state(self, chat_id: str):
        return tuple(self.store._conn.execute(
            "SELECT last_read_msg_id, memory_cursor_msg_id FROM chats WHERE chat_id = ?", (chat_id,)).fetchone())

    def ids(self, chat_id: str) -> list[int]:
        return [r[0] for r in self.store._conn.execute(
            "SELECT msg_id FROM messages WHERE chat_id = ? AND msg_id IS NOT NULL ORDER BY id", (chat_id,))]

    async def unread(self, chat_id: str) -> int:
        return (await self.store.get_unread(chat_id))[0]

    async def memory_texts(self, chat_id: str) -> list[str]:
        return [m.text for m in await self.store.get_memory_batch(chat_id, 1000)]

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
        # The owner DM: the fixed marker 63 stays; the old cursor starts over.
        self.assertEqual(self.state(CHAT), (63, 0))
        self.assertEqual(await self.unread(CHAT), 11)  # 64..74
        self.assertEqual(await self.memory_texts(CHAT), [f"old {n}" for n in range(1, 75)])
        self.assertEqual(await self.store.count_new_for_memory(CHAT), 74)
        # The friend's DM: both its old marker and cursor were above every new id.
        self.assertEqual(self.state(DM), (0, 0))
        self.assertEqual(await self.unread(DM), 5)
        self.assertEqual(len(await self.memory_texts(DM)), 5)
        # A chat that only has the old bot's rows keeps its history exactly as it was.
        self.assertEqual(self.state(GROUP), (2307, 2300))
        self.assertEqual(await self.unread(GROUP), 0)
        self.assertEqual(await self.memory_texts(GROUP), [f"old {n}" for n in range(2301, 2308)])
        topics = self.store._conn.execute("SELECT chat_id, last_read_msg_id FROM chat_topic_reads ORDER BY chat_id").fetchall()
        self.assertEqual([tuple(t) for t in topics], [(CHAT, 63), (DM, 0)])

    async def test_a_second_start_changes_nothing_and_the_repair_is_noted_once(self):
        self.real_shape()
        await self.store.adopt_bot(2002)
        await self.store.advance_memory_cursor(CHAT, 74)
        self.assertEqual(await self.store.count_new_for_memory(CHAT), 0)
        before = ([self.state(c) for c in (CHAT, DM, GROUP)], [self.ids(c) for c in (CHAT, DM, GROUP)])
        self.assertEqual(await self.store.adopt_bot(2002), 0)
        self.assertEqual(([self.state(c) for c in (CHAT, DM, GROUP)], [self.ids(c) for c in (CHAT, DM, GROUP)]), before)
        events = self.store._conn.execute("SELECT text FROM messages WHERE role = 'event'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn("2 chats", events[0][0])
        self.assertTrue((Path(self._tmp.name) / "transcripts" / f"{CHAT}.jsonl").exists())

    async def test_new_bot_messages_numbered_like_old_ones_are_not_dropped(self):
        """A friend's DM: old ids 1-500, then the new bot's 1-5. Nothing is lost, the old history stays in order and
        read, its reply links still resolve and its kept media is still found."""
        await self.store.adopt_bot(1001)
        self.rows(DM, range(1, 501), replies=True, media={7, 250})
        self.marks(DM, 500, 480, topic=500)
        for msg_id in range(1, 6):
            await self.store.record_new_message(msg(DM, msg_id))
        # all five new messages are stored, unread and next for the memory pass; none was taken for an old one
        count, newest = await self.store.get_unread(DM)
        self.assertEqual((count, newest.msg_id, newest.text), (5, 5, "new 5"))
        self.assertEqual(await self.memory_texts(DM), [f"new {n}" for n in range(1, 6)])
        self.assertEqual(self.state(DM), (0, 0))
        self.assertEqual(self.store._conn.execute("SELECT last_read_msg_id FROM chat_topic_reads").fetchone()[0], 0)
        # the old history is untouched in order and content, under ids no Telegram message has
        old = self.store._conn.execute(
            "SELECT msg_id, reply_to_msg_id, text FROM messages WHERE chat_id = ? AND msg_id < 0 ORDER BY id", (DM,)).fetchall()
        self.assertEqual([r[2] for r in old], [f"old {n}" for n in range(1, 501)])
        self.assertEqual([r[0] for r in old], sorted(r[0] for r in old))
        self.assertTrue(all(r[0] < 0 and (r[1] is None or r[1] < 0) for r in old))
        # reply links resolve to the row they pointed at
        for msg_id, reply_to, text in old[1:]:
            self.assertEqual((await self.store.get_message(DM, reply_to)).text, f"old {int(text.split()[1]) - 1}")
        # kept media: the same files, found through the rows' new ids, and still updatable by them
        kept = {m["media"]["file"]: m["msg_id"] for m in await self.store.kept_media(chat_id=DM)}
        self.assertEqual(sorted(kept), [f"media/{DM}/2026-11/250.png", f"media/{DM}/2026-11/7.png"])
        self.assertTrue(all(i < 0 for i in kept.values()))
        self.assertTrue(await self.store.update_media(DM, kept[f"media/{DM}/2026-11/7.png"], {"removed": True}))
        self.assertTrue((await self.store.get_message(DM, kept[f"media/{DM}/2026-11/7.png"])).media_json.count("removed"))
        # a restart finds nothing left to repair
        self.assertEqual(await self.store.adopt_bot(1001), 0)

    async def test_a_second_bot_change_sets_the_first_eras_rows_aside_too(self):
        await self.store.adopt_bot(1001)
        self.rows(DM, range(1, 31), replies=True)
        for msg_id in range(1, 11):  # the second bot
            await self.store.record_new_message(msg(DM, msg_id))
        self.assertEqual(await self.store.adopt_bot(3003), 1)  # and a third
        for msg_id in range(1, 4):
            await self.store.record_new_message(msg(DM, msg_id))
        ids = self.ids(DM)
        self.assertEqual(len(ids), len(set(ids)) , "no id twice")
        self.assertEqual(len(ids), 43)
        self.assertEqual(await self.unread(DM), 3)
        self.assertEqual(await self.memory_texts(DM), [f"new {n}" for n in range(1, 4)])

    async def test_first_start_with_one_bot_only_stores_the_id(self):
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3434, topic=3497)
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(await self.store.kv_get("bot_id"), "1001")
        self.assertEqual(self.state(CHAT), (3497, 3434))
        self.assertEqual(await self.unread(CHAT), 5)  # 3498..3502, as before
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'event'").fetchone()[0], 0)

    async def test_a_normal_restart_with_the_same_bot_changes_nothing(self):
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3434)
        await self.store.adopt_bot(1001)
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(self.state(CHAT), (3497, 3434))
        self.assertEqual(self.ids(CHAT), list(range(3000, 3503)))

    async def test_a_changed_bot_id_sets_every_chat_aside_before_any_message_arrives(self):
        await self.store.adopt_bot(1001)
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3502, topic=3497)
        self.rows(GROUP, range(2200, 2308))
        self.marks(GROUP, 2307, 2300)
        self.assertEqual(await self.store.adopt_bot(2002), 2)
        for chat in (CHAT, GROUP):
            self.assertEqual(self.state(chat), (0, 0))
            self.assertEqual(await self.unread(chat), 0)  # the old history is not suddenly unread
            self.assertEqual(await self.memory_texts(chat), [])
        for msg_id in (64, 65, 70):  # the real shape: what the new bot receives
            await self.store.record_new_message(msg(CHAT, msg_id))
        count, newest = await self.store.get_unread(CHAT)
        self.assertEqual((count, newest.msg_id), (3, 70))
        self.assertEqual(await self.memory_texts(CHAT), ["new 64", "new 65", "new 70"])
        await self.store.mark_chat_read(CHAT, 70)
        self.assertEqual(await self.unread(CHAT), 0)
        await self.store.record_new_message(msg(CHAT, 71))
        self.assertEqual(await self.unread(CHAT), 1)
        await self.store.record_new_message(msg(GROUP, 4000))  # above every old id, still the new bot's
        self.assertEqual(await self.unread(GROUP), 1)

    async def test_messages_saved_a_little_out_of_order_are_not_a_new_bot(self):
        await self.store.adopt_bot(1001)
        for msg_id in (100, 102, 101, 103):
            await self.store.record_new_message(msg(GROUP, msg_id))
        self.assertEqual(self.ids(GROUP), [100, 102, 101, 103])
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(self.ids(GROUP), [100, 102, 101, 103])

    async def test_pointers_into_the_old_history_move_with_it(self):
        await self.store.adopt_bot(1001)
        self.rows(DM, range(1, 101))
        conn = self.store._conn
        conn.execute("INSERT INTO pending_turns (chat_id, first_queued_msg_id, last_queued_msg_id, updated_at) VALUES (?, 98, 100, 'x')", (DM,))
        conn.execute("INSERT INTO menu_cards (chat_id, message_id, sent_at, expires_at) VALUES (?, 99, 'x', 'y')", (DM,))
        conn.execute(
            "INSERT INTO noticings (day, kind, title, body, source_kind, source_chat_id, source_msg_id, created_at) "
            "VALUES ('d', 'idea', 't', 'b', 'owner_dm', ?, 42, 'x')", (DM,))
        await self.store.record_new_message(msg(DM, 1))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pending_turns").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM menu_cards").fetchone()[0], 0)
        source = conn.execute("SELECT source_msg_id FROM noticings").fetchone()[0]
        self.assertEqual((await self.store.get_message(DM, source)).text, "old 42")


class FakeBot:
    def __init__(self):
        self.calls = []

    async def set_message_reaction(self, chat_id, message_id, **kw):
        self.calls.append(("react", message_id))

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", kw.get("reply_parameters")))
        from types import SimpleNamespace
        return SimpleNamespace(message_id=9001)

    async def delete_message(self, chat_id, message_id, **kw):
        self.calls.append(("delete", message_id))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit", message_id))


class EarlierBotTests(unittest.IsolatedAsyncioTestCase):
    """The new bot can't reach a message the old bot sent: a negative id is skipped, never sent to Telegram."""

    async def test_reactions_edits_and_deletes_of_earlier_bot_messages_are_skipped(self):
        from dom.telegram.executor import _delete_later, edit_text, react

        async def nap(_):
            return None

        bot = FakeBot()
        self.assertIsNone(await react(bot, 5, -7, "🔥"))
        self.assertIsNone(await edit_text(bot, 5, -7, "hi"))
        await _delete_later(bot, None, 5, -7, 0, nap)
        self.assertEqual(bot.calls, [])
        self.assertTrue(await react(bot, 5, 7, "🔥"))
        self.assertEqual(bot.calls, [("react", 7)])

    async def test_a_reply_to_an_earlier_bot_message_is_sent_as_a_plain_message(self):
        from dom.actions import Bubble
        from dom.telegram.executor import _earlier_bot

        self.assertTrue(_earlier_bot(Bubble(text="x", reply_to=-3).reply_to, "a reply", 5))
        self.assertFalse(_earlier_bot(Bubble(text="x", reply_to=3).reply_to, "a reply", 5))
        self.assertFalse(_earlier_bot(None, "a reply", 5))
