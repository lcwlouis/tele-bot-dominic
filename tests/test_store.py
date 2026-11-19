import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dom.models import StoredMessage
from dom.store.store import MIGRATIONS_DIR, TARGET_VERSION, Store, migrate_database

OWNER_ID = 111
ADMIN_USER_ID = str(OWNER_ID)

V1_SCHEMA_SQL = (MIGRATIONS_DIR / "0001_init.sql").read_text()


def msg(chat_id="-100", msg_id=1, text="hello", scope=None, **kw) -> StoredMessage:
    return StoredMessage(
        chat_id=chat_id,
        msg_id=msg_id,
        text=text,
        visibility_scope=scope or f"chat:{chat_id}",
        sender_handle=kw.pop("sender_handle", "@alan"),
        sender_name=kw.pop("sender_name", "Alan"),
        created_at=kw.pop("created_at", "2026-11-18T10:00:00+00:00"),
        **kw,
    )


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.store = Store(self.ws, owner_id=OWNER_ID)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def transcript(self, chat_id="-100"):
        path = self.ws / "transcripts" / f"{chat_id}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def set_chat(self, chat_id="-100", **cols):
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.store._conn.execute(f"UPDATE chats SET {sets} WHERE chat_id = ?", (*cols.values(), chat_id))


class MigrationTests(unittest.TestCase):
    def test_fresh_db_reaches_target_without_backup(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            self.assertEqual(migrate_database(db), TARGET_VERSION)
            self.assertEqual(TARGET_VERSION, 12)
            self.assertEqual(list(Path(d).glob("*.bak")), [])
            conn = sqlite3.connect(db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            self.assertTrue({"sender_user_id", "original_seen_before_edit", "previous_seen_text"} <= cols)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            self.assertTrue({"chats", "people_identities", "scheduled_jobs", "messages", "messages_fts",
                             "messages_ai", "messages_ad", "messages_au", "pending_turns",
                             "idx_messages_chat_msg_unique"} <= names)
            conn.close()

    def test_existing_v2_db_is_backed_up_and_data_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(V1_SCHEMA_SQL)  # what scripts/migrate_v1_to_v2.py produces (version 2)
            conn.execute(
                "INSERT INTO messages (chat_id, msg_id, role, visibility_scope, text, created_at) "
                "VALUES ('c', 1, 'user', 'global_public', 'old row', 't')"
            )
            conn.commit()
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            conn.close()

            self.assertEqual(migrate_database(db), TARGET_VERSION)

            bak = Path(d) / "state.db.v2.bak"
            self.assertTrue(bak.exists())
            old = sqlite3.connect(bak)
            self.assertNotIn("previous_seen_text", {r[1] for r in old.execute("PRAGMA table_info(messages)")})
            self.assertEqual(old.execute("SELECT text FROM messages").fetchone()[0], "old row")
            old.close()
            new = sqlite3.connect(db)
            row = new.execute("SELECT text, original_seen_before_edit, previous_seen_text FROM messages").fetchone()
            self.assertEqual(row, ("old row", 0, None))
            new.close()

    def test_idempotent_when_columns_already_exist(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute("ALTER TABLE messages ADD COLUMN sender_user_id INTEGER")
            conn.commit()
            conn.close()
            self.assertEqual(migrate_database(db), TARGET_VERSION)
            self.assertEqual(migrate_database(db), TARGET_VERSION)  # no-op rerun

    def test_v3_upgrade_cleans_duplicates_and_owner_dm_is_approved(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            db = workspace / "state.db"
            with sqlite3.connect(db) as conn:
                conn.executescript(V1_SCHEMA_SQL)
                conn.executescript((MIGRATIONS_DIR / "0002_offline_edit_tracking.sql").read_text())
                conn.executemany(
                    "INSERT INTO chats (chat_id, chat_type, visibility_scope, updated_at) "
                    "VALUES (?, 'dm', 'global_public', 't')",
                    [(ADMIN_USER_ID,), ("-100",), ("-200",)],
                )
                conn.executemany(
                    "INSERT INTO messages (chat_id, msg_id, role, visibility_scope, text, created_at, is_edited) "
                    "VALUES ('-100', ?, 'user', 'global_public', ?, 't', ?)",
                    [(1, "original", 0), (1, "edited", 1), (1, "replay", 0),
                     (2, "deleted", 0), (2, "tombstone duplicate", 0), (2, "late replay", 0),
                     (None, "repeat", 0), (None, "repeat", 0)],
                )
                conn.execute("UPDATE messages SET is_deleted = 1 WHERE id IN (4, 5)")

            self.assertEqual(migrate_database(db), TARGET_VERSION)
            self.assertTrue(db.with_name("state.db.v3.bak").exists())
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT id, is_deleted FROM messages ORDER BY id").fetchall(), [
                    (2, 0), (5, 1), (7, 0), (8, 0),
                ])
                self.assertEqual(conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'edited'").
                                 fetchall(), [(2,)])
                self.assertEqual(conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'replay'").
                                 fetchall(), [])
            # no owner id lives in SQL: migrated chats are pending until a Store knows the owner
            store = Store(workspace, owner_id=OWNER_ID)
            try:
                self.assertEqual(
                    {r["chat_id"]: r["authorization_status"] for r in store._conn.execute("SELECT * FROM chats")},
                    {ADMIN_USER_ID: "approved", "-100": "pending_approval", "-200": "pending_approval"},
                )
            finally:
                store.close()

    def test_v4_db_upgrades_to_7_preserving_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            with sqlite3.connect(db) as conn:
                for name in ("0001_init.sql", "0002_offline_edit_tracking.sql", "0003_chat_auth_and_outbox.sql"):
                    conn.executescript((MIGRATIONS_DIR / name).read_text())
                conn.execute(
                    "INSERT INTO chats (chat_id, chat_type, visibility_scope, authorization_status, title, updated_at) "
                    "VALUES ('-100', 'group', 'chat:-100', 'approved', 'Old group', 't')"
                )
                conn.execute(
                    "INSERT INTO messages (chat_id, msg_id, role, visibility_scope, text, created_at) "
                    "VALUES ('-100', 1, 'user', 'chat:-100', 'kept row', 't')"
                )
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            store = Store(Path(d), owner_id=OWNER_ID)
            try:
                self.assertEqual(store._conn.execute("PRAGMA user_version").fetchone()[0], 12)
                chat = store._conn.execute("SELECT * FROM chats WHERE chat_id = '-100'").fetchone()
                self.assertEqual(
                    (chat["authorization_status"], chat["title"], chat["traits_json"], chat["bot_depth"],
                     chat["memory_cursor_msg_id"]),
                    ("approved", "Old group", "{}", 0, 0),
                )
                self.assertEqual(
                    store._conn.execute("SELECT text FROM messages").fetchall()[0][0], "kept row")
                self.assertEqual(len(store._search_chat_history("kept", "-100", 10)), 1)
            finally:
                store.close()
            self.assertTrue((Path(d) / "state.db.v4.bak").exists())

    def test_failed_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute("DROP TABLE messages")  # 0002 ALTER will fail
            conn.commit()
            conn.close()
            with self.assertRaises(sqlite3.OperationalError):
                migrate_database(db)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            conn.close()


class MessageTests(StoreTestCase):
    async def test_new_message_indexed_and_logged(self):
        rid = await self.store.record_new_message(msg(text="supper at utown", sender_user_id=7))
        self.assertGreater(rid, 0)
        window = await self.store.get_recent_chat_window("-100")
        self.assertEqual([m.text for m in window], ["supper at utown"])
        self.assertEqual(window[0].sender_user_id, 7)
        hits = await self.store.search_chat_history("utown", "-100")
        self.assertEqual(len(hits), 1)
        ev = self.transcript()
        self.assertEqual(ev[0]["event_type"], "message_created")
        self.assertEqual(ev[0]["text"], "supper at utown")
        self.assertEqual(ev[0]["msg_id"], 1)

    async def test_message_deduplication_across_connections_and_after_delete(self):
        other = Store(self.ws, owner_id=OWNER_ID)
        self.addCleanup(other.close)
        message = msg(text="original")
        first_id, second_id = await asyncio.gather(
            self.store.record_new_message(message), other.record_new_message(message)
        )
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.transcript()), 1)
        self.assertEqual(len(await self.store.get_recent_chat_window("-100")), 1)
        await self.store.record_message_delete("-100", [1])
        self.assertEqual(await other.record_new_message(message), first_id)
        self.assertEqual(len(self.transcript()), 2)
        self.assertEqual(await self.store.get_recent_chat_window("-100"), [])
        self.assertNotEqual(await self.store.record_new_message(msg("-200")), first_id)
        self.assertNotEqual(
            await self.store.record_new_message(msg(msg_id=None)),
            await self.store.record_new_message(msg(msg_id=None)),
        )

    async def test_recent_window_order_limit_and_deleted_excluded(self):
        for i in range(1, 6):
            await self.store.record_new_message(msg(msg_id=i, text=f"m{i}"))
        await self.store.record_message_delete("-100", [5])
        window = await self.store.get_recent_chat_window("-100", limit=3)
        self.assertEqual([m.text for m in window], ["m2", "m3", "m4"])

    async def test_edit_seen_while_online_keeps_previous_text(self):
        await self.store.record_new_message(msg(text="original"))
        out = await self.store.record_message_edit("-100", 1, "changed", dom_was_online_and_read=True)
        self.assertTrue(out.is_edited)
        self.assertTrue(out.original_seen_before_edit)
        self.assertEqual(out.previous_seen_text, "original")
        self.assertEqual(out.text, "changed")
        ev = self.transcript()[-1]
        self.assertEqual((ev["event_type"], ev["old_text"], ev["new_text"]), ("message_edited", "original", "changed"))

    async def test_edit_while_offline_hides_original(self):
        await self.store.record_new_message(msg(text="original"))
        self.set_chat(is_offline=1)
        out = await self.store.record_message_edit("-100", 1, "changed", dom_was_online_and_read=False)
        self.assertTrue(out.is_edited)
        self.assertFalse(out.original_seen_before_edit)
        self.assertIsNone(out.previous_seen_text)
        xml = Store.format_chat_window_xml("-100", await self.store.get_recent_chat_window("-100"))
        self.assertIn('edited="true"', xml)
        self.assertIn("changed", xml)
        self.assertNotIn("original", xml)
        self.assertNotIn("previous_seen_text", xml)

    async def test_edit_while_sleeping_hides_original(self):
        await self.store.record_new_message(msg(text="original"))
        self.set_chat(is_sleeping=1, is_offline=1)
        out = await self.store.record_message_edit("-100", 1, "changed")
        self.assertFalse(out.original_seen_before_edit)

    async def test_edit_inferred_seen_from_online_chat_state(self):
        await self.store.record_new_message(msg(text="original"))  # chat defaults to online/awake
        out = await self.store.record_message_edit("-100", 1, "changed")
        self.assertTrue(out.original_seen_before_edit)
        self.assertEqual(out.previous_seen_text, "original")

    async def test_edit_inferred_seen_from_read_marker_even_if_offline(self):
        await self.store.record_new_message(msg(msg_id=1, text="a"))
        await self.store.record_new_message(msg(msg_id=2, text="b"))
        self.set_chat(is_offline=1)
        await self.store.mark_chat_read("-100", 1)
        read = await self.store.record_message_edit("-100", 1, "a2")
        unread = await self.store.record_message_edit("-100", 2, "b2")
        self.assertTrue(read.original_seen_before_edit)
        self.assertFalse(unread.original_seen_before_edit)

    async def test_reedit_offline_keeps_earlier_seen_version(self):
        await self.store.record_new_message(msg(text="v1"))
        await self.store.record_message_edit("-100", 1, "v2", dom_was_online_and_read=True)
        self.set_chat(is_offline=1)
        out = await self.store.record_message_edit("-100", 1, "v3")
        self.assertEqual((out.text, out.previous_seen_text, out.original_seen_before_edit), ("v3", "v1", True))

    async def test_noop_edit_and_unknown_or_deleted_message(self):
        await self.store.record_new_message(msg(text="same"))
        out = await self.store.record_message_edit("-100", 1, "same", dom_was_online_and_read=True)
        self.assertFalse(out.is_edited)
        self.assertEqual(len(self.transcript()), 1)
        self.assertIsNone(await self.store.record_message_edit("-100", 99, "x"))
        await self.store.record_message_delete("-100", [1])
        self.assertIsNone(await self.store.record_message_edit("-100", 1, "x"))

    async def test_edit_reindexes_fts(self):
        await self.store.record_new_message(msg(text="alpha"))
        await self.store.record_message_edit("-100", 1, "bravo")
        self.assertEqual(await self.store.search_chat_history("alpha", "-100"), [])
        self.assertEqual(len(await self.store.search_chat_history("bravo", "-100")), 1)

    async def test_delete_purges_fts_and_is_idempotent(self):
        await self.store.record_new_message(msg(msg_id=1, text="secret plan"))
        await self.store.record_new_message(msg(msg_id=2, text="keep me"))
        self.assertEqual(await self.store.record_message_delete("-100", [1, 42]), 1)
        self.assertEqual(await self.store.search_chat_history("secret", "-100"), [])
        self.assertEqual(await self.store.record_message_delete("-100", [1]), 0)
        self.assertEqual(await self.store.record_message_delete("-100", []), 0)
        row = self.store._conn.execute("SELECT is_deleted, deleted_at FROM messages WHERE msg_id = 1").fetchone()
        self.assertEqual(row["is_deleted"], 1)
        self.assertIsNotNone(row["deleted_at"])
        deletes = [e for e in self.transcript() if e["event_type"] == "message_deleted"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["msg_ids"], [1])
        # FTS index stays consistent after soft deletes
        self.store._conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('integrity-check')")

    async def test_mark_chat_read_only_moves_forward(self):
        await self.store.mark_chat_read("-100", 10)
        await self.store.mark_chat_read("-100", 4)
        row = self.store._conn.execute("SELECT last_read_msg_id FROM chats WHERE chat_id = '-100'").fetchone()
        self.assertEqual(row[0], 10)


