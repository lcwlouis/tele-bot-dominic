"""The shared workspace: one folder, `<home>/workspace`, that Dom's file tools, the owner's desktop and the dashboard's
Files page all use, and the safe file access every one of them goes through.

The desktop sandbox mounts the folder at `/workspace`, so the sandbox's browser and anything it runs can put files
there, including links and special files that point at the host. Every access therefore walks the path one directory at
a time with `O_NOFOLLOW`, relative to the directory it just opened (so a directory swapped for a link between a check
and the use cannot lead out), and only touches regular files and directories. A path with `..`, a leading `/`, a
backslash or a hidden part is refused. Sizes are capped. `private/`, `.env`, the database and the rest of the home are
not under the folder, so nothing here can reach them; the home's readable folders (memory, media, briefs, skills) get
their own read-only view for the dashboard.

Everything here blocks: call it through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from io import BytesIO
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from dom.models import IncomingMessage
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

WORKSPACE_DIR = "workspace"
SHARED_DIR = "shared"  # inside the workspace: what groups and friends may read when the owner turns that on
INBOX_DIR = "inbox"  # documents sent to Dom land here (or in shared/inbox/<chat> elsewhere than the owner DM)
MOUNT = "/workspace"  # where the owner's desktop sees the folder
HOME_READABLE = ("memory", "media", "briefs", "skills")  # the dashboard may read these beside the workspace

MAX_FILE_BYTES = 20_000_000  # a document that lands or is uploaded, and the most a download returns
MAX_TEXT_BYTES = 5_000_000  # the largest text file Dom reads
MAX_WRITE_CHARS = 1_000_000  # the most Dom writes in one call
MAX_TOTAL_BYTES = 500_000_000  # everything in the workspace
READ_CHARS = 12_000  # one read's worth of text; the result says where the next starts
MAX_ENTRIES = 500
MAX_WALK = 200_000  # files counted when measuring the workspace
USAGE_TTL = 5.0  # seconds a measurement of the workspace is reused
MAX_DEPTH = 8
MAX_NAME = 120
MAX_PDF_BYTES = 10_000_000  # a PDF is not parsed at all above this
PDF_SECONDS = 30  # wall-clock limit on reading one
PDF_WORKER = Path(__file__).with_name("pdf_worker.py")
DOWNLOAD_TIMEOUT = 60  # seconds a document may take to download

TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".yaml", ".yml", ".toml", ".ini", ".xml",
    ".html", ".htm", ".rst", ".tex", ".py", ".js", ".ts", ".sh", ".sql", ".ics",
})
PDF_SUFFIX = ".pdf"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


class WorkspaceError(ValueError):
    """A refusal with a sentence fit to show the owner or the model; it never carries a path outside the workspace."""


def workspace_dir(home: Path) -> Path:
    return Path(home) / WORKSPACE_DIR


def ensure_workspace(home: Path) -> Path:
    """The workspace folder, created owner-only. A link in its place is refused: it would lead out of the home."""
    folder = workspace_dir(home)
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        pass
    if folder.is_symlink() or not folder.is_dir():
        raise WorkspaceError("The workspace must be a regular folder in the home.")
    (folder / SHARED_DIR).mkdir(mode=0o755, exist_ok=True)
    return folder


class BoundedFile(BytesIO):
    """A download target that stops at the file limit, so a lying or huge document cannot fill memory."""

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_FILE_BYTES:
            raise WorkspaceError(f"documents can be at most {MAX_FILE_BYTES // 1_000_000} MB")
        return super().write(data)


def safe_name(raw: str | None) -> str:
    """A file name safe to create: no folders, nothing hidden, plain characters, a bounded length."""
    base = PurePosixPath((raw or "").replace("\\", "/")).name
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", base).strip("._-")
    stem, dot, suffix = cleaned.rpartition(".")
    if dot and len(suffix) <= 10 and stem:
        return stem[: MAX_NAME - len(suffix) - 1] + "." + suffix
    return cleaned[:MAX_NAME] or "document"


def _parts(rel: str) -> tuple[str, ...]:
    if not isinstance(rel, str) or "\0" in rel or "\\" in rel or len(rel) > 1000:
        raise WorkspaceError("That is not a path in the workspace.")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise WorkspaceError("Paths are relative to the workspace, with no '..'.")
    parts = tuple(part for part in pure.parts if part != ".")
    if len(parts) > MAX_DEPTH or any(len(part) > 255 or part.startswith(".") for part in parts):
        raise WorkspaceError("That is not a path in the workspace.")
    return parts


STAGING = re.compile(r"\.incoming-[0-9a-f]{12}")  # the names `Tree._stage` makes: a half-written file, gone a moment later


def is_staging(name: str) -> bool:
    """Whether a file name is one `Tree._stage` could have made. Any other `.incoming-` name is someone's file."""
    return STAGING.fullmatch(name) is not None


