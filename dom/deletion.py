"""Deletion archive: move removed files and DB snapshots into backups/deleted/ with crash recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dom.store import Store

log = logging.getLogger(__name__)
DELETED_DIR_NAME = "deleted"
STATE_FILE = "state"
MANIFEST_FILE = "manifest.json"
PRUNE_DAYS = 30
PRUNE_MAX_ENTRIES = 50
STALE_MESSAGE = "This changed since you confirmed."
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9._-]+")


def deleted_root(home: Path) -> Path:
    """Where deletion archives live, beside the scheduled home backups."""
    return home.resolve().parent / "backups" / DELETED_DIR_NAME


def stamp_token(stamp: str) -> str:
    """Short deterministic fingerprint of a creation timestamp for compact Telegram callbacks."""
    return sha256(stamp.encode("utf-8")).hexdigest()[:16]


def stamp_matches(actual: str | None, expected: str | None) -> bool:
    """True if `expected` is the exact creation timestamp or its 16-char fingerprint."""
    if not actual or not expected:
        return False
    return expected == actual or (len(expected) == 16 and expected == stamp_token(actual))


def hash_matches(actual: str | None, expected: str | None) -> bool:
    """True if `expected` is the exact 64-char SHA-256 hex digest or a valid prefix of >= 12 chars."""
    if not actual or not expected:
        return False
    if expected == actual:
        return True
    return 12 <= len(expected) < len(actual) and actual.startswith(expected)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        pending = Path(temp.name)
        try:
            os.chmod(pending, 0o600)
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
            temp.close()
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def _safe_label(value: object) -> str:
    cleaned = _SAFE_LABEL.sub("_", str(value)).strip("._-")[:64]
    return cleaned or "item"


def _make_folder(root: Path, stamp: datetime, kind: str, item: object) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = f"{stamp.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{_safe_label(kind)}-{_safe_label(item)}"
    candidate = root / base
    suffix = 2
    while True:
        try:
            candidate.mkdir(mode=0o700, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = root / f"{base}-{suffix}"
            suffix += 1


@dataclass
class DeletionArchive:
    folder: Path
    kind: str
    item: str
    deleted_at: str
    event_text: str
    event_at: str
    rows: dict[str, Any]
    sources: list[Path]
    _moved: list[tuple[Path, Path]] = field(default_factory=list)

    def _manifest_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "item": self.item,
            "deleted_at": self.deleted_at,
            "event": self.event_text,
            "event_at": self.event_at,
            "rows": self.rows,
            "files": [
                {"source": str(src), "archived": f"files/{idx}", "name": src.name}
                for idx, src in enumerate(self.sources)
            ],
        }

    def write_manifest(self) -> None:
        _write_atomic(self.folder / MANIFEST_FILE, json.dumps(self._manifest_payload(), ensure_ascii=False, indent=2) + "\n")

    def update_rows(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.write_manifest()

    def move_in(self) -> None:
        """Move each existing source path into this archive using os.replace. Rolls back on error."""
        if not self.sources:
            return
        files_dir = self.folder / "files"
        files_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            for idx, src in enumerate(self.sources):
                if not src.exists():
                    continue
                if src.is_symlink():
                    raise ValueError(f"Refusing to archive symlink: {src.name}")
                dst = files_dir / str(idx)
                os.replace(src, dst)
                self._moved.append((src, dst))
        except BaseException:
            self._restore_moved()
            raise

    def _restore_moved(self) -> None:
        for src, dst in reversed(self._moved):
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dst, src)
        self._moved.clear()

    def rollback(self) -> None:
        """Move any archived files back to their original paths and remove this pending archive."""
        self._restore_moved()
        # Also restore anything recorded in sources that ended up on disk (e.g. if called twice).
        for idx, src in enumerate(self.sources):
            dst = self.folder / "files" / str(idx)
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dst, src)
        shutil.rmtree(self.folder, ignore_errors=True)

    def commit(self) -> None:
        """Mark the archive committed after the DB transaction has succeeded."""
        _write_atomic(self.folder / STATE_FILE, "committed\n")


def stage_deletion(
    home: Path,
    kind: str,
    item: object,
    *,
    event_text: str,
    event_at: str,
    rows: dict[str, Any],
    sources: list[Path] | None = None,
    now: datetime | None = None,
) -> DeletionArchive:
    """Create a pending deletion archive folder with manifest.json and state=pending."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    folder = _make_folder(deleted_root(home), stamp, kind, item)
    archive = DeletionArchive(
        folder=folder,
        kind=kind,
        item=str(item),
        deleted_at=stamp.isoformat(),
        event_text=event_text,
        event_at=event_at,
        rows=rows,
        sources=list(sources or ()),
    )
    try:
        _write_atomic(folder / STATE_FILE, "pending\n")
        archive.write_manifest()
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return archive