class ChatWindowXmlTests(StoreTestCase):
    async def test_xml_escapes_and_shows_previous_text_only_when_seen(self):
        await self.store.record_new_message(msg(msg_id=1, text="a </msg> & b", reply_to_msg_id=None))
        await self.store.record_new_message(msg(msg_id=2, text="two", reply_to_msg_id=1, sender_name='A"l'))
        await self.store.record_message_edit("-100", 1, "a2", dom_was_online_and_read=True)
        xml = Store.format_chat_window_xml("-100", await self.store.get_recent_chat_window("-100"))
        self.assertTrue(xml.startswith('<chat_window chat_id="-100">'))
        self.assertTrue(xml.endswith("</chat_window>"))
        self.assertIn("<previous_seen_text>a &lt;/msg&gt; &amp; b</previous_seen_text>", xml)
        self.assertIn('reply_to="1"', xml)
        self.assertIn("name='A\"l'", xml)  # quoteattr picks a safe quote
        self.assertEqual(xml.count("</msg>"), 2)


class ChatAuthorizationTests(StoreTestCase):
    async def test_only_owner_dm_starts_approved(self):
        self.assertEqual((await self.store.get_chat_auth_status(OWNER_ID))["authorization_status"], "approved")
        await self.store.record_new_message(msg("-100"))
        self.assertEqual((await self.store.get_chat_auth_status("-100"))["authorization_status"], "pending_approval")
        self.assertEqual((await self.store.get_chat_auth_status(222))["authorization_status"], "pending_approval")
        self.assertEqual(await self.store.get_chat_auth_status("-10"), {
            "chat_id": "-10", "authorization_status": "pending_approval", "title": "",
            "invited_by_handle": "", "approval_prompt_sent": False,
        })

    async def test_nothing_approved_without_owner(self):
        store = Store(self.ws / "other", owner_id=None)
        self.addCleanup(store.close)
        self.assertEqual((await store.get_chat_auth_status(OWNER_ID))["authorization_status"], "pending_approval")

    async def test_authorization_update_and_filtered_list(self):
        updated = await self.store.set_chat_auth_status(
            "-100", "ignored", title="Group", invited_by_handle="@inviter", approval_prompt_sent=True
        )
        self.assertIs(updated["approval_prompt_sent"], True)
        await self.store.mark_chat_read("-100", 42)
        await self.store.get_chat_auth_status("-200")
        updated = await self.store.set_chat_auth_status("-100", "approved", approval_prompt_sent=False)
        self.assertEqual(updated, {
            "chat_id": "-100", "authorization_status": "approved", "title": "Group",
            "invited_by_handle": "@inviter", "approval_prompt_sent": False,
        })
        self.set_chat(is_offline=1)
        rows = await self.store.list_chats("approved")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].pop("updated_at"))
        self.assertEqual(rows[0], {
            "chat_id": "-100", "authorization_status": "approved", "title": "Group",
            "preset": "", "is_sleeping": False, "is_offline": True, "last_read_msg_id": 42,
        })
        self.assertEqual(len(await self.store.list_chats()), 2)
        with self.assertRaises(ValueError):
            await self.store.set_chat_auth_status("-100", "invalid")