def _mtime(info: os.stat_result) -> str:
    return datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds")


class Tree:
    """A folder to read, and (when `writable`) to change, as one rooted, link-free view.

    `top` limits the first level to those names (the dashboard's view of the home). `hidden` names paths, as parts
    below the root, that behave as if they did not exist."""

    def __init__(
        self, root: Path, *, top: tuple[str, ...] | None = None, writable: bool = True,
        hidden: Callable[[tuple[str, ...]], bool] | None = None,
    ):
        self.root = Path(root)
        self.top = top
        self.writable = writable
        self._hidden = hidden

    # ---- walking ------------------------------------------------------------------------------

    def _check(self, rel: str, *, write: bool = False) -> tuple[str, ...]:
        if write and not self.writable:
            raise WorkspaceError("This folder is read-only.")
        parts = _parts(rel)
        if self.top is not None and parts and parts[0] not in self.top:
            raise WorkspaceError("Not found.")
        if self._hidden is not None and self._hidden(parts):
            raise WorkspaceError("Not found.")
        return parts

    @contextlib.contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
        """An open file descriptor for the directory at `parts`, reached without following a link."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            fd = os.open(self.root, flags)
        except OSError:
            raise WorkspaceError("The folder is not available.") from None
        try:
            for part in parts:
                try:
                    if create:
                        try:
                            os.mkdir(part, 0o755, dir_fd=fd)
                        except FileExistsError:
                            pass
                    following = os.open(part, flags, dir_fd=fd)
                except OSError:
                    raise WorkspaceError("Not found.") from None
                os.close(fd)
                fd = following
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _entry(self, rel: str, *, create_parents: bool = False, write: bool = False) -> Iterator[tuple[int, str]]:
        """The directory holding `rel` (opened) and the file's name in it."""
        parts = self._check(rel, write=write)
        if not parts:
            raise WorkspaceError("Name a file or folder.")
        with self._directory(parts[:-1], create=create_parents) as parent:
            yield parent, parts[-1]

    @staticmethod
    def _lstat(parent: int, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise WorkspaceError("Not found.") from None

    def _open_file(self, rel: str) -> tuple[int, os.stat_result]:
        with self._entry(rel) as (parent, name):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError:
                raise WorkspaceError("Not found, or not a regular file.") from None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise WorkspaceError("That is not a regular file.")
        if info.st_nlink > 1:
            # The desktop can hard-link any file it can reach into another folder, past the folder rules (the shared
            # folder, another chat's inbox). Which name it is read by no longer says whose it is, so it is not read.
            os.close(fd)
            raise WorkspaceError("This file has several links; copy it to read it.")
        return fd, info

    # ---- reading ------------------------------------------------------------------------------

    def list(self, rel: str = "") -> dict:
        parts = self._check(rel)
        with self._directory(parts) as directory:
            entries, truncated = [], False
            with os.scandir(directory) as scan:
                for entry in sorted(scan, key=lambda item: item.name):
                    if entry.name.startswith(".") or (self.top is not None and not parts and entry.name not in self.top):
                        continue
                    if self._hidden is not None and self._hidden((*parts, entry.name)):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        kind = "dir"
                    elif stat.S_ISREG(info.st_mode):
                        kind = "file"
                    else:  # links, sockets, pipes and devices are never shown
                        continue
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    entries.append({"name": entry.name, "kind": kind, "size": info.st_size if kind == "file" else None,
                                    "modified": _mtime(info)})
        entries.sort(key=lambda item: (item["kind"] != "dir", item["name"].lower()))
        return {"path": "/".join(parts), "entries": entries, "truncated": truncated}

    def stat(self, rel: str) -> dict:
        """A regular file's size, to tell the page it is there before it asks for it."""
        fd, info = self._open_file(rel)
        os.close(fd)
        return {"size": info.st_size, "modified": _mtime(info)}

    def read_bytes(self, rel: str, *, limit: int = MAX_FILE_BYTES) -> tuple[str, bytes]:
        fd, info = self._open_file(rel)
        with os.fdopen(fd, "rb") as handle:
            if info.st_size > limit:
                raise WorkspaceError(f"That file is larger than {limit // 1_000_000} MB.")
            return PurePosixPath(rel).name, handle.read(limit + 1)[:limit]

    def read_text(self, rel: str, offset: int = 0) -> dict:
        """The text of a text, Markdown, CSV or JSON file, `READ_CHARS` at a time from `offset`. A PDF goes through
        `read_document`, which parses it in a process of its own."""
        if PurePosixPath(rel).suffix.lower() not in TEXT_SUFFIXES:
            raise WorkspaceError("I can read text, Markdown, CSV, JSON and PDF files; that type is not one of them.")
        name, data = self.read_bytes(rel, limit=MAX_TEXT_BYTES)
        return _part(name, data.decode("utf-8", errors="replace"), offset)

    # ---- changing -----------------------------------------------------------------------------

    def used_bytes(self) -> int:
        """Bytes in every regular file below the root, without following links. A walk that grows past `MAX_WALK`
        entries (a flood of tiny files) stops and counts as over the cap."""
        total, seen = 0, 0
        for folder, _, names in os.walk(self.root, followlinks=False):
            for name in names:
                seen += 1
                if seen > MAX_WALK:
                    return MAX_TOTAL_BYTES + 1
                try:
                    info = os.lstat(os.path.join(folder, name))
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def _room(self, size: int) -> None:
        if self.used_bytes() + size > MAX_TOTAL_BYTES:
            raise WorkspaceError(f"The workspace is full ({MAX_TOTAL_BYTES // 1_000_000} MB). Delete something first.")

    @staticmethod
    def _stage(parent: int, data: bytes) -> str:
        """Write `data` to a hidden temporary file in the directory and return its name (see `is_staging`)."""
        name = f".incoming-{os.urandom(6).hex()}"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=parent)
            raise
        return name

    def write_text(self, rel: str, content: str) -> int:
        """Create or replace a text file. The new content is staged and moved over, so a crash leaves the old file."""
        if not isinstance(content, str) or len(content) > MAX_WRITE_CHARS:
            raise WorkspaceError(f"Write at most {MAX_WRITE_CHARS // 1000} thousand characters at a time.")
        data = content.encode("utf-8")
        with self._entry(rel, create_parents=True, write=True) as (parent, name):
            if name.startswith(".") or len(name) > MAX_NAME:
                raise WorkspaceError("Choose a plain file name.")
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise WorkspaceError("That name is taken by something that is not a file.")
            self._room(len(data) - (existing.st_size if existing is not None else 0))
            temporary = self._stage(parent, data)
            try:
                os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)  # atomic; it replaces a file
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=parent)
                raise WorkspaceError("Could not save that file.") from None
        return len(data)

    def save_new(self, folder: str, name: str | None, data: bytes) -> str:
        """Add a file under a name no other file has (`-2`, `-3` are added when it is taken), in `folder`, which is
        created. Returns the path relative to the root. For documents that arrive and uploads."""
        if len(data) > MAX_FILE_BYTES:
            raise WorkspaceError(f"Files can be at most {MAX_FILE_BYTES // 1_000_000} MB.")
        wanted = safe_name(name)
        stem, suffix = os.path.splitext(wanted)
        parts = self._check(folder, write=True)
        with self._directory(parts, create=True) as parent:
            self._room(len(data))
            temporary = self._stage(parent, data)
            try:
                number, candidate = 2, wanted
                while True:
                    try:
                        os.link(temporary, candidate, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                        break
                    except FileExistsError:
                        tail = f"-{number}{suffix}"
                        candidate = stem[: MAX_NAME - len(tail)] + tail
                        number += 1
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=parent)
        return "/".join((*parts, candidate))

    def move(self, source: str, target: str) -> None:
        with self._entry(source, write=True) as (from_dir, from_name), \
                self._entry(target, create_parents=True, write=True) as (to_dir, to_name):
            info = self._lstat(from_dir, from_name)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise WorkspaceError("Only files and folders can be moved.")
            if to_name.startswith(".") or len(to_name) > 255:
                raise WorkspaceError("Choose a plain name.")
            try:
                os.stat(to_name, dir_fd=to_dir, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceError("Something is already there. Move it somewhere else first.")
            try:
                os.rename(from_name, to_name, src_dir_fd=from_dir, dst_dir_fd=to_dir)
            except OSError:
                raise WorkspaceError("Could not move that.") from None

    def delete(self, rel: str) -> str:
        """Delete a file, or an empty folder. Returns "file" or "folder"."""
        with self._entry(rel, write=True) as (parent, name):
            info = self._lstat(parent, name)
            try:
                if stat.S_ISREG(info.st_mode):
                    os.unlink(name, dir_fd=parent)
                    return "file"
                if stat.S_ISDIR(info.st_mode):
                    os.rmdir(name, dir_fd=parent)
                    return "folder"
            except OSError:
                raise WorkspaceError("Could not delete that; a folder must be empty first.") from None
        raise WorkspaceError("Only files and empty folders can be deleted.")


def _part(name: str, text: str, offset: object) -> dict:
    offset = max(0, offset) if isinstance(offset, int) else 0
    chunk = text[offset:offset + READ_CHARS]
    more = len(text) > offset + READ_CHARS
    return {"name": name, "text": chunk, "offset": offset, "next_offset": offset + len(chunk) if more else None}


async def read_document(tree: Tree, rel: str, offset: int = 0) -> dict:
    """`Tree.read_text` for any readable type. A PDF is untrusted input that can cost a parser unbounded time and memory,
    so it is parsed in a separate process with memory and CPU limits, a wall-clock timeout and a cap on its output,
    and the process is killed if the timeout passes or the caller is cancelled."""
    if PurePosixPath(rel).suffix.lower() != PDF_SUFFIX:
        return await asyncio.to_thread(tree.read_text, rel, offset)
    name, data = await asyncio.to_thread(tree.read_bytes, rel, limit=MAX_PDF_BYTES)  # the size is checked before parsing
    offset = max(0, offset) if isinstance(offset, int) else 0
    return _part(name, await _pdf_text(data, offset + READ_CHARS + 1), offset)  # one more, to know there is more


async def _pdf_text(data: bytes, wanted: int) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(PDF_WORKER), str(wanted),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env={},
    )
    try:
        try:
            async with asyncio.timeout(PDF_SECONDS):
                out, _ = await process.communicate(data)
        except TimeoutError:
            raise WorkspaceError("Reading that PDF took too long, so I stopped.") from None
    finally:
        if process.returncode is None:  # a timeout, a cancel or any other way out: nothing is left running
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.shield(process.wait())
    if process.returncode == 0:
        return out.decode("utf-8", errors="replace")
    if process.returncode == 3:
        raise WorkspaceError("That PDF has no text I can read (it may be scanned pages).")
    if process.returncode == 4:
        raise WorkspaceError("That PDF is password-protected.")
    log.warning("could not read a PDF (the reader ended with %s)", process.returncode)
    raise WorkspaceError("I could not read the text of that PDF.")


