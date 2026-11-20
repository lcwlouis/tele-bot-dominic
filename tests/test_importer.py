import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from dom import cli
from dom.config import load_config
from dom.home import LIMITS, init_home
from dom.importer import import_v2
from dom.store.store import MIGRATIONS_DIR, TARGET_VERSION

OWNER, FRIEND, GROUP = 111, 222, -1009


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def src(tmp_path):
    """A synthetic V2 workspace with the real folder shape and made-up content."""
    root = tmp_path / "old_workspace"
    write(root, "identity/SOUL.md", "OLD SOUL")
    write(root, "identity/SCHEDULE.md", "OLD SCHEDULE")
    write(root, "identity/TRAITS.json", '{"version": 2, "parameters": {"humor_level": 0.7}}')
    write(root, "identity/presets/classic.md", "PRESET")
    write(root, "memory/MEMORY.md", "- world fact")
    write(root, "memory/JOURNAL.md", "diary")
    write(root, "memory/people/uid_222_fran.md", "---\nperson_key: uid_222\n---\n- likes tea")
    write(root, "memory/people/handle_alan_alan.md", "- legacy handle notes")
    write(root, "memory/people/handle_corrupted_one_corrupted_one.md", "- underscore handle")
    write(root, "admin/PRIVATE_MEMORY.md", "- owner private fact")
    write(root, "admin/tasks.json", "{}")
    write(root, f"chats/{OWNER}/STATE.json", '{"chat_type": "admin_dm"}')
    write(root, f"chats/{OWNER}/CONTEXT.md", "OWNER-DM-CONTEXT")
    write(root, f"chats/{OWNER}/transcript.jsonl", '{"event_type": "message_created", "text": "dm"}\n')
    write(root, f"chats/{GROUP}/CONTEXT.md", "group running joke")
    write(root, f"chats/{GROUP}/TRAITS.json", '{"humor_level": 0.9, "emoji_level": 0.1}')
    write(root, f"chats/{GROUP}/transcript.jsonl", '{"event_type": "message_created", "text": "hi"}\n')
    write(root, "plugins/registry.json", "{}")

    db = root / "state.db"
    with sqlite3.connect(db) as conn:
        for name in ("0001_init.sql", "0002_offline_edit_tracking.sql", "0003_chat_auth_and_outbox.sql"):
            conn.executescript((MIGRATIONS_DIR / name).read_text())
        conn.executemany(
            "INSERT INTO chats (chat_id, chat_type, visibility_scope, authorization_status, updated_at) "
            "VALUES (?, ?, ?, ?, 't')",
            [(str(OWNER), "admin_dm", "admin_private", "pending_approval"),
             (str(GROUP), "group", f"chat:{GROUP}", "approved")],
        )
        conn.execute(
            "INSERT INTO messages (chat_id, msg_id, role, visibility_scope, text, created_at) "
            f"VALUES ('{GROUP}', 1, 'user', 'chat:{GROUP}', 'kept message', 't')"
        )
        conn.executemany(
            "INSERT INTO people_identities (person_key, telegram_user_id, current_handle, known_handles_json, "
            "display_name, preferred_name, profile_path, updated_at) VALUES (?, ?, ?, '[]', 'n', 'n', ?, 't')",
            [("uid_222", FRIEND, "@fran", "memory/people/uid_222_fran.md"),
             ("handle_alan", None, "@alan", "memory/people/handle_alan_alan.md")],
        )
    return root


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    init_home(path)
    return path


def test_owner_dm_context_goes_private(src, home):
    import_v2(src, home, OWNER)
    notes = (home / "private" / "NOTES.md").read_text()
    assert notes == "- owner private fact\n\nOWNER-DM-CONTEXT"
    assert not (home / "memory" / "chats" / f"{OWNER}.md").exists()
    assert (home / "memory" / "chats" / f"{GROUP}.md").read_text() == "group running joke"
    assert "OWNER-DM-CONTEXT" not in "".join(
        p.read_text() for p in (home / "memory").rglob("*.md")
    )