class PendingTurnsTests(StoreTestCase):
    async def test_queue_bounds_failure_and_restart(self):
        for msg_id in (None, 10, None, 17, 5, 5):
            await self.store.enqueue_pending_turn("-100", msg_id)
        self.assertEqual(await self.store.record_turn_failure("-100", "network timeout"), 1)
        self.assertEqual(await self.store.record_turn_failure("-100", "still unavailable"), 2)
        await self.store.enqueue_pending_turn("-100", 20)
        self.assertEqual(await self.store.list_pending_turns(max_attempts=2), [])
        other = Store(self.ws, owner_id=OWNER_ID)
        self.addCleanup(other.close)
        rows = await other.list_pending_turns()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].pop("updated_at"))
        self.assertEqual(rows[0], {
            "chat_id": "-100", "first_queued_msg_id": 5, "last_queued_msg_id": 20,
            "attempt_count": 2, "last_error": "still unavailable",
        })
        await other.complete_pending_turn("-100")
        await other.complete_pending_turn("-100")
        self.assertEqual(await other.record_turn_failure("-100", "late result"), 0)
        self.assertEqual(await self.store.list_pending_turns(), [])
        await other.enqueue_pending_turn("-100", None)
        turn, = await self.store.list_pending_turns()
        self.assertEqual((turn["first_queued_msg_id"], turn["last_queued_msg_id"], turn["attempt_count"]), (None, None, 0))