def workspace(home: Path) -> Tree:
    """The owner's view: the whole workspace, which is created on first use."""
    return Tree(ensure_workspace(home))


def shared_for(home: Path, chat_id: int) -> Tree:
    """What groups and friends read: `workspace/shared/`, read-only, without other chats' landed documents."""
    own = str(chat_id)

    def other_chats(parts: tuple[str, ...]) -> bool:
        return len(parts) >= 2 and parts[0] == INBOX_DIR and parts[1] != own

    return Tree(ensure_workspace(home) / SHARED_DIR, writable=False, hidden=other_chats)


def home_view(home: Path) -> Tree:
    """The dashboard's read-only view of what Dom may read in his home: memory, media, briefs and skills."""
    return Tree(Path(home), top=HOME_READABLE, writable=False)


# ---- the size cap and the mount's state ---------------------------------------------------------------------


_measured: dict[str, tuple[float, int]] = {}
FULL_FLAG = "workspace:full_notified"
MOUNT_STATE = "workspace-mount.json"  # beside the config: why the desktop was started without the workspace, if it was


def measure(home: Path, *, fresh: bool = False) -> int:
    """Bytes in the workspace, reused for `USAGE_TTL` seconds. Blocks (a walk): call it through a thread."""
    now = time.monotonic()
    hit = _measured.get(str(home))
    if hit is not None and not fresh and now - hit[0] < USAGE_TTL:
        return hit[1]
    used = Tree(workspace_dir(home)).used_bytes()
    _measured[str(home)] = (now, used)
    return used