def test_straight_copies_and_people_renamed(src, home):
    report = import_v2(src, home, OWNER)
    assert (home / "memory" / "MEMORY.md").read_text() == "- world fact"
    assert (home / "memory" / "JOURNAL.md").read_text() == "diary"
    assert (home / "memory" / "people" / f"{FRIEND}.md").read_text().endswith("- likes tea")
    assert (home / "transcripts" / f"{GROUP}.jsonl").read_text() == '{"event_type": "message_created", "text": "hi"}\n'
    assert (home / "transcripts" / f"{OWNER}.jsonl").is_file()
    assert "memory/MEMORY.md" in report.imported


def test_handle_profiles_reported_unresolved(src, home):
    report = import_v2(src, home, OWNER)
    assert (home / "memory/people/handle_alan.md").read_text() == "- legacy handle notes"
    assert (home / "memory/people/handle_corrupted_one.md").read_text() == "- underscore handle"
    assert sorted(report.unresolved) == ["memory/people/handle_alan.md", "memory/people/handle_corrupted_one.md"]
    with sqlite3.connect(home / "state.db") as conn:
        paths = dict(conn.execute("SELECT person_key, profile_path FROM people_identities"))
    assert paths == {"uid_222": f"memory/people/{FRIEND}.md", "handle_alan": "memory/people/handle_alan.md"}


def test_over_limit_reported_not_truncated(src, home):
    big = "x" * (LIMITS["memory/MEMORY.md"] + 500)
    write(src, "memory/MEMORY.md", big)
    report = import_v2(src, home, OWNER)
    assert (home / "memory" / "MEMORY.md").read_text() == big
    assert report.over_limit == ["memory/MEMORY.md"]


def test_source_untouched(src, home):
    before = tree_hashes(src)
    listing = sorted(p.name for p in src.rglob("*"))
    import_v2(src, home, OWNER)
    assert tree_hashes(src) == before
    assert sorted(p.name for p in src.rglob("*")) == listing


def test_state_db_migrated_with_traits_and_owner_approval(src, home):
    import_v2(src, home, OWNER)
    with sqlite3.connect(home / "state.db") as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == TARGET_VERSION
        chats = {r["chat_id"]: r for r in conn.execute("SELECT * FROM chats")}
        assert json.loads(chats[str(GROUP)]["traits_json"]) == {"humor_level": 0.9, "emoji_level": 0.1}
        assert chats[str(GROUP)]["authorization_status"] == "approved"  # earlier approvals survive
        assert chats[str(OWNER)]["authorization_status"] == "approved"
        assert conn.execute("SELECT text FROM messages").fetchone()[0] == "kept message"


def test_legacy_files_go_to_legacy_not_home(src, home):
    import_v2(src, home, OWNER)
    assert (home / "SOUL.md").read_text() != "OLD SOUL"
    assert (home / "legacy" / "SOUL.md").read_text() == "OLD SOUL"
    assert (home / "legacy" / "SCHEDULE.md").is_file()
    assert (home / "legacy" / "TRAITS.json").is_file()
    assert (home / "legacy" / "presets" / "classic.md").read_text() == "PRESET"
    assert not (home / "plugins").exists()


def test_refuses_to_overwrite_existing_state_db(src, home):
    (home / "state.db").write_bytes(b"live data")
    with pytest.raises(FileExistsError):
        import_v2(src, home, OWNER)
    assert (home / "state.db").read_bytes() == b"live data"


def test_import_into_initialised_home_sets_owner(src, home):
    (home / ".claim").write_text("CODE1234")
    import_v2(src, home, OWNER)
    assert load_config(home).owner_id == OWNER
    assert not (home / ".claim").exists()
    assert (home / "SOUL.md").read_text() != "OLD SOUL"  # the new soul stays
    assert (home / "memory" / "MEMORY.md").read_text() == "- world fact"


