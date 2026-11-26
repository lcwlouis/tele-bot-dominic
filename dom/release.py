"""Release updates: find the newest `vX.Y.Z` tag and deploy it into the release layout.

    ~/dom/app -> releases/<name>    the running release (a relative symlink)
    ~/dom/releases/<name>/          one unpacked tag each, with its own .venv
    ~/dom/repo.git                  a bare cache of the public repository's release tags

`dom deploy` (run by hand, or detached by `/update` through a transient systemd unit) stages the tag
while the bot keeps running, then stops the bot, takes a backup, repoints `app` and starts the bot.
The new process marks itself ready; if it is not ready and stable within the deadline, the previous
release is put back. The outcome is saved in the instance home, and the process that comes up sends
it to the owner. Only tags that are exactly `vX.Y.Z` are ever fetched or deployed.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dom import own_code

log = logging.getLogger(__name__)

TAG = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
RUNNING = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)(?:-[0-9]+-g[0-9a-f]+)?(?:-dirty)?")
SERVICE = "dom.service"
UNIT = "dom-update"
STATE = "update-state.json"
READY = "ready.json"
HEALTH_SECONDS = 120
STABLE_SECONDS = 10  # the new process must keep the same PID this long after it is ready
POLL_SECONDS = 2
NOTES_LIMIT = 3000
HISTORY_NOTES_LIMIT = 12_000
GIT_TIMEOUT = 300
SYNC_TIMEOUT = 1800
RESULT_TIMEOUT = 2400  # how long a starting bot waits for a deploy it finds in progress


class DeployError(Exception):
    """A deploy cannot start, with a message for the owner."""


class DeployBusy(DeployError):
    """Another deploy holds the lock. It owns the saved state, so this one must not touch it."""


def release_tag(tag: object) -> tuple[int, int, int]:
    """The version numbers of a release tag; anything but exactly `vX.Y.Z` is refused."""
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        raise ValueError("release tags look like vX.Y.Z")
    major, minor, patch = map(int, match.groups())
    return major, minor, patch


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str
    notes: str


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def cache(self) -> Path:
        return self.root / "repo.git"

    def current(self) -> Path:
        if not self.app.is_symlink():
            raise DeployError(f"{self.app} is not a symlink to a release folder")
        return self.app.resolve(strict=True)

    @classmethod
    def installed(cls, base: Path | None = None) -> Layout:
        """The layout this code runs from: `<root>/releases/<name>`, with `<root>/app` pointing at it."""
        base = (base or own_code.installed_base()).resolve()
        app = base.parent.parent / "app"
        if base.parent.name != "releases" or not app.is_symlink() or app.resolve() != base:
            raise DeployError(
                "Dom is not running from a release folder (releases/<name> behind the app symlink), "
                "so it cannot update itself. See docs/deploy-pi.md."
            )
        return cls(base.parent.parent)


# ---- tags and notes ----------------------------------------------------------------

def _git(*args: str, timeout: float = GIT_TIMEOUT) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=timeout, env=env,
    ).stdout


def fetch(layout: Layout, repo_url: str) -> dict[str, str]:
    """Fetch the repository's release tags into the cache: {tag: commit}. Other refs are never fetched."""
    cache = str(layout.cache)
    if not (layout.cache / "HEAD").is_file():
        layout.cache.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "--quiet", cache)
    _git("-C", cache, "fetch", "--quiet", "--no-tags", "--prune", "--", repo_url, "+refs/tags/v*:refs/tags/v*")
    listing = _git(
        "-C", cache, "for-each-ref",
        "--format=%(refname:strip=2)%09%(objecttype)%09%(objectname)%09%(*objecttype)%09%(*objectname)", "refs/tags",
    )
    tags = {}
    for line in listing.splitlines():
        name, kind, target, peeled_kind, peeled = (line.split("\t") + [""] * 5)[:5]
        commit = target if kind == "commit" else peeled if peeled_kind == "commit" else ""
        if TAG.fullmatch(name) and commit:
            tags[name] = commit
    return tags


def notes(layout: Layout, tag: str) -> str:
    """The tag's own CHANGELOG.md section, shortened to fit a message."""
    version = ".".join(map(str, release_tag(tag)))
    try:
        text = _git("-C", str(layout.cache), "show", f"refs/tags/{tag}:CHANGELOG.md")
    except subprocess.CalledProcessError:
        text = ""
    for title, section in own_code.changelog_sections(text):
        if title.startswith(f"[{version}]"):
            section = section.strip()
            return section if len(section) <= NOTES_LIMIT else section[:NOTES_LIMIT].rstrip() + "\n[…]"
    return f"{tag} has no changelog section."


