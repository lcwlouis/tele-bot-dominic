"""A zip of everything Dom remembers, for the owner: SOUL, memory/, private/, skills and presets.

It is a whitelist of folders, so `.env`, `state.db`, backups and transcripts can never be swept in; `dom backup`
is the full copy. It runs in a thread (callers use `asyncio.to_thread`) and stops at a size cap.
"""

from __future__ import annotations

import io
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


def _files(home: Path) -> list[Path]:
    found = [home / name for name in ROOT_FILES]
    for folder in ROOT_FOLDERS:
        base = home / folder
        if base.is_symlink() or not base.is_dir():
            continue
        found.extend(sorted(base.rglob("*")))
    # rglob does not enter linked folders; a linked file is skipped, so nothing outside the home gets in
    return [path for path in found if path.is_file() and not path.is_symlink() and ".bak" not in path.name]


def build_export(home: Path, max_bytes: int = MAX_EXPORT_BYTES) -> bytes:
    """The zip's bytes. Raises ExportTooLarge when the files add up to more than `max_bytes`."""
    home = home.resolve()
    out = io.BytesIO()
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in _files(home):
            try:
                data = path.read_bytes()
            except OSError:
                continue  # a live writer may have removed it
            total += len(data)
            if total > max_bytes:
                raise ExportTooLarge(f"Dom's memory is over {max_bytes // (1024 * 1024)} MB; use `dom backup` for a full copy.")
            archive.writestr(path.relative_to(home).as_posix(), data)
    return out.getvalue()