class SearchSecurityTests(StoreTestCase):
    async def asyncSetUp(self):
        await self.store.record_new_message(msg("-100", 1, "banana public", scope="global_public"))
        await self.store.record_new_message(msg("-100", 2, "banana in group100"))
        await self.store.record_new_message(msg("-200", 1, "banana in group200"))
        await self.store.record_new_message(msg(ADMIN_USER_ID, 1, "banana admin eyes only", scope="owner_private"))

    async def texts(self, requester):
        return sorted(h["text"] for h in await self.store.search_chat_history("banana", requester))

    async def test_group_sees_only_own_chat(self):
        self.assertEqual(await self.texts("-100"), ["banana in group100", "banana public"])  # public row is in -100
        self.assertEqual(await self.texts("-200"), ["banana in group200"])

    async def test_unknown_requester_sees_nothing(self):
        self.assertEqual(await self.texts("999"), [])

    async def test_admin_sees_everything(self):
        self.assertEqual(len(await self.texts(ADMIN_USER_ID)), 4)

    async def test_fts_operator_syntax_is_neutralised(self):
        self.assertEqual(await self.store.search_chat_history('banana OR "', "999"), [])
        self.assertEqual(await self.store.search_chat_history("   ", "999"), [])
        # a column filter / prefix operator in user text must not widen visibility
        hits = await self.store.search_chat_history("text:banana*", "999")
        self.assertEqual([h["text"] for h in hits if h["visibility_scope"] != "global_public"], [])