async def check_cap(app: App, *, fresh: bool = False) -> bool:
    """Whether the workspace is over its size cap. The cap is soft: a bind mount has no quota, so the desktop can fill
    the disk behind the file tools' backs, and one download or write can overshoot before the next look. It is looked at
    between desktop steps (from the short-lived measurement) and, with `fresh`, at the boundaries that act on the answer:
    a task ending and a new one starting. The first time it is over, the owner gets one quiet notice with a link to
    Files; the notice comes again only after it has been under."""
    used = await asyncio.to_thread(measure, app.home.root, fresh=fresh)
    full = used > MAX_TOTAL_BYTES
    flagged = bool(await app.store.kv_get(FULL_FLAG))
    if full and not flagged:
        await app.store.kv_set(FULL_FLAG, "1")
        if app.owner_id is not None:
            from dom.notice import send_notice

            try:
                await send_notice(
                    app, app.owner_id, "The workspace is full",
                    [f"It holds {used // 1_000_000} MB of its {MAX_TOTAL_BYTES // 1_000_000} MB. Dom's desktop stops and "
                     "Dom can't save files until you delete some."],
                    path="/app/work/files", label="Open Files", disable_notification=True,
                )
            except Exception as error:
                log.warning("could not tell the owner the workspace is full (%s)", type(error).__name__)
    elif not full and flagged:
        await app.store.kv_set(FULL_FLAG, "")
    return full


