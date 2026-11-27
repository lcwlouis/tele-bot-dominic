"""A new bot token restarts Telegram's message ids: on a real bot change the old bot's rows are set aside under negative
ids and read markers and memory cursors start over (v0.8.6 W8)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dom.models import StoredMessage
from dom.store.store import ERA_SUSPECT, PENDING_ERA, Store

OWNER = 111
CHAT = str(OWNER)
DM = "222"
GROUP = "-100"
BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def scope(chat_id: str) -> str:
    return "owner_private" if chat_id == CHAT else f"chat:{chat_id}"


class BotChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name), owner_id=OWNER)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)
        self.minutes = 0

    def tick(self) -> str:
        """The next moment, one minute on: rows saved in order are created in order."""
        self.minutes += 1
        return (BASE + timedelta(minutes=self.minutes)).isoformat()

    def msg(self, chat_id: str, msg_id: int, **kw) -> StoredMessage:
        return StoredMessage(
            chat_id=chat_id, msg_id=msg_id, text=f"new {msg_id}", visibility_scope=scope(chat_id),
            sender_handle="friend", sender_name="Friend", role="user", created_at=self.tick(), **kw,
        )

    def rows(self, chat_id: str, ids, *, replies=False, media=(), assistant=(), thread=None):
        """Save rows exactly as a database from before any of this holds them, no matter how their ids jump."""
        conn = self.store._conn
        conn.execute("INSERT INTO chats (chat_id, chat_type, visibility_scope, authorization_status, updated_at) "
                     "VALUES (?, 'private', ?, 'approved', 'x') ON CONFLICT(chat_id) DO NOTHING", (chat_id, scope(chat_id)))
        for msg_id in ids:
            conn.execute(
                "INSERT INTO messages (chat_id, msg_id, reply_to_msg_id, thread_id, role, visibility_scope, text, media_json, "
                "is_bot, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'x')",
                (chat_id, msg_id, msg_id - 1 if replies and msg_id > 1 else None, thread,
                 "assistant" if msg_id in media or msg_id in assistant else "user", scope(chat_id), f"old {msg_id}",
                 json.dumps({"kind": "photo", "file": f"media/{chat_id}/2026-11/{msg_id}.png"}) if msg_id in media else None,
                 self.tick()),
            )

    def marks(self, chat_id: str, read: int, cursor: int, topic: int | None = None, thread: int = 0):
        conn = self.store._conn
        conn.execute("UPDATE chats SET last_read_msg_id = ?, memory_cursor_msg_id = ? WHERE chat_id = ?", (read, cursor, chat_id))
        if topic is not None:
            conn.execute("INSERT INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) VALUES (?, ?, ?, 'x')",
                         (chat_id, thread, topic))

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
        new bot's (Dom's last reply is 74), a group has only old rows, and a friend's DM has both."""
        self.rows(CHAT, range(3000, 3503))
        self.rows(CHAT, range(1, 76), assistant={5, 74})
        self.marks(CHAT, 3497, 3502, topic=3497)
        self.rows(GROUP, range(2200, 2308))
        self.marks(GROUP, 2307, 2300)
        self.rows(DM, range(3300, 3498))
        self.rows(DM, range(1, 6))
        self.marks(DM, 3497, 3434, topic=3497)

    # ---- an instance that switched before it stored a bot id: recovered once, from clear evidence -------------------

    async def test_a_switch_made_before_any_bot_id_was_stored_is_recovered_from_the_data(self):
        self.real_shape()
        self.assertEqual(await self.store.adopt_bot(2002), 2)  # the owner DM and the friend's DM
        self.assertEqual(await self.store.kv_get("bot_id"), "2002")
        self.assertEqual(await self.store.open_era_suspects(), [])  # the owner DM and the friend's DM were both proven
        # Marked read up to Dom's last reply, so only the owner's 75 is unread; the memory pass starts over.
        self.assertEqual(self.state(CHAT), (74, 0))
        count, newest = await self.store.get_unread(CHAT)
        self.assertEqual((count, newest.msg_id), (1, 75))
        self.assertEqual(await self.memory_texts(CHAT), [f"old {n}" for n in range(1, 76)])
        self.assertEqual(await self.store.count_new_for_memory(CHAT), 75)
        self.assertEqual(self.state(DM), (0, 0))  # no reply from Dom in its new run yet
        self.assertEqual(await self.unread(DM), 5)
        self.assertEqual(len(await self.memory_texts(DM)), 5)
        self.assertEqual(self.state(GROUP), (2307, 2300))  # old rows only: history exactly as it was
        self.assertEqual(await self.unread(GROUP), 0)
        self.assertEqual(await self.memory_texts(GROUP), [f"old {n}" for n in range(2301, 2308)])
        topics = self.store._conn.execute("SELECT chat_id, last_read_msg_id FROM chat_topic_reads ORDER BY chat_id").fetchall()
        self.assertEqual([tuple(t) for t in topics], [(CHAT, 74), (DM, 0)])
        events = self.store._conn.execute("SELECT text FROM messages WHERE role = 'event'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn(f"chat {CHAT}", events[0][0])
        self.assertIn(f"chat {DM}", events[0][0])
        self.assertTrue((Path(self._tmp.name) / "transcripts" / f"{CHAT}.jsonl").exists())

    async def test_a_second_start_changes_nothing(self):
        self.real_shape()
        await self.store.adopt_bot(2002)
        await self.store.advance_memory_cursor(CHAT, 75)
        before = ([self.state(c) for c in (CHAT, DM, GROUP)], [self.ids(c) for c in (CHAT, DM, GROUP)])
        self.assertEqual(await self.store.adopt_bot(2002), 0)
        self.assertEqual(([self.state(c) for c in (CHAT, DM, GROUP)], [self.ids(c) for c in (CHAT, DM, GROUP)]), before)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'event'").fetchone()[0], 1)

    async def test_recovery_needs_clear_evidence_and_leaves_an_ordinary_chat_alone(self):
        cases = {
            "late edit": [(1000, 0), (1001, 0), (100, 0)],  # one odd row (a late edit or an unseen message)
            "two rows": [(1000, 0), (1001, 0), (5, 0), (6, 0)],  # a suffix too short to believe
            "not a tenth": [(1000, 0), (1001, 0), (400, 0), (401, 0), (402, 0)],  # drops, but not that far
            "climbs back": [(1000, 0), (1001, 0), (5, 0), (6, 0), (7, 0), (1500, 0)],  # a new run never passes the old
            "created earlier": [(1000, 0), (1001, 0), (5, -5), (6, -5), (7, -5)],  # rows from before the old ones
        }
        for number, (name, spec) in enumerate(cases.items()):
            chat = str(500 + number)
            for msg_id, offset in spec:
                self.rows(chat, [msg_id])
                if offset:
                    self.store._conn.execute(
                        "UPDATE messages SET created_at = ? WHERE chat_id = ? AND msg_id = ?",
                        ((BASE - timedelta(days=1)).isoformat(), chat, msg_id))
            self.marks(chat, 1001, 1001)
        self.assertEqual(await self.store.adopt_bot(2002), 0)
        for number, (name, spec) in enumerate(cases.items()):
            chat = str(500 + number)
            self.assertEqual(self.ids(chat), [msg_id for msg_id, _ in spec], name)
            self.assertEqual(self.state(chat), (1001, 1001), name)

    async def test_an_odd_lower_id_arriving_later_changes_nothing_at_insert_either(self):
        await self.store.adopt_bot(1001)
        for msg_id in (1000, 1001, 100):
            await self.store.record_new_message(self.msg(DM, msg_id))
        await self.store.mark_chat_read(DM, 1001)
        await self.store.record_new_message(self.msg(DM, 101))
        self.assertEqual(self.ids(DM), [1000, 1001, 100, 101])
        self.assertEqual(self.state(DM)[0], 1001)

    async def test_recovery_recomputes_markers_even_when_a_stale_one_is_within_the_new_range(self):
        """The ids overlap, so a marker of 50 from the old bot would pass for one in a new run that reached 100."""
        self.rows(DM, range(900, 1000))
        self.rows(DM, range(1, 101), assistant={90})
        self.marks(DM, 50, 50, topic=50)
        await self.store.adopt_bot(2002)
        self.assertEqual(self.state(DM), (90, 0))
        self.assertEqual(self.store._conn.execute("SELECT last_read_msg_id FROM chat_topic_reads").fetchone()[0], 90)
        self.assertEqual(await self.unread(DM), 10)

    async def test_a_topic_marker_goes_to_dom_s_last_reply_in_that_topic(self):
        self.rows(GROUP, range(2200, 2300))
        self.rows(GROUP, range(1, 21), assistant={10, 15})
        conn = self.store._conn
        conn.execute("UPDATE messages SET thread_id = 7 WHERE chat_id = ? AND msg_id BETWEEN 1 AND 20 AND msg_id != 15", (GROUP,))
        self.marks(GROUP, 2299, 2299)
        for thread in (0, 7):
            conn.execute("INSERT INTO chat_topic_reads (chat_id, thread_id, last_read_msg_id, updated_at) VALUES (?, ?, 2299, 'x')", (GROUP, thread))
        await self.store.adopt_bot(2002)
        reads = dict(conn.execute("SELECT thread_id, last_read_msg_id FROM chat_topic_reads WHERE chat_id = ?", (GROUP,)).fetchall())
        self.assertEqual(reads, {0: 15, 7: 10})
        self.assertEqual(self.state(GROUP)[0], 15)

    async def test_a_noticing_keeps_its_source_when_it_was_made_from_the_new_run(self):
        self.rows(DM, range(3300, 3400))
        before = self.tick()
        self.rows(DM, range(1, 101))
        after = self.tick()
        conn = self.store._conn
        for source, made in ((3350, before), (70, after)):
            conn.execute(
                "INSERT INTO noticings (day, kind, title, body, source_kind, source_chat_id, source_msg_id, created_at) "
                "VALUES ('d', 'idea', ?, 'b', 'owner_dm', ?, ?, ?)", (f"from {source}", DM, source, made))
        await self.store.adopt_bot(2002)
        found = {title: source for title, source in conn.execute("SELECT title, source_msg_id FROM noticings")}
        self.assertEqual(found["from 70"], 70)
        self.assertEqual((await self.store.get_message(DM, found["from 3350"])).text, "old 3350")

    # ---- a suspected switch the evidence can't yet prove stays open ------------------------------------------------

    def two_new_rows(self):
        """A friend's DM that switched before any bot id was stored, with only two messages from the new bot so far."""
        self.rows(DM, range(3300, 3400))
        self.rows(DM, range(1, 3))
        self.marks(DM, 3399, 3399)

    async def test_a_switch_with_too_few_new_rows_stays_open_and_moves_once_the_third_arrives(self):
        self.two_new_rows()
        self.assertEqual(await self.store.adopt_bot(2002), 0)
        self.assertEqual(await self.store.kv_get("bot_id"), "2002")
        self.assertEqual(self.ids(DM), list(range(3300, 3400)) + [1, 2])  # nothing moved on what is not yet proof
        self.assertIsNotNone(await self.store.kv_get(ERA_SUSPECT + DM))
        self.assertEqual(await self.store.adopt_bot(2002), 0)  # a restart with no more evidence: still open
        self.assertIsNotNone(await self.store.kv_get(ERA_SUSPECT + DM))
        self.rows(DM, [3])  # the new bot's third message
        self.assertEqual(await self.store.adopt_bot(2002), 1)
        self.assertEqual(self.ids(DM)[-3:], [1, 2, 3])
        self.assertTrue(all(i < 0 for i in self.ids(DM)[:-3]))
        self.assertEqual(self.state(DM), (0, 0))
        self.assertEqual(await self.unread(DM), 3)
        self.assertIsNone(await self.store.kv_get(ERA_SUSPECT + DM))
        self.assertEqual(await self.store.adopt_bot(2002), 0)  # settled
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'event'").fetchone()[0], 1)

    async def test_an_open_chat_is_logged_at_each_start_and_listed_for_the_doctor(self):
        self.two_new_rows()
        with self.assertLogs("dom.store", "WARNING") as first:
            await self.store.adopt_bot(2002)
        self.assertEqual(len([line for line in first.output if f"chat {DM}" in line]), 1)
        with self.assertLogs("dom.store", "WARNING") as second:
            await self.store.adopt_bot(2002)
        self.assertEqual(len([line for line in second.output if f"chat {DM}" in line]), 1)
        self.assertEqual(await self.store.open_era_suspects(), [DM])

    async def test_a_suspicion_ends_when_the_chat_climbs_back_above_the_old_run(self):
        """One odd lower id (a late edit, an unseen message) looks like a switch until the chat carries on past the old ids."""
        self.rows(DM, [1000, 1001, 100])
        await self.store.adopt_bot(2002)
        self.assertEqual(await self.store.open_era_suspects(), [DM])
        self.rows(DM, [1002])
        self.assertEqual(await self.store.adopt_bot(2002), 0)
        self.assertEqual(await self.store.open_era_suspects(), [])
        self.assertEqual(self.ids(DM), [1000, 1001, 100, 1002])

    async def test_a_normal_chat_is_never_suspected(self):
        self.rows(CHAT, range(3000, 3503))
        self.rows(GROUP, [100, 102, 101, 103, 104])  # saved a little out of order
        self.rows(DM, range(1, 40))
        await self.store.adopt_bot(2002)
        self.assertEqual(await self.store.open_era_suspects(), [])
        self.assertIsNone(self.store._conn.execute("SELECT 1 FROM kv WHERE key LIKE 'era_suspect:%'").fetchone())

    async def test_a_real_bot_change_closes_the_open_suspicions(self):
        """Every chat is marked for the new bot anyway, so what was suspected is moved then, not by a strict check."""
        self.two_new_rows()
        await self.store.adopt_bot(2002)
        self.assertEqual(await self.store.adopt_bot(3003), 1)
        self.assertEqual(await self.store.open_era_suspects(), [])
        await self.store.record_new_message(self.msg(DM, 1))
        self.assertEqual(self.ids(DM)[-1], 1)
        self.assertTrue(all(i < 0 for i in self.ids(DM)[:-1]))

    # ---- the first start, a restart -------------------------------------------------------------------------------

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

    # ---- a real change: every chat moves when the new bot first writes there --------------------------------------

    async def test_a_changed_bot_id_moves_each_chat_when_its_first_new_message_arrives(self):
        await self.store.adopt_bot(1001)
        self.rows(CHAT, range(3000, 3503))
        self.marks(CHAT, 3497, 3502, topic=3497)
        self.rows(GROUP, range(2200, 2308))
        self.marks(GROUP, 2307, 2300)
        self.assertEqual(await self.store.adopt_bot(2002), 2)
        self.assertEqual(await self.store.kv_get("bot_id"), "2002")
        self.assertEqual(self.ids(CHAT), list(range(3000, 3503)))  # nothing is rewritten before the new bot writes
        self.assertEqual(self.state(CHAT), (3497, 3502))
        for msg_id in (64, 65, 70):  # the real shape: what the new bot receives
            await self.store.record_new_message(self.msg(CHAT, msg_id))
        self.assertEqual(self.state(CHAT), (0, 0))
        count, newest = await self.store.get_unread(CHAT)
        self.assertEqual((count, newest.msg_id), (3, 70))
        self.assertEqual(await self.memory_texts(CHAT), ["new 64", "new 65", "new 70"])
        self.assertEqual(self.state(GROUP), (2307, 2300))  # still waiting for its own first message
        self.assertIsNotNone(await self.store.kv_get(PENDING_ERA + GROUP))
        self.assertIsNone(await self.store.kv_get(PENDING_ERA + CHAT))
        await self.store.mark_chat_read(CHAT, 70)
        await self.store.record_new_message(self.msg(CHAT, 71))
        self.assertEqual(await self.unread(CHAT), 1)
        await self.store.record_new_message(self.msg(GROUP, 4000))  # above every old id, still the new bot's
        self.assertEqual((await self.unread(GROUP), self.state(GROUP)), (1, (0, 0)))
        self.assertEqual(await self.store.adopt_bot(2002), 0)

    async def test_new_bot_messages_numbered_like_old_ones_are_not_dropped(self):
        """A friend's DM: old ids 1-500, then the new bot's 1-5. Nothing is lost, the old history stays in order and
        read, its reply links still resolve and its kept media is still found."""
        await self.store.adopt_bot(1001)
        self.rows(DM, range(1, 501), replies=True, media={7, 250})
        self.marks(DM, 500, 480, topic=500)
        await self.store.adopt_bot(2002)
        for msg_id in range(1, 6):
            await self.store.record_new_message(self.msg(DM, msg_id))
        count, newest = await self.store.get_unread(DM)
        self.assertEqual((count, newest.msg_id, newest.text), (5, 5, "new 5"))
        self.assertEqual(await self.memory_texts(DM), [f"new {n}" for n in range(1, 6)])
        self.assertEqual(self.state(DM), (0, 0))
        self.assertEqual(self.store._conn.execute("SELECT last_read_msg_id FROM chat_topic_reads").fetchone()[0], 0)
        old = self.store._conn.execute(
            "SELECT msg_id, reply_to_msg_id, text FROM messages WHERE chat_id = ? AND msg_id < 0 ORDER BY id", (DM,)).fetchall()
        self.assertEqual([r[2] for r in old], [f"old {n}" for n in range(1, 501)])
        self.assertEqual([r[0] for r in old], sorted(r[0] for r in old))
        self.assertTrue(all(r[0] < 0 and (r[1] is None or r[1] < 0) for r in old))
        for msg_id, reply_to, text in old[1:]:  # reply links resolve to the row they pointed at
            self.assertEqual((await self.store.get_message(DM, reply_to)).text, f"old {int(text.split()[1]) - 1}")
        kept = {m["media"]["file"]: m["msg_id"] for m in await self.store.kept_media(chat_id=DM)}
        self.assertEqual(sorted(kept), [f"media/{DM}/2026-11/250.png", f"media/{DM}/2026-11/7.png"])
        self.assertTrue(all(i < 0 for i in kept.values()))
        self.assertTrue(await self.store.update_media(DM, kept[f"media/{DM}/2026-11/7.png"], {"removed": True}))
        self.assertIn("removed", (await self.store.get_message(DM, kept[f"media/{DM}/2026-11/7.png"])).media_json)

    async def test_a_second_bot_change_sets_the_first_eras_rows_aside_too(self):
        await self.store.adopt_bot(1001)
        self.rows(DM, range(1, 31), replies=True)
        await self.store.adopt_bot(2002)
        for msg_id in range(1, 11):
            await self.store.record_new_message(self.msg(DM, msg_id))
        self.assertEqual(await self.store.adopt_bot(3003), 1)
        for msg_id in range(1, 4):
            await self.store.record_new_message(self.msg(DM, msg_id))
        ids = self.ids(DM)
        self.assertEqual((len(ids), len(set(ids))), (43, 43))
        self.assertEqual(await self.unread(DM), 3)
        self.assertEqual(await self.memory_texts(DM), [f"new {n}" for n in range(1, 4)])

    async def test_the_repair_handles_soft_deleted_rows_and_leaves_the_search_index_sound(self):
        await self.store.adopt_bot(1001)
        for msg_id in range(1, 31):
            await self.store.record_new_message(self.msg(DM, msg_id))
        await self.store.record_message_delete(DM, [3, 4, 5])  # out of the full-text index already
        await self.store.adopt_bot(2002)
        for msg_id in range(1, 4):
            await self.store.record_new_message(self.msg(DM, msg_id))  # raises if the repair trips the index
        conn = self.store._conn
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('integrity-check')")
        hits = conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'new' AND chat_id = ?", (DM,)).fetchone()[0]
        self.assertEqual(hits, 30 - 3 + 3)  # the deleted ones are not found, the moved ones still are
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = 'messages_au'").fetchone()[0], 1)
        await self.store.record_message_edit(DM, 2, "edited text")  # the trigger works again
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'edited'").fetchone()[0], 1)

    async def test_pointers_into_the_old_history_move_or_go_with_it(self):
        await self.store.adopt_bot(1001)
        self.rows(CHAT, range(1, 101))
        conn = self.store._conn
        conn.execute("INSERT INTO pending_turns (chat_id, first_queued_msg_id, last_queued_msg_id, updated_at) VALUES (?, 98, 100, 'x')", (CHAT,))
        conn.execute("INSERT INTO menu_cards (chat_id, message_id, sent_at, expires_at) VALUES (?, 99, 'x', 'y')", (CHAT,))
        conn.execute("INSERT INTO noticings (day, kind, title, body, source_kind, source_chat_id, source_msg_id, opener_msg_id, created_at) "
                     "VALUES ('d', 'idea', 't', 'b', 'owner_dm', ?, 42, 77, ?)", (CHAT, self.tick()))
        conn.execute("INSERT INTO desktop_permission_history (action, status, card_message_id, created_at) VALUES ('visit_site', 'pending', 55, 'x')")
        conn.execute("INSERT INTO secret_requests (label, reason, target, target_key, token_hash, status, card_message_id, created_at, expires_at) "
                     "VALUES ('l', 'r', 't', 'k', 'h', 'pending', 56, 'x', 'y')")
        conn.execute("INSERT INTO cross_chat (kind, source_chat_id, target_chat_id, text, status, sent_msg_id, created_at, updated_at) "
                     f"VALUES ('message', -5, {OWNER}, 't', 'sent', 90, 'x', 'x')")
        await self.store.adopt_bot(2002)
        await self.store.record_new_message(self.msg(CHAT, 1))
        count = lambda table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        self.assertEqual((count("pending_turns"), count("menu_cards")), (0, 0))
        source, opener = conn.execute("SELECT source_msg_id, opener_msg_id FROM noticings").fetchone()
        self.assertEqual((await self.store.get_message(CHAT, source)).text, "old 42")
        self.assertIsNone(opener)
        self.assertEqual(tuple(conn.execute("SELECT status, card_message_id FROM desktop_permission_history").fetchone()), ("expired", None))
        self.assertEqual(tuple(conn.execute("SELECT status, card_message_id FROM secret_requests").fetchone()), ("expired", None))
        self.assertIsNone(conn.execute("SELECT sent_msg_id FROM cross_chat").fetchone()[0])

    async def test_messages_saved_a_little_out_of_order_are_not_a_new_bot(self):
        await self.store.adopt_bot(1001)
        for msg_id in (100, 102, 101, 103):
            await self.store.record_new_message(self.msg(GROUP, msg_id))
        self.assertEqual(self.ids(GROUP), [100, 102, 101, 103])
        self.assertEqual(await self.store.adopt_bot(1001), 0)
        self.assertEqual(self.ids(GROUP), [100, 102, 101, 103])

    async def test_a_turn_after_the_repair_marks_read_in_the_new_run_never_an_old_id(self):
        """On v0.8.5 the old bot's rows counted as unread (ids above the marker), so the newest of them won and the
        marker slid back to 3497 after every answer."""
        self.real_shape()
        await self.store.adopt_bot(2002)
        count, newest = await self.store.get_unread(CHAT)
        self.assertEqual((count, newest.msg_id), (1, 75))
        self.assertEqual([(t, c, m.msg_id) for t, c, m in await self.store.get_unread_topics(CHAT)], [(None, 1, 75)])
        self.assertEqual((await self.store.latest_chat_message(CHAT))[0], 75)
        await self.store.mark_chat_read(CHAT, newest.msg_id)  # what the dispatcher does once it has answered
        self.assertEqual(self.state(CHAT)[0], 75)
        self.assertEqual(await self.unread(CHAT), 0)
        await self.store.mark_chat_read(CHAT, -5)  # a pre-repair id (now negative) can never move a marker
        self.assertEqual(self.state(CHAT)[0], 75)

    # Once one chat's history proves the bot changed, the rest of the chats it numbers follow (v0.8.6 gate).

    async def test_a_proven_switch_marks_the_chats_that_hold_only_old_bot_rows_and_they_move_on_their_first_new_message(self):
        self.rows(DM, range(2000, 2010))
        self.marks(DM, 2009, 2009)
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.marks(CHAT, 3099, 3099)
        self.rows(CHAT, range(1, 6), assistant={5})
        self.assertEqual(await self.store.adopt_bot(9), 1)  # the owner's DM is proven and moved now
        self.assertIsNotNone(await self.store.kv_get(PENDING_ERA + DM))  # the friend's DM waits for the new bot
        await self.store.record_new_message(self.msg(DM, 7))
        unread, _ = await self.store.get_unread(int(DM))
        self.assertEqual(unread, 1)  # without the mark, 7 would sit under the old marker 2009 and never be read
        self.assertEqual(await self.store.adopt_bot(9), 0)

    async def test_a_chat_that_began_with_the_new_bot_is_not_marked(self):
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.rows(CHAT, range(1, 6), assistant={5})
        self.rows(DM, range(1, 4))  # a friend who first wrote to the new bot
        await self.store.adopt_bot(9)
        self.assertIsNone(await self.store.kv_get(PENDING_ERA + DM))

    async def test_a_chat_that_only_looked_switched_is_moved_once_another_chat_proves_it(self):
        self.rows(DM, range(2000, 2010))
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.rows(CHAT, range(1, 6), assistant={5})
        self.rows(DM, [1])  # one new-bot row: too few to prove on its own
        self.assertEqual(await self.store.adopt_bot(9), 2)
        self.assertEqual(await self.store.open_era_suspects(), [])
        self.assertEqual([row[0] for row in self.store._conn.execute("SELECT msg_id FROM messages WHERE chat_id = ? AND msg_id > 0", (DM,)).fetchall()], [1])

    async def test_supergroups_keep_their_numbering_through_a_bot_change(self):
        supergroup = "-100" + "9" * 10  # a supergroup id, built so it reads as no real one
        self.rows(supergroup, range(283500, 283520))
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.rows(CHAT, range(1, 6), assistant={5})
        await self.store.adopt_bot(9)
        self.assertIsNone(await self.store.kv_get(PENDING_ERA + supergroup))
        await self.store.adopt_bot(10)  # a recorded change marks every chat the bot numbers, and no supergroup
        self.assertIsNotNone(await self.store.kv_get(PENDING_ERA + CHAT))
        self.assertIsNone(await self.store.kv_get(PENDING_ERA + supergroup))

    async def test_a_switch_first_proven_on_a_later_start_still_marks_the_quiet_chats(self):
        self.rows(DM, range(2000, 2010))  # a friend who has not written to the new bot
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.rows(CHAT, range(1, 3))  # two new-bot rows: only a suspect
        self.assertEqual(await self.store.adopt_bot(9), 0)
        self.assertEqual(await self.store.open_era_suspects(), [CHAT])
        self.rows(CHAT, [3])
        self.assertEqual(await self.store.adopt_bot(9), 1)
        self.assertIsNotNone(await self.store.kv_get(PENDING_ERA + DM))

    async def test_a_quiet_chat_begun_with_the_new_bot_before_the_proof_is_not_marked(self):
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.rows(DM, [1, 2])  # began with the new bot, then went quiet
        self.rows(CHAT, range(3, 9), assistant={8})
        await self.store.adopt_bot(9)
        self.assertIsNone(await self.store.kv_get(PENDING_ERA + DM))

    async def test_a_late_odd_row_numbered_past_the_new_bots_counter_is_not_taken_as_a_switch(self):
        self.rows(CHAT, range(3000, 3100), assistant={3099})
        self.rows(DM, range(1000, 1500))
        self.rows(CHAT, range(1, 6), assistant={5})
        self.rows(DM, [90])  # under a tenth of 1499, but above anything the new bot has reached (5)
        await self.store.adopt_bot(9)
        self.assertEqual([row[0] for row in self.store._conn.execute(
            "SELECT msg_id FROM messages WHERE chat_id = ? AND msg_id < 0", (DM,))], [])

    def test_a_basic_group_whose_id_starts_with_100_is_numbered_by_the_bot(self):
        from dom.store.store import bot_numbered
        self.assertTrue(bot_numbered("-100" + "123456"))
        self.assertFalse(bot_numbered("-100" + "1" * 10))


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