class IdentityTests(StoreTestCase):
    async def test_new_person_creates_profile_keyed_by_uid(self):
        p = await self.store.resolve_person(333, "@newbie", "Newbie")
        self.assertEqual((p.person_key, p.telegram_user_id, p.current_handle), ("uid_333", 333, "@newbie"))
        self.assertEqual(p.known_handles, ["@newbie"])
        self.assertEqual(p.profile_path, "memory/people/333.md")
        self.assertIn("Newbie", (self.ws / p.profile_path).read_text())
        again = await self.store.resolve_person(333, "@newbie", "Newbie")
        self.assertEqual(again.person_key, p.person_key)
        self.assertEqual([f.name for f in (self.ws / "memory" / "people").glob("*.md")], ["333.md"])

    async def test_existing_profile_is_never_overwritten(self):
        (self.ws / "memory" / "people").mkdir(parents=True)
        (self.ws / "memory" / "people" / "333.md").write_text("- likes tea")
        await self.store.resolve_person(333, "@newbie", "Newbie")
        self.assertEqual((self.ws / "memory" / "people" / "333.md").read_text(), "- likes tea")

    async def test_handle_change_tracked_by_user_id(self):
        await self.store.resolve_person(333, "@old", "Dude")
        p = await self.store.resolve_person(333, "new", "Dude")  # bare handle is normalised
        self.assertEqual(p.current_handle, "@new")
        self.assertEqual(p.known_handles, ["@old", "@new"])
        # missing handle later keeps the last known one
        p = await self.store.resolve_person(333, None, "Dude")
        self.assertEqual(p.current_handle, "@new")

    async def test_legacy_handle_only_row_binds_on_first_sight(self):
        rel = "memory/people/handle_alan.md"
        (self.ws / "memory" / "people").mkdir(parents=True, exist_ok=True)
        (self.ws / rel).write_text("- legacy notes")
        self.store._conn.execute(
            "INSERT INTO people_identities (person_key, telegram_user_id, current_handle, known_handles_json, "
            "display_name, preferred_name, profile_path, updated_at) VALUES "
            "('handle_alan', NULL, '@alan', '[\"@alan\"]', 'Alan', 'Alan', ?, 't')",
            (rel,),
        )
        p = await self.store.resolve_person(222, "@Alan", "Alan R")
        self.assertEqual((p.person_key, p.telegram_user_id, p.profile_path), ("handle_alan", 222, "memory/people/222.md"))
        # the legacy notes follow the person to their uid-keyed file
        self.assertEqual((self.ws / "memory" / "people" / "222.md").read_text(), "- legacy notes")
        self.assertFalse((self.ws / rel).exists())
        n = self.store._conn.execute("SELECT COUNT(*) FROM people_identities").fetchone()[0]
        self.assertEqual(n, 1)
        # second sighting resolves by id
        self.assertEqual((await self.store.resolve_person(222, None, "Alan R")).person_key, "handle_alan")


