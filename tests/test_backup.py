"""Backup and restore through the CLI using a temporary synthetic home."""

import io
import re
import sqlite3
import stat
import tarfile
from contextlib import closing

import pytest

from dom.cli import main


@pytest.fixture
def source(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with closing(sqlite3.connect(home / "state.db")) as conn, conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE sample (name TEXT)")
        conn.execute("INSERT INTO sample VALUES ('committed')")
    return home


def create_fixture_files(home):
    (home / "memory").mkdir()
    for name, text in {
        "SOUL.md": "A synthetic soul",
        "memory/MEMORY.md": "A synthetic note",
        ".env": "# Synthetic fixture with no secrets",
        "state.db-stale": "excluded",
        ".claim": "excluded",
        "memory/MEMORY.md.bak-old": "excluded",
    }.items():
        (home / name).write_text(text)


def test_backup_contains_home_files_and_a_private_sqlite_snapshot(source, capsys):
    create_fixture_files(source)
    with closing(sqlite3.connect(source / "state.db")) as writer:
        writer.execute("INSERT INTO sample VALUES ('second committed row')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO sample VALUES ('not committed')")

        assert main(["backup", str(source)]) == 0

        (archive,) = (source.parent / "backups").glob("*.tar.gz")
        assert re.fullmatch(r"dom-\d{8}-\d{6}\.tar\.gz", archive.name)
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        output = capsys.readouterr().out
        assert str(archive) in output and str(archive.stat().st_size) in output
        with tarfile.open(archive) as tar:
            assert {member.name for member in tar if member.isfile()} == {
                "SOUL.md", "memory/MEMORY.md", ".env", "state.db",
            }
            snapshot = source.parent / "snapshot.db"
            snapshot.write_bytes(tar.extractfile("state.db").read())
        with closing(sqlite3.connect(snapshot)) as copied:
            assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert copied.execute("SELECT COUNT(*) FROM sample").fetchone() == (2,)


def test_backup_rotation_only_removes_matching_old_archives(source, tmp_path):
    dest = tmp_path / "custom"
    dest.mkdir()
    old = dest / "dom-20000101-010101.tar.gz"
    unrelated = dest / "dom-20000101.tar.gz"
    other = dest / "weekly.tar.gz"
    for path in (old, unrelated, other):
        path.touch()

    for _ in range(3):
        assert main(["backup", str(source), "--keep", "2", "--to", str(dest)]) == 0

    assert not old.exists()
    assert unrelated.exists() and other.exists()
    assert len(list(dest.glob("dom-????????-??????.tar.gz"))) == 2


def test_backup_failure_exits_1_and_leaves_no_archive(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    assert main(["backup", str(home)]) == 1
    assert not list((tmp_path / "backups").glob("*.tar.gz"))


def test_restore_round_trip_then_refuses_the_nonempty_home(source, tmp_path):
    create_fixture_files(source)
    assert main(["backup", str(source)]) == 0
    (archive,) = (tmp_path / "backups").glob("*.tar.gz")

    restored = tmp_path / "restored"
    restored.mkdir()
    assert main(["restore", str(archive), str(restored)]) == 0
    assert (restored / "SOUL.md").read_text() == "A synthetic soul"
    assert (restored / "memory/MEMORY.md").read_text() == "A synthetic note"
    assert (restored / ".env").read_text() == "# Synthetic fixture with no secrets"
    assert not (restored / ".claim").exists()
    with closing(sqlite3.connect(restored / "state.db")) as conn:
        assert conn.execute("SELECT name FROM sample").fetchall() == [("committed",)]

    (restored / "SOUL.md").write_text("Keep this")
    assert main(["restore", str(archive), str(restored)]) == 1
    assert (restored / "SOUL.md").read_text() == "Keep this"


@pytest.mark.parametrize(("name", "kind"), [
    ("../outside", tarfile.REGTYPE),
    ("memory/../../outside", tarfile.REGTYPE),
    ("/outside", tarfile.REGTYPE),
    ("link", tarfile.SYMTYPE),
    ("link", tarfile.LNKTYPE),
    ("fifo", tarfile.FIFOTYPE),
])
def test_restore_refuses_unsafe_members_without_installing_any_files(tmp_path, name, kind):
    archive = tmp_path / "unsafe.tar.gz"
    home = tmp_path / "restored"
    home.mkdir()
    with tarfile.open(archive, "w:gz") as tar:
        valid = tarfile.TarInfo("SOUL.md")
        valid.size = 4
        tar.addfile(valid, io.BytesIO(b"test"))
        unsafe = tarfile.TarInfo(name)
        unsafe.type = kind
        unsafe.linkname = "SOUL.md"
        tar.addfile(unsafe)

    assert main(["restore", str(archive), str(home)]) == 1
    assert list(home.iterdir()) == []
    assert not (tmp_path / "outside").exists()