def _archive_time(folder: Path) -> datetime:
    manifest_path = folder / MANIFEST_FILE
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("deleted_at"), str):
                dt = datetime.fromisoformat(data["deleted_at"])
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (OSError, ValueError):
            pass
    return datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc)


def _read_state(folder: Path) -> str:
    state_path = folder / STATE_FILE
    if not state_path.is_file():
        return ""
    try:
        return state_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def prune_deletions(
    home: Path,
    *,
    now: datetime | None = None,
    max_age_days: int = PRUNE_DAYS,
    max_entries: int = PRUNE_MAX_ENTRIES,
) -> int:
    """Remove committed deletion folders older than `max_age_days` or beyond `max_entries`."""
    root = deleted_root(home)
    if not root.is_dir() or root.is_symlink():
        return 0
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=max_age_days)
    committed: list[tuple[datetime, str, Path]] = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        if _read_state(entry) != "committed":
            continue
        try:
            committed.append((_archive_time(entry), entry.name, entry))
        except OSError:
            continue
    committed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    pruned = 0
    for index, (stamp, _, folder) in enumerate(committed):
        if index >= max_entries or stamp < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            pruned += 1
    return pruned


async def recover_deletions(home: Path, store: Store, *, now: datetime | None = None) -> dict[str, int]:
    """Startup sweep: finish committed half-done deletion folders, roll back uncommitted ones, then prune."""
    root = deleted_root(home)
    finished = rolled_back = 0
    if root.is_dir() and not root.is_symlink():
        for folder in sorted(root.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            if _read_state(folder) == "committed":
                continue
            manifest_path = folder / MANIFEST_FILE
            manifest = None
            if manifest_path.is_file():
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        manifest = loaded
                except (OSError, ValueError):
                    manifest = None
            if manifest is None:
                files_dir = folder / "files"
                if not files_dir.exists() or not any(files_dir.iterdir()):
                    shutil.rmtree(folder, ignore_errors=True)
                    rolled_back += 1
                else:
                    log.warning("leaving unreadable pending deletion folder with files: %s", folder.name)
                continue
            event_text = manifest.get("event")
            event_at = manifest.get("event_at")
            if (
                isinstance(event_text, str) and isinstance(event_at, str)
                and await store.has_owner_event(event_text, event_at)
            ):
                await asyncio.to_thread(_write_atomic, folder / STATE_FILE, "committed\n")
                finished += 1
                continue
            # Uncommitted: restore any moved files back to their original paths and remove the folder.
            restored_ok = True
            for entry in reversed(manifest.get("files") or []):
                if not isinstance(entry, dict):
                    continue
                archived_rel, source_str = entry.get("archived"), entry.get("source")
                if not isinstance(archived_rel, str) or not isinstance(source_str, str):
                    continue
                archived = folder / archived_rel
                source = Path(source_str)
                if archived.exists() and not source.exists():
                    try:
                        source.parent.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(os.replace, archived, source)
                    except OSError:
                        log.warning("could not restore %s from pending deletion %s", source, folder.name)
                        restored_ok = False
            if restored_ok:
                await asyncio.to_thread(shutil.rmtree, folder, True)
                rolled_back += 1
    pruned = await asyncio.to_thread(prune_deletions, home, now=now)
    return {"finished": finished, "rolled_back": rolled_back, "pruned": pruned}
