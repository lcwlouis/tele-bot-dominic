"""Transfer owner documents through the desktop inbox and sandbox file API."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from dom.actions import SendFile
from dom.desktop.client import SandboxClient, SandboxError

MAX_FILE_BYTES = 20_000_000
MAX_RESULT_FILES = 5
MAX_INBOX_FILES = 5
MAX_INBOX_BYTES = 50_000_000
INBOX = "/home/gem/inbox"


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


async def upload_inbox(home: Path, client: SandboxClient) -> None:
    directory = _inbox(home)
    if not directory.exists():
        return
    for path in sorted(directory.iterdir()):
        if safe_filename(path.name) != path.name or path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as staged:
            data = staged.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError(f"{path.name} exceeds 20 MB")
        result = await client.upload_file(f"{INBOX}/{path.name}", data)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise SandboxError("sandbox did not confirm the file upload")
        path.unlink()


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