FULL_MESSAGE = "The workspace is full, so the desktop stopped. Delete some files in Work → Files, then ask again."


def mount_problem(home: Path) -> str | None:
    """Why the desktop runs without the workspace (it could not write there as its user), or None."""
    try:
        return json.loads((Path(home) / MOUNT_STATE).read_text()).get("problem") or None
    except (OSError, ValueError, AttributeError):
        return None


def set_mount_problem(home: Path, problem: str | None) -> None:
    path = Path(home) / MOUNT_STATE
    if problem is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(MOUNT_STATE + ".tmp")
    temporary.write_text(json.dumps({"problem": problem}))
    os.replace(temporary, path)


# ---- documents sent to Dom (Telegram and the Talk page) ---------------------------------------------------------------


def destination(app: App, msg: IncomingMessage) -> tuple[Tree, str, str] | None:
    """Where a document in this chat lands: the tree to write to, the folder in it, and the path a reader in that chat
    uses. None when documents do not land here (anywhere but the owner DM, unless the owner shares the workspace)."""
    home = app.home.root
    if msg.chat_type == "private" and app.owner_id is not None and msg.chat_id == app.owner_id:
        return workspace(home), INBOX_DIR, INBOX_DIR
    if app.config.workspace.share_with_chats:
        folder = f"{SHARED_DIR}/{INBOX_DIR}/{msg.chat_id}"
        return workspace(home), folder, f"{INBOX_DIR}/{msg.chat_id}"
    return None