def test_import_into_home_with_other_owner_refused(src, home):
    cli_owner = 999
    from dom.config import save_owner
    save_owner(home, cli_owner)
    with pytest.raises(ValueError):
        import_v2(src, home, OWNER)
    assert not (home / "legacy").exists()


def test_cli_import_v2_prints_report(src, home, capsys):
    assert cli.main(["import-v2", str(src), str(home), "--owner-id", str(OWNER)]) == 0
    out = capsys.readouterr().out
    assert "handle_alan.md" in out and "legacy" in out
    assert (home / "state.db").is_file()


def test_legacy_owner_differing_from_owner_id_refused_before_writing(src, home):
    with pytest.raises(ValueError, match="legacy owner"):
        import_v2(src, home, FRIEND)
    assert not (home / "state.db").exists() and not (home / "legacy").exists()


def test_legacy_owner_found_from_private_memory_when_no_state_json(src, home):
    (src / "chats" / str(OWNER) / "STATE.json").unlink()
    write(src, "admin/PRIVATE_MEMORY.md", "- see memory/people/uid_111_owner.md\n- owner private fact")
    with pytest.raises(ValueError, match="legacy owner"):
        import_v2(src, home, FRIEND)
    assert not (home / "state.db").exists() and not (home / "legacy").exists()


def test_symlinks_in_source_are_skipped_and_reported(src, home, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET OUTSIDE")
    (src / "memory" / "people" / "uid_333_eve.md").symlink_to(outside)
    (src / "memory" / "MEMORY.md").unlink()
    (src / "memory" / "MEMORY.md").symlink_to(outside)
    (src / "identity" / "presets" / "evil.md").symlink_to(outside)
    report = import_v2(src, home, OWNER)
    assert "SECRET OUTSIDE" not in "".join(p.read_text() for p in home.rglob("*") if p.is_file() and p.suffix in {".md", ".jsonl"})
    assert not any(p.is_symlink() for p in home.rglob("*"))
    assert any("uid_333_eve.md" in item for item in report.skipped)
    assert any("MEMORY.md" in item for item in report.skipped)
    assert not (home / "legacy" / "presets" / "evil.md").exists()


def test_import_requires_an_initialised_home(src, tmp_path):
    fresh = tmp_path / "fresh"
    with pytest.raises(ValueError, match="dom init"):
        import_v2(src, fresh, OWNER)
    assert not fresh.exists()
    assert cli.main(["import-v2", str(src), str(fresh), "--owner-id", str(OWNER)]) == 1
    assert not fresh.exists()


def test_imported_history_is_marked_read(src, home):
    # Otherwise Dom's first catch-up answers months-old messages in every imported chat.
    import_v2(src, home, OWNER)
    with sqlite3.connect(home / "state.db") as conn:
        rows = conn.execute(
            "SELECT c.last_read_msg_id, (SELECT MAX(msg_id) FROM messages m WHERE m.chat_id = c.chat_id) "
            "FROM chats c WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = c.chat_id)"
        ).fetchall()
    assert rows and all(read == newest for read, newest in rows)


def test_imported_history_is_not_fed_to_the_memory_pass(src, home):
    import_v2(src, home, OWNER)
    with sqlite3.connect(home / "state.db") as conn:
        rows = conn.execute("SELECT memory_cursor_msg_id, last_read_msg_id FROM chats WHERE last_read_msg_id IS NOT NULL").fetchall()
    assert rows and all(cursor == read for cursor, read in rows)


def test_preset_copied_with_normalised_sliders(src, home):
    from dom.home import Home, parse_front_matter

    write(src, "presets/assistant.md", "---\ntitle: Assistant\ndescription: Direct\ntraits:\n  humor_level: 0.3\n  singlish: 0.2\n---\nKeep it clear.\n")
    report = import_v2(src, home, OWNER)
    assert "presets/assistant.md" in report.imported
    assert parse_front_matter(Home(home).read("presets/assistant.md")) == (
        {"title": "Assistant", "description": "Direct", "traits": {"humor": 0.3, "slang": 0.2}},
        "Keep it clear.\n",
    )
