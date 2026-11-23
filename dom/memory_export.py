"""A zip of everything Dom remembers, for the owner: SOUL, memory/, private/, skills and presets.

It is a whitelist of folders, so `.env`, `state.db`, backups and transcripts can never be swept in; `dom backup`
is the full copy. It runs in a thread (callers use `asyncio.to_thread`) and stops at a size cap.
"""

from __future__ import annotations

import io
import os
import stat
import zipfile
from datetime import datetime
from pathlib import Path

MAX_EXPORT_BYTES = 20 * 1024 * 1024  # of file content, before compression; Telegram accepts documents up to 50 MB
ROOT_FILES = ("SOUL.md",)
ROOT_FOLDERS = ("memory", "private", "skills", "presets")


class ExportTooLarge(Exception):
    pass


def export_name(now: datetime) -> str:
    return f"dom-memory-{now:%Y%m%d-%H%M%S}.zip"


def _skip_file(name: str) -> bool:
    lower = name.lower()
    return lower.startswith(".env") or ".bak" in lower or lower.endswith((".orig", ".db", ".db-wal", ".db-shm", ".db-journal"))


def _files(home: Path) -> list[Path]:
    """Regular files only: no symlinks, no secrets, no databases, no backups, wherever they sit under a folder."""
    found = [home / name for name in ROOT_FILES]
    for folder in ROOT_FOLDERS:
        base = home / folder
        if base.is_symlink() or not base.is_dir():
            continue
        for root, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d.lower() != "backups" and not (Path(root) / d).is_symlink())
            found.extend(Path(root) / name for name in sorted(names) if not _skip_file(name))
    return [path for path in found if _regular(path)]


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def build_export(home: Path, max_bytes: int = MAX_EXPORT_BYTES) -> bytes:
    """The zip's bytes. Raises ExportTooLarge when the files add up to more than `max_bytes`."""
    home = home.resolve()
    out = io.BytesIO()
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in _files(home):
            try:
                with path.open("rb") as handle:
                    data = handle.read(max_bytes - total + 1)  # never more than the budget left, plus one byte to notice
            except OSError:
                continue  # a live writer may have removed it
            total += len(data)
            if total > max_bytes:
                raise ExportTooLarge(f"Dom's memory is over {max_bytes // (1024 * 1024)} MB; use `dom backup` for a full copy.")
            archive.writestr(path.relative_to(home).as_posix(), data)
    return out.getvalue()