class ApprovalPromptTests(StoreTestCase):
    async def test_claim_approval_prompt_succeeds_once_per_pending_chat(self):
        results = await asyncio.gather(*(self.store.claim_approval_prompt("-100") for _ in range(5)))
        self.assertEqual(sorted(results), [False] * 4 + [True])
        await self.store.set_chat_auth_status("-100", "pending_approval", approval_prompt_sent=False)
        self.assertTrue(await self.store.claim_approval_prompt("-100"))
        # the owner's own DM is approved from the start and never needs a prompt
        self.assertFalse(await self.store.claim_approval_prompt(OWNER_ID))


class ScopeTests(StoreTestCase):
    def test_scope_for_owner_private_vs_group(self):
        self.assertEqual(self.store.scope_for(OWNER_ID, "private"), "owner_dm")
        self.assertEqual(self.store.scope_for(222, "private"), "friend_dm")
        self.assertEqual(self.store.scope_for(-1009, "group"), "group")
        self.assertEqual(self.store.scope_for(-1009, "supergroup"), "group")
        # the owner's identity never makes a group private: the chat decides
        self.assertEqual(self.store.scope_for(OWNER_ID, "group"), "group")

    def test_no_owner_means_no_owner_dm(self):
        store = Store(self.ws / "other", owner_id=None)
        self.addCleanup(store.close)
        self.assertEqual(store.scope_for(OWNER_ID, "private"), "friend_dm")


if __name__ == "__main__":
    unittest.main()