_BULLET = re.compile(r"-\s+(\*\*.+?\*\*)?\s*(.*)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")
DIGEST_SENTENCE = 160
DIGEST_CLOSING = (
    'This is each change in short. Read the full section with your own_code tool (action changelog, version "{version}"); '
    "a long section comes in parts."
)


def _digest(section: str, version: str) -> str:
    """Every bullet of a changelog section as its bold lead and first sentence, headings kept, never cut mid-bullet.
    Dom reads this once after an update; the whole text stays one `own_code` call away. Text with no bullets is kept as is."""
    lines, bullets = [], 0
    for line in section.splitlines():
        match = _BULLET.fullmatch(line.strip())
        if match is None:
            if line.strip().startswith("-"):
                continue
            lines.append(line.rstrip())
            continue
        bullets += 1
        lead, rest = match.groups()
        first = _SENTENCE_END.split(" ".join(rest.split()), maxsplit=1)[0]
        if len(first) > DIGEST_SENTENCE:  # a long sentence loses its tail at a word, so the digest stays well under the limit
            first = first[:DIGEST_SENTENCE - 1].rpartition(" ")[0].rstrip(" ,;:-") + "…"
        lines.append("- " + " ".join(part for part in (lead, first) if part))
    if not bullets:
        return section
    closing = DIGEST_CLOSING.format(version=version)
    while len("\n".join([*lines, "", closing])) > HISTORY_NOTES_LIMIT:  # whole bullets only, from the end
        last = max(index for index, line in enumerate(lines) if line.startswith("- "))
        del lines[last]
    return "\n".join([*lines, "", closing])


def installed_notes(tag: str, base: Path | None = None) -> str:
    """The installed release's changelog section for Dom's chat history: every bullet as its bold lead and first
    sentence (see `_digest`), bounded rather than Telegram-sized."""
    version = ".".join(map(str, release_tag(tag)))
    path = (base or own_code.installed_base()) / "CHANGELOG.md"
    try:
        text = path.read_text() if path.is_file() and not path.is_symlink() else ""
    except OSError:
        text = ""
    for title, section in own_code.changelog_sections(text):
        if title.startswith(f"[{version}]"):
            digest = _digest(section.strip(), version)
            return digest if len(digest) <= HISTORY_NOTES_LIMIT else digest[:HISTORY_NOTES_LIMIT].rstrip() + "\n[…]"
    return f"## [{version}]\n\nNo changelog section was installed."


def check(layout: Layout, repo_url: str, running: str) -> Release | None:
    """The newest release tag newer than the running version, or None. An unversioned build may update."""
    tags = fetch(layout, repo_url)
    match = RUNNING.fullmatch(running)
    floor = tuple(map(int, match.groups())) if match else (-1, -1, -1)
    newer = [(release_tag(tag), tag) for tag in tags if release_tag(tag) > floor]
    if not newer:
        return None
    _, tag = max(newer)
    return Release(tag, tags[tag], notes(layout, tag))


# ---- state shared with the bot -----------------------------------------------------

def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value))
    os.replace(temp, path)


def read_state(home: Path) -> dict | None:
    return _read_json(Path(home) / STATE)


def write_state(home: Path, state: dict) -> None:
    _write_json(Path(home) / STATE, state)


def mark_ready(home: Path, *, base: Path | None = None, pid: int | None = None) -> None:
    """Called by the bot once it has started: the deploy's health check waits for this."""
    _write_json(Path(home) / READY, {
        "base": str(base or own_code.installed_base()), "pid": pid if pid is not None else os.getpid(),
    })


async def deliver_result(
    home: Path, send: Callable[[str], Awaitable[None]], *,
    sleep: Callable[[float], Awaitable[None]], timeout: float = RESULT_TIMEOUT,
) -> bool:
    """Wait for a deploy in progress to finish, send its outcome once, then forget it."""
    waited = 0.0
    while (state := read_state(home)) is not None and state.get("status") != "done":
        if waited >= timeout:
            log.warning("gave up waiting for the update in progress to report")
            return False
        await sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    if state is None:
        return False
    try:
        await send(str(state.get("message") or "The update finished."))
    except Exception:
        log.warning("could not send the update result; it is kept for the next start", exc_info=True)
        return False
    if read_state(home) == state:  # a newer update may have started meanwhile
        (Path(home) / STATE).unlink(missing_ok=True)
    return True


# ---- deploy ------------------------------------------------------------------------

