"""Transfer owner documents through the desktop inbox and sandbox file API."""

from __future__ import annotations

import logging
import re
import shutil
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from dom.actions import SendFile
from dom.desktop.client import SandboxClient, SandboxError

MAX_FILE_BYTES = 20_000_000
MAX_RESULT_FILES = 5
MAX_INBOX_FILES = 5
MAX_INBOX_BYTES = 50_000_000
log = logging.getLogger(__name__)


def inbox_dir(task_id: int) -> str:
    """Where one task's documents go in the sandbox: a fresh directory per task."""
    return f"/home/gem/inbox-{task_id}"


def safe_filename(raw: str | None) -> str:
    basename = PurePosixPath((raw or "").replace("\\", "/")).name
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", basename).strip("._-")[:120] or "document"


class BoundedFile(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_FILE_BYTES:
            raise ValueError("Desktop documents can be at most 20 MB.")
        return super().write(data)


def _inbox(home: Path) -> Path:
    directory = home.resolve().parent / "desktop-inbox"
    if directory.is_symlink() or directory == home.resolve():
        raise ValueError("The desktop inbox must be a regular directory beside DOM_HOME.")
    return directory


def stage_inbox(home: Path, name: str | None, content: bytes) -> str:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Desktop documents can be at most 20 MB.")
    directory = _inbox(home)
    directory.mkdir(mode=0o700, exist_ok=True)
    pending = [path.stat().st_size for path in directory.iterdir() if path.is_file() and not path.name.startswith(".")]
    if len(pending) >= MAX_INBOX_FILES or sum(pending) + len(content) > MAX_INBOX_BYTES:
        raise ValueError("Inbox full")
    filename = safe_filename(name)
    basename, suffix = Path(filename).stem, Path(filename).suffix
    with NamedTemporaryFile(dir=directory, prefix=".incoming-") as temporary:
        temporary.write(content)
        temporary.flush()
        candidate = directory / filename
        number = 2
        while True:
            try:
                candidate.hardlink_to(temporary.name)
            except FileExistsError:
                tail = f"-{number}{suffix}"
                candidate = directory / (basename[:120 - len(tail)] + tail)
                number += 1
            else:
                return candidate.name


def bind_inbox(home: Path, task_id: int) -> None:
    """Hand every document staged so far to this task. Documents staged later belong to the next one."""
    directory = _inbox(home)
    if not directory.is_dir():
        return
    pending = [path for path in directory.iterdir() if path.is_file() and not path.is_symlink() and not path.name.startswith(".")]
    if not pending:
        return
    bound = directory / f"task-{task_id}"
    bound.mkdir(mode=0o700)
    for path in pending:
        path.rename(bound / path.name)


def discard_bound(home: Path, task_id: int) -> None:
    try:
        shutil.rmtree(_inbox(home) / f"task-{task_id}", ignore_errors=True)
    except ValueError:
        pass


def drop_stale(home: Path, keep: set[int]) -> None:
    """Remove documents bound to tasks that will never run, e.g. after a crash."""
    try:
        directory = _inbox(home)
        kept = {f"task-{task_id}" for task_id in keep}
        stale = [path for path in directory.glob("task-*") if path.name not in kept] if directory.is_dir() else []
    except (OSError, ValueError):
        return
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)


async def upload_inbox(home: Path, client: SandboxClient, task_id: int) -> list[str]:
    """Upload the task's bound documents into its own sandbox directory, then drop the local copies.

    A failed upload drops them too: the task fails and its report says so.
    """
    bound = _inbox(home) / f"task-{task_id}"
    if bound.is_symlink() or not bound.is_dir():
        return []
    uploaded = []
    try:
        for path in sorted(bound.iterdir()):
            if safe_filename(path.name) != path.name or path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as staged:
                data = staged.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise SandboxError(f"{path.name} exceeds 20 MB")
            result = await client.upload_file(f"{inbox_dir(task_id)}/{path.name}", data)
            if not isinstance(result, dict) or result.get("success") is not True:
                raise SandboxError("sandbox did not confirm the file upload")
            uploaded.append(path.name)
    except Exception:
        raise SandboxError("the attached documents could not be uploaded and were dropped") from None
    finally:
        shutil.rmtree(bound, ignore_errors=True)
    return uploaded


async def download_results(client: SandboxClient, paths: list[str]) -> list[SendFile]:
    if len(paths) > MAX_RESULT_FILES:
        raise ValueError("finish can send at most 5 files")
    if any(not isinstance(path, str) or not PurePosixPath(path).name for path in paths):
        raise ValueError("result files must be sandbox paths")
    result = []
    for path in paths:
        data = await client.download_file(path, max_bytes=MAX_FILE_BYTES)
        result.append(SendFile(safe_filename(path), data))
    return result
