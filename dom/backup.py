"""Create a private home archive with a consistent SQLite snapshot, and restore it."""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from dom import workspace

log = logging.getLogger(__name__)

ARCHIVE_NAME = re.compile(r"dom-[0-9]{8}-[0-9]{6}\.tar\.gz")


UNREADABLE = "links, pipes and files with several names"
OVER_LIMIT = "files over the size limit"
OVER_CAP = "files over the cap"


def _archive_workspace(tar: tarfile.TarFile, home: Path) -> None:
    """Archive `workspace/` without trusting any name in it. The desktop container writes there while the backup
    runs, so a file may be replaced by a link, or a second name given to one of the owner's files, at any moment.
    Each directory is opened relative to its parent with O_NOFOLLOW, each file the same way and checked on the open
    descriptor (a regular file with one name), and the bytes come from that descriptor. Everything else is skipped and
    counted. No more than the workspace's size cap is archived, and when the files do not all fit the newest are kept."""
    try:
        root = os.open(home / workspace.WORKSPACE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        log.warning("the workspace is not a plain folder, so it was not archived")
        return
    try:
        candidates: list[tuple[float, str, int]] = []
        _scan(root, workspace.WORKSPACE_DIR, candidates)
        kept, room = set(), workspace.MAX_TOTAL_BYTES
        for _, arcname, size in sorted(candidates, key=lambda item: (-item[0], item[1])):  # newest first
            if size <= room:
                kept.add(arcname)
                room -= size
        skipped = {UNREADABLE: 0, OVER_LIMIT: 0, OVER_CAP: 0}
        capped: list[str] = []
        _archive_directory(tar, root, workspace.WORKSPACE_DIR, kept, [workspace.MAX_TOTAL_BYTES], skipped, capped)
    finally:
        os.close(root)
    if any(skipped.values()):
        what = ", ".join(f"{count} {kind}" for kind, count in skipped.items() if count)
        log.warning("the workspace backup skipped %s%s", what, f" (not kept: {_names(capped)})" if capped else "")


def _names(paths: list[str], limit: int = 10) -> str:
    shown = ", ".join(path.removeprefix(workspace.WORKSPACE_DIR + "/") for path in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def _entries(directory: int) -> list[tuple[str, os.stat_result]]:
    """Every name in a directory with its status (not following links), read before any is opened. A name that vanished
    is left out."""
    with os.scandir(directory) as scan:
        names = sorted(entry.name for entry in scan)
    entries = []
    for name in names:
        try:
            entries.append((name, os.stat(name, dir_fd=directory, follow_symlinks=False)))
        except OSError:
            continue
    return entries


def _open_directory(directory: int, name: str) -> int | None:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return None  # replaced by a link since it was listed


def _skip_staging(name: str, info: os.stat_result) -> bool:
    """Only a regular file with the exact name the workspace stages with is left out of the archive: a half-written
    file that is gone a moment later. A directory or file the desktop or the owner named `.incoming-...` is kept."""
    return stat.S_ISREG(info.st_mode) and workspace.is_staging(name)


def _scan(directory: int, arcname: str, found: list[tuple[float, str, int]]) -> None:
    """The files that could be archived, with their age and size, so the cap can be spent on the newest first."""
    for name, info in _entries(directory):
        child = f"{arcname}/{name}"
        if stat.S_ISDIR(info.st_mode):
            if (sub := _open_directory(directory, name)) is not None:
                try:
                    _scan(sub, child, found)
                finally:
                    os.close(sub)
        elif stat.S_ISREG(info.st_mode) and not _skip_staging(name, info):
            if info.st_nlink == 1 and info.st_size <= workspace.MAX_FILE_BYTES:
                found.append((info.st_mtime, child, info.st_size))


def _archive_directory(
    tar: tarfile.TarFile, directory: int, arcname: str, kept: set[str], budget: list[int], skipped: dict[str, int],
    capped: list[str],
) -> None:
    info = tarfile.TarInfo(arcname)
    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o700, os.fstat(directory).st_mtime
    tar.addfile(info)
    for name, info in _entries(directory):
        if _skip_staging(name, info):
            continue
        child = f"{arcname}/{name}"
        if stat.S_ISDIR(info.st_mode):
            if (sub := _open_directory(directory, name)) is None:
                skipped[UNREADABLE] += 1
                continue
            try:
                _archive_directory(tar, sub, child, kept, budget, skipped, capped)
            finally:
                os.close(sub)
        elif stat.S_ISREG(info.st_mode):
            _archive_file(tar, directory, name, child, kept, budget, skipped, capped)
        else:
            skipped[UNREADABLE] += 1


def _archive_file(
    tar: tarfile.TarFile, directory: int, name: str, arcname: str, kept: set[str], budget: list[int],
    skipped: dict[str, int], capped: list[str],
) -> None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError:
        skipped[UNREADABLE] += 1  # replaced by a link since it was listed
        return
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            skipped[UNREADABLE] += 1
        elif info.st_size > workspace.MAX_FILE_BYTES:
            skipped[OVER_LIMIT] += 1
        elif arcname not in kept or info.st_size > budget[0]:  # not among the newest that fit, or it grew since
            skipped[OVER_CAP] += 1
            capped.append(arcname)
        else:
            data = handle.read(info.st_size + 1)
            if len(data) != info.st_size:  # it changed while it was read
                skipped[UNREADABLE] += 1
                return
            member = tarfile.TarInfo(arcname)
            member.size, member.mtime, member.mode = len(data), info.st_mtime, info.st_mode & 0o777
            tar.addfile(member, io.BytesIO(data))
            budget[0] -= len(data)


def _home_files(tar: tarfile.TarFile, home: Path, folder: Path, destination: Path, work: Path) -> None:
    for path in sorted(folder.iterdir()):
        name = path.name
        if name.startswith("state.db") or name == ".claim" or ".bak" in name or path == work:
            continue
        if folder == destination and ARCHIVE_NAME.fullmatch(name):
            continue
        if folder == home and name == "workspace":
            _archive_workspace(tar, home)  # the desktop writes there: walked by descriptors, not by name
            continue
        if folder == home and name.startswith("traces.db"):  # debug traces: large and short-lived (tracer.retention_days)
            continue
        if folder == home and name == "media":
            # The media Dom sent is large, and Telegram keeps each file's copy. Only the place photos, the owner's own
            # uploads with no other copy, are archived (a missing folder is skipped below). `media` itself is checked
            # first, so a link there can never lead the archive outside the home.
            if path.is_symlink():
                raise ValueError("cannot archive a link or special file: media")
            if not path.is_dir():
                continue
            path = path / "places"
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


def _publish(temp_archive: Path, archive: Path) -> None:
    """Atomically place `temp_archive` at `archive`; never overwrite an existing archive."""
    try:
        os.link(temp_archive, archive)
        return
    except FileExistsError:
        raise
    except OSError:
        pass  # no hard links on this filesystem: exclusive copy instead
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, temp_archive.open("rb") as source:
            shutil.copyfileobj(source, out)
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


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
                _publish(temp_archive, archive)
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)

    others = sorted(
        (path for path in destination.iterdir()
         if path != archive and ARCHIVE_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file()),
        reverse=True,
    )
    for old in others[keep - 1:]:
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