@contextmanager
def deploy_lock(layout: Layout) -> Iterator[None]:
    layout.root.mkdir(parents=True, exist_ok=True)
    with open(layout.root / "deploy.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployBusy("Another deploy is already running.") from None
        yield


def _uv_sync(path: Path) -> None:
    uv = shutil.which("uv") or shutil.which("uv", path=str(Path.home() / ".local" / "bin"))
    if uv is None:
        raise DeployError("uv was not found on PATH or in ~/.local/bin")
    env = {key: value for key, value in os.environ.items() if key not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    subprocess.run([uv, "sync", "--frozen", "--no-dev"], cwd=path, check=True, timeout=SYNC_TIMEOUT, env=env)


def _backup(home: Path) -> Path:
    from dom.backup import backup

    return backup(home)


def _systemctl(args: list[str]) -> str:
    return subprocess.run(
        ["systemctl", "--user", *args], check=True, capture_output=True, text=True, timeout=180,
    ).stdout


def _notify_owner(home: Path, text: str) -> None:
    """Last resort when no Dom process is up to report: send through the Bot API directly."""
    import httpx

    from dom.config import load_config

    try:
        config = load_config(home)
        token = config.secret("TELEGRAM_BOT_TOKEN")
        if token and config.owner_id is not None:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": config.owner_id, "text": text}, timeout=30,
            ).raise_for_status()
    except Exception as exc:
        log.error("could not message the owner directly (%s)", type(exc).__name__)


def _stage(layout: Layout, tag: str, sync: Callable[[Path], None]) -> Path:
    """Unpack the tag into releases/<tag> and install its locked dependencies there."""
    target = layout.releases / tag
    layout.releases.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{tag}-", dir=layout.releases) as work:
        archive, unpacked = Path(work) / "release.tar", Path(work) / "tree"
        with archive.open("wb") as out:
            # git archive fills dom/_version.py (export-subst), so the release reports its tag.
            subprocess.run(
                ["git", "-C", str(layout.cache), "archive", "--format=tar", f"refs/tags/{tag}"],
                stdout=out, stderr=subprocess.PIPE, check=True, timeout=GIT_TIMEOUT,
            )
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter="data")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)  # a leftover from an earlier attempt; never the running release
        unpacked.rename(target)
    sync(target)  # in place: the virtual environment records its own path
    return target


def _switch(layout: Layout, target: str) -> None:
    """Point app at target (relative to the root) in one rename."""
    staged = layout.root / "app.next"
    staged.unlink(missing_ok=True)
    staged.symlink_to(target)
    os.replace(staged, layout.app)


def _start(home: Path, systemctl: Callable[[list[str]], str]) -> None:
    (Path(home) / READY).unlink(missing_ok=True)
    try:
        systemctl(["reset-failed", SERVICE])  # a crash loop may have hit the unit's start limit
    except (OSError, subprocess.SubprocessError):
        pass
    systemctl(["start", SERVICE])