def keep(tree: Tree, folder: str, seen: str, name: str, mime: str, data: bytes) -> dict:
    """Save a document and return the media fields that say where it is and what it is (name, type, size)."""
    final = tree.save_new(folder, name, data).rsplit("/", 1)[-1]
    path = f"{seen}/{final}"
    kilobytes = max(1, round(len(data) / 1000))
    return {"saved": path, "name": final, "description": f"{final} ({mime}, {kilobytes} KB), saved as {path}"}


_MIME = re.compile(r"[a-z0-9][a-z0-9.+-]{0,60}/[a-z0-9][a-z0-9.+-]{0,60}")


def clean_mime(value: object) -> str:
    """The type a sender's client claims, if it looks like one: it is shown to Dom, so nothing else gets through."""
    mime = str(value or "").partition(";")[0].strip().lower()
    return mime if _MIME.fullmatch(mime) else "application/octet-stream"


async def document_fields(app: App, msg: IncomingMessage, data: bytes | None = None) -> dict | None:
    """Keep a document that was sent to Dom in the workspace and return the media fields that say where it is and what
    it is: the file's own name and type show in its `<media>` tag even when it could not be saved. `data` is the file
    when Dom already holds it (the dashboard's Talk page sends it with the message), so nothing is downloaded. None
    when there is nothing to land: not a document, or one that already says where it is."""
    media = msg.media or {}
    if media.get("kind") != "document" or media.get("saved") or media.get("description") is not None:
        return None
    name, mime = safe_name(media.get("name")), clean_mime(media.get("mime"))
    try:
        target = destination(app, msg)
        if target is None:
            return {"description": f"{name} ({mime})"}
        tree, folder, seen = target
        if (len(data) if data is not None else media.get("file_size") or 0) > MAX_FILE_BYTES:
            raise WorkspaceError(f"documents can be at most {MAX_FILE_BYTES // 1_000_000} MB")
        if data is None:
            with BoundedFile() as content:
                async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                    downloaded = await app.bot.download(media["file_id"], destination=content)
                if downloaded is None:
                    raise OSError("no document returned")
                data = content.getvalue()
        return await asyncio.to_thread(keep, tree, folder, seen, name, mime, data)
    except WorkspaceError as error:
        return {"description": f"{name} ({mime}), not saved: {error}"}
    except Exception as error:
        log.warning("could not save a document to the workspace (%s)", type(error).__name__)
        return {"description": f"{name} ({mime}), could not be saved"}


async def land_document(app: App, msg: IncomingMessage) -> dict | None:
    """Download a document that was sent to Dom in Telegram and keep it in the workspace, off the reply path: the
    dispatcher waits for this like any other media. The stored message's media then names the file and where Dom can
    read it."""
    fields = await document_fields(app, msg)
    if fields is not None:
        await app.store.update_media(msg.chat_id, msg.msg_id, fields)
    return fields
