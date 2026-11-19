"""Create a private home archive with a consistent SQLite snapshot, and restore it."""

from __future__ import annotations

import os
import re
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

_ARCHIVE_NAME = re.compile(r"dom-[0-9]{8}-[0-9]{6}\.tar\.gz")


def _home_files(tar: tarfile.TarFile, home: Path, folder: Path, destination: Path, work: Path) -> None:
    for path in sorted(folder.iterdir()):
        name = path.name
        if name.startswith("state.db") or name == ".claim" or ".bak" in name or path == work:
            continue
        if folder == destination and _ARCHIVE_NAME.fullmatch(name):
            continue
        arcname = path.relative_to(home).as_posix()
        try:
            info = tar.gettarinfo(arcname=arcname, name=str(path))
            if info is None or not (info.isfile() or info.isdir()):
                raise ValueError(f"cannot archive a link or special file: {arcname}")
            if info.isdir():
                tar.addfile(info)
            else:
                with path.open("rb") as content:
                    tar.addfile(info, content)
        except FileNotFoundError:
            continue  # a live writer may remove a temporary file
        if info.isdir():
            _home_files(tar, home, path, destination, work)


def backup(home: Path, *, keep: int = 7, destination: Path | None = None) -> Path:
    if keep < 1:
        raise ValueError("--keep must be at least 1")
    home = home.resolve()
    db = home / "state.db"
    if db.is_symlink() or not db.is_file():
        raise ValueError(f"no regular state.db in {home}")
    destination = destination.resolve() if destination is not None else home.parent / "backups"
    destination.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".dom-backup-", dir=destination) as directory:
        work = Path(directory)
        snapshot = work / "state.db"
        with (
            closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as copy,
        ):
            source.backup(copy)

        temp_archive = work / "archive.tar.gz"
        temp_archive.touch(mode=0o600)
        with tarfile.open(temp_archive, "w:gz") as tar:
            tar.add(snapshot, arcname="state.db")
            _home_files(tar, home, home, destination, work)

        stamp = datetime.now(timezone.utc)
        while True:
            archive = destination / f"dom-{stamp:%Y%m%d-%H%M%S}.tar.gz"
            try:
                os.link(temp_archive, archive)
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)

    archives = sorted(
        (path for path in destination.iterdir()
         if _ARCHIVE_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file()),
        reverse=True,
    )
    for old in archives[keep:]:
        old.unlink(missing_ok=True)
    return archive


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for member in members:
        posix, windows = PurePosixPath(member.name), PureWindowsPath(member.name)
        if (
            posix.is_absolute() or windows.anchor or ".." in posix.parts or ".." in windows.parts
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"unsafe archive member: {member.name!r}")
    return members


def restore(archive: Path, home: Path) -> None:
    if home.is_symlink() or (home.exists() and (not home.is_dir() or any(home.iterdir()))):
        raise ValueError(f"restore needs an empty home: {home}")
    home.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dom-restore-", dir=home.parent) as directory:
        staging = Path(directory)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, members=_safe_members(tar), filter="data")
        # A directory rename can replace an empty directory, and fails if it gained files meanwhile.
        staging.replace(home)