def _healthy(
    home: Path, base: Path, systemctl: Callable[[list[str]], str], seconds: float,
    sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> bool:
    """The service is active, its main process is `base` and marked itself ready, and it stays up."""
    deadline = monotonic() + seconds
    seen: tuple[str, float] | None = None
    while True:
        try:
            output = systemctl(["show", SERVICE, "-p", "ActiveState", "-p", "MainPID"])
        except (OSError, subprocess.SubprocessError):
            output = ""
        props = dict(line.partition("=")[::2] for line in output.splitlines())
        ready = _read_json(Path(home) / READY) or {}
        pid = props.get("MainPID", "0")
        if props.get("ActiveState") == "active" and pid != "0" and str(ready.get("pid")) == pid \
                and ready.get("base") == str(base):
            now = monotonic()
            if seen is None or seen[0] != pid:
                seen = (pid, now)
            elif now - seen[1] >= STABLE_SECONDS:
                return True
        else:
            seen = None
        if monotonic() >= deadline:
            return False
        sleep(POLL_SECONDS)


def prune(layout: Layout, keep: set[Path]) -> None:
    """Remove every release folder except `keep`. Failures are logged, not raised."""
    for entry in layout.releases.iterdir():
        if entry.is_symlink() or not entry.is_dir() or entry.resolve() in keep:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            log.warning("could not remove old release %s (%s)", entry.name, exc)


def _command_name(error: subprocess.CalledProcessError) -> str:
    command = [str(part) for part in error.cmd] if isinstance(error.cmd, (list, tuple)) else [str(error.cmd)]
    return " ".join([Path(command[0]).name, *command[1:2]])


def deploy(
    layout: Layout, home: Path, repo_url: str, tag: str, *, expect: str | None = None,
    sync: Callable[[Path], None] = _uv_sync, backup: Callable[[Path], Path] = _backup,
    systemctl: Callable[[list[str]], str] = _systemctl, health_seconds: float = HEALTH_SECONDS,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    notify: Callable[[Path, str], None] = _notify_owner,
) -> Result:
    """Deploy release `tag`. The outcome is also saved for the bot to report."""
    release_tag(tag)
    try:
        with deploy_lock(layout):
            try:
                result = _deploy(layout, Path(home), repo_url, tag, expect, sync, backup, systemctl,
                                 health_seconds, sleep, monotonic, notify)
            except DeployError as exc:
                result = Result(False, str(exc))
            write_state(home, {"status": "done", "ok": result.ok, "tag": tag, "message": result.message})
    except DeployBusy as exc:
        result = Result(False, str(exc))  # the deploy that holds the lock reports; this one saves nothing
    (log.info if result.ok else log.error)("%s", result.message)
    return result


def _deploy(layout, home, repo_url, tag, expect, sync, backup, systemctl, health_seconds, sleep, monotonic, notify):
    current = layout.current()
    candidate = layout.releases / tag
    try:
        commit = fetch(layout, repo_url).get(tag)
    except (OSError, subprocess.SubprocessError):
        return Result(False, f"Could not fetch release tags from {repo_url}.")
    if commit is None:
        return Result(False, f"There is no release tag {tag} in {repo_url}.")
    if expect is not None and commit != expect:
        return Result(False, f"{tag} changed since it was offered. Run /update again.")
    if candidate.resolve() == current:
        return Result(False, f"{tag} is already running.")
    try:
        _stage(layout, tag, sync)
    except (OSError, subprocess.SubprocessError, tarfile.TarError, DeployError) as exc:
        shutil.rmtree(candidate, ignore_errors=True)
        reason = f"{_command_name(exc)} failed" if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        return Result(False, f"Could not stage {tag}: {reason}. Dom kept running {current.name}.")

    previous = os.readlink(layout.app)
    write_state(home, {"status": "pending", "tag": tag})
    try:
        systemctl(["stop", SERVICE])
        try:
            archive = backup(home)
        except Exception as exc:
            _start(home, systemctl)
            return Result(False, f"The backup failed ({type(exc).__name__}: {exc}), so {tag} was not deployed. "
                                 f"Dom is running {current.name} again.")
        _switch(layout, f"releases/{tag}")
        _start(home, systemctl)
        if _healthy(home, candidate.resolve(), systemctl, health_seconds, sleep, monotonic):
            prune(layout, {candidate.resolve(), current})
            return Result(True, f"Updated to {tag}. Backup: {archive.name}. Kept {current.name} for a rollback.")
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("deploy step failed: %s", exc)
        archive = None
    # Put the previous release back.
    try:
        _switch(layout, previous)
        try:
            systemctl(["stop", SERVICE])
        except (OSError, subprocess.SubprocessError):
            pass
        _start(home, systemctl)
        back = _healthy(home, current, systemctl, health_seconds, sleep, monotonic)
    except (OSError, subprocess.SubprocessError):
        back = False
    logs = "journalctl --user -u dom.service"
    if back:
        return Result(False, f"{tag} did not start cleanly within {health_seconds:g} seconds, so Dom rolled back "
                             f"to {current.name}. Logs: {logs}")
    restore = f" If {tag} changed the database, restore {archive.name} with the old release's dom restore." if archive else ""
    message = (f"{tag} did not start cleanly and the rollback to {current.name} did not come up either. "
               f"Logs: {logs}.{restore} See docs/deploy-pi.md to restore by hand.")
    notify(home, message)
    return Result(False, message)


def launch(layout: Layout, home: Path, offer: Release, python: Path | None = None) -> None:
    """Run `dom deploy` for an offered release in a transient user unit, outside the bot's own service.

    The fixed unit name means a second update cannot start while one runs.
    """
    base = own_code.installed_base()
    python = python or base / ".venv" / "bin" / "python"  # the resolved release, not the app symlink
    write_state(home, {"status": "pending", "tag": offer.tag})
    command = [
        "systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet", f"--working-directory={base}",
        "--", str(python), "-m", "dom", "deploy", str(home), offer.tag,
        "--root", str(layout.root), "--expect", offer.commit,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        (Path(home) / STATE).unlink(missing_ok=True)
        raise DeployError(f"Could not start the update ({type(exc).__name__}).") from None
    if result.returncode:
        (Path(home) / STATE).unlink(missing_ok=True)
        busy = "already" in result.stderr or "exists" in result.stderr
        raise DeployError("An update is already running." if busy else
                          f"Could not start the update: {result.stderr.strip()[:300]}")
