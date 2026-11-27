"""The photos, voice notes and video notes Dom sends, and the frame strips of the clips he is sent, kept as files under
the home.

Each file is `media/<chat id>/<YYYY-MM>/<msg id>.<ext>`; the message's media_json records its path, size, the scene or
speech text, kind, time and Telegram's `file_id`. A clip someone sent keeps only its frame strips, as
`<msg id>-strip<N>.jpg` (`strips` in media_json, `bytes` their total), so a later description can look at them again;
they count toward the same cap and go the same way. The folder is private (0700) and a chat's files are read only
through that chat's own id, so they never reach another chat's prompt. Files are written to a temporary name and moved
into place, so a cancelled or crashed save leaves nothing half-written. Total size is capped: past
`studio.keep_cap_mb` the oldest files are removed, and their messages keep the `file_id`, so Telegram can still
provide them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dom.store import Store

log = logging.getLogger("dom.media_files")
MEDIA_DIR = "media"
_PATH = re.compile(r"media/(-?\d+)/(\d{4}-\d{2})/(-?\d+)(?:-still|-strip\d+)?\.(?:png|jpg|webp|ogg|mp4|mp3)")
MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "ogg": "audio/ogg", "mp4": "video/mp4", "mp3": "audio/mpeg"}


def extension(kind: str, data: bytes) -> str:
    if kind == "photo":
        return "jpg" if data.startswith(b"\xff\xd8\xff") else "webp" if data[8:12] == b"WEBP" else "png"
    if kind == "voice":
        return "mp3" if data.startswith((b"ID3", b"\xff\xfb")) else "ogg"
    return "mp4"


def relative_path(chat_id: int, msg_id: int, kind: str, data: bytes, at: datetime) -> str:
    return f"{MEDIA_DIR}/{chat_id}/{at:%Y-%m}/{msg_id}.{extension(kind, data)}"


def files_of(media: dict) -> list[str]:
    """Every relative path a message's media_json records: its file, the still it was filmed from, its frame strips."""
    strips = media.get("strips")
    found = [media.get("file"), media.get("still_file"), *(strips if isinstance(strips, list) else ())]
    return [str(path) for path in found if path]


def file_id_of(sent: object, kind: str) -> str | None:
    """Telegram's file_id for what a send call just delivered."""
    found = getattr(sent, {"photo": "photo", "voice": "voice", "video_note": "video_note"}.get(kind, kind), None)
    if isinstance(found, list):  # a photo arrives in several sizes, smallest first
        found = found[-1] if found else None
    value = getattr(found, "file_id", None)
    return value if isinstance(value, str) else None


class MediaFiles:
    def __init__(self, home: Path, store: Store, cap_bytes: Callable[[], int]):
        self.home = Path(home)
        self.store = store
        self._cap = cap_bytes  # read each time, so a config change applies at once
        self._evicting = asyncio.Lock()

    @property
    def cap_bytes(self) -> int:
        return self._cap()

    def _absolute(self, rel: str, chat_id: int | str | None = None) -> Path | None:
        """The file for a stored relative path, or None when it isn't one of ours (or belongs to another chat)."""
        match = _PATH.fullmatch(rel)
        if match is None or (chat_id is not None and match.group(1) != str(chat_id)):
            return None
        return self.home / rel

    def save(self, rel: str, data: bytes) -> None:
        """Write the file atomically with private permissions. Blocking: run it in a thread."""
        path = self.home / rel
        for folder in (path.parent.parent.parent, path.parent.parent, path.parent):
            folder.mkdir(mode=0o700, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    async def keep(
        self, chat_id: int, msg_id: int, kind: str, data: bytes, at: datetime, still: bytes | None = None,
    ) -> dict:
        """Save one generated file and return the media_json fields that describe it. Nothing is saved when the cap is
        zero or the file alone is bigger than the cap. A video note's `still` (the picture it was filmed from) is saved
        beside it as `<msg id>-still.png` when both fit; `bytes` then counts the two, so the cap and eviction treat the
        pair as one item."""
        cap = self.cap_bytes
        if cap <= 0 or len(data) > cap:
            return {}
        rel = relative_path(chat_id, msg_id, kind, data, at)
        fields = {"file": rel, "bytes": len(data)}
        if still is not None and len(data) + len(still) <= cap:
            still_rel = rel.rpartition(".")[0] + "-still.png"
            await asyncio.to_thread(self.save, still_rel, still)
            fields |= {"still_file": still_rel, "bytes": len(data) + len(still)}
        await asyncio.to_thread(self.save, rel, data)
        return fields

    async def keep_strips(self, chat_id: int, msg_id: int, images: list[bytes], at: datetime) -> dict:
        """Save the frame strips of an incoming clip and return the media_json fields that describe them. Nothing is
        saved when the cap is zero or the strips together are bigger than the cap."""
        cap = self.cap_bytes
        total = sum(len(image) for image in images)
        if cap <= 0 or not images or total > cap:
            return {}
        stem = f"{MEDIA_DIR}/{chat_id}/{at:%Y-%m}/{msg_id}"
        paths = [f"{stem}-strip{number}.jpg" for number in range(1, len(images) + 1)]
        for rel, image in zip(paths, images):
            await asyncio.to_thread(self.save, rel, image)
        return {"strips": paths, "bytes": total}

    async def read(self, chat_id: int | str, rel: str) -> bytes | None:
        """A kept file's bytes, only through its own chat's id."""
        path = self._absolute(rel, chat_id)
        if path is None:
            return None

        def load() -> bytes | None:
            try:
                return path.read_bytes()
            except OSError:
                return None

        return await asyncio.to_thread(load)

    async def usage(self) -> dict:
        rows = await self.store.kept_media()
        return {
            "files": len(rows), "bytes": sum(int(row["media"].get("bytes") or 0) for row in rows),
            "cap_bytes": self.cap_bytes,
        }

    async def reconcile(self) -> int:
        """Remove files no message records (the process died between saving and recording) and half-written temp
        files, so the cap, which counts recorded files, is also the disk's real use. Run it at startup, before
        anything is being saved. Returns how many went."""
        known = {path for row in await self.store.kept_media() for path in files_of(row["media"])}
        root = self.home / MEDIA_DIR

        def sweep() -> int:
            removed = 0
            for path in root.glob("*/*/*"):
                if path.is_file() and path.relative_to(self.home).as_posix() not in known:
                    path.unlink(missing_ok=True)
                    removed += 1
            return removed

        removed = await asyncio.to_thread(sweep)
        if removed:
            log.info("removed %d kept media files no message records", removed)
        return removed

    async def evict(self) -> int:
        """Remove the oldest files until the total fits the cap; return how many went. The messages keep their
        file_id (and scene or speech text), and lose the path."""
        async with self._evicting:
            rows = await self.store.kept_media(oldest_first=True)
            total = sum(int(row["media"].get("bytes") or 0) for row in rows)
            cap, removed = self.cap_bytes, 0
            for row in rows:
                if total <= cap:
                    break
                media = row["media"]
                for rel in files_of(media):
                    path = self._absolute(rel)
                    if path is not None:
                        await asyncio.to_thread(path.unlink, True)
                await self.store.update_media(
                    row["chat_id"], row["msg_id"], {"removed": True}, remove=("file", "bytes", "still_file", "strips"),
                )
                total -= int(media.get("bytes") or 0)
                removed += 1
            if removed:
                log.info("removed %d kept media files over the %d byte cap", removed, cap)
            return removed
