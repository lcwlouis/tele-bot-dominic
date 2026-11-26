"""Release checks, deploys and rollbacks, against a real local git repository and a fake systemd."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from dom import release

GIT = ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com"]
CHANGELOG = (
    "# Changelog\n\n## [Unreleased]\n\nFuture work\n\n"
    "## [0.10.0] - 2026-11-20\n\nNew release notes\n\n### Fixed\n\n- One fix\n\n"
    "## [0.9.0] - 2026-11-01\n\nOld notes\n"
)


def git(repo, *args):
    return subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    (source / "CHANGELOG.md").write_text(CHANGELOG)
    (source / "marker.txt").write_text("v0.9.0\n")
    (source / ".gitattributes").write_text("version.txt export-subst\n")
    (source / "version.txt").write_text("$Format:%(describe:tags=true)$")
    git(source, "add", ".")
    git(source, "commit", "-qm", "first")
    git(source, "tag", "v0.9.0")
    (source / "marker.txt").write_text("v0.10.0\n")
    git(source, "commit", "-qam", "second")
    git(source, "tag", "-am", "release", "v0.10.0")  # annotated: the commit is the tag's target
    for name in ("v0.11.0-rc1", "extra", "release-v0.20.0", "v1.0"):
        git(source, "tag", name)
    git(source, "branch", "v99.0.0")  # a branch named like a release is never offered
    return source


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "dom"
    current = root / "releases" / "c0ffee0"
    current.mkdir(parents=True)
    (root / "releases" / "v0.6.0").mkdir()
    (root / "releases" / "0ld0ld0").mkdir()
    (root / "app").symlink_to("releases/c0ffee0")
    home = tmp_path / "home"
    home.mkdir()
    return release.Layout(root), home


@pytest.mark.parametrize("tag", ["v0.10.0", "v1.2.3", "v10.20.30"])
def test_release_tags_are_plain_numeric_versions(tag):
    assert release.release_tag(tag)


@pytest.mark.parametrize("tag", [
    "main", "v0.10.0-rc1", "refs/tags/v0.10.0", "v0.10.0\n", "0.10.0", "v1.0", "v١.٠.٠", "-v1.0.0", "v1.0.0^{}",
])
def test_anything_else_is_refused(tag):
    with pytest.raises(ValueError, match="vX.Y.Z"):
        release.release_tag(tag)


def test_offer_is_the_newest_release_tag_with_only_its_changelog_section(repo, layout):
    lay, _ = layout
    offer = release.check(lay, repo.as_uri(), "v0.9.0-2-gabc1234")
    assert offer.tag == "v0.10.0"
    assert offer.commit == git(repo, "rev-parse", "v0.10.0^{commit}")
    assert "New release notes" in offer.notes and "### Fixed" in offer.notes
    assert "Old notes" not in offer.notes and "Future work" not in offer.notes
    assert release.check(lay, repo.as_uri(), "v0.10.0") is None
    assert release.check(lay, repo.as_uri(), "v0.10.0-4-gabc1234-dirty") is None
    assert release.check(lay, repo.as_uri(), "unknown").tag == "v0.10.0"  # an unversioned build may update


def test_installed_notes_reads_the_full_release_section_and_bounds_it(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    section = release.installed_notes("v0.10.0", tmp_path)
    assert section.startswith("## [0.10.0]") and "One fix" in section
    assert "Old notes" not in section and "Future work" not in section
    huge = "## [0.10.0] - 2026-11-20\n\n" + "x" * (release.HISTORY_NOTES_LIMIT + 100)
    (tmp_path / "CHANGELOG.md").write_text(huge)
    bounded = release.installed_notes("v0.10.0", tmp_path)
    assert len(bounded) <= release.HISTORY_NOTES_LIMIT + 4 and bounded.endswith("[…]")


def test_installed_notes_gives_dom_every_bullets_lead_and_first_sentence_of_the_real_changelog(tmp_path):
    import re

    from dom import own_code

    root = Path(__file__).resolve().parents[1]
    _, section = own_code.changelog_sections((root / "CHANGELOG.md").read_text())[0]  # the release in progress
    (tmp_path / "CHANGELOG.md").write_text("## [0.8.4] - 2026-11-27\n" + section.split("\n", 1)[1])
    digest = release.installed_notes("v0.8.4", tmp_path)
    leads = re.findall(r"^- (\*\*.+?\*\*)", section, re.MULTILINE)
    assert len(leads) > 40 and all(lead in digest for lead in leads)
    assert len(section) > release.HISTORY_NOTES_LIMIT > len(digest) + 2000  # the digest fits well under the limit
    assert digest.endswith('your own_code tool (action changelog, version "0.8.4"); a long section comes in parts.')
    # Every part of the section is reachable through the tool the digest names.
    parts, number = [], 1
    while True:
        text = own_code.changelog(tmp_path, "0.8.4", number)
        assert not text.startswith("error")
        body, _, footer = text.rpartition("\n[truncated: ")
        parts.append(body if footer else text)
        if not footer or "for the rest]" not in footer:
            break
        number += 1
    assert number > 1 and "\n".join(parts).replace("\n", "") == (
        "## [0.8.4] - 2026-11-27\n" + section.split("\n", 1)[1]).rstrip("\n").replace("\n", "")
    assert all(line.endswith(("…", ".", "`", ")", "!", "?", "\"")) or not line.startswith("- ") for line in digest.splitlines())


def test_a_digest_that_would_pass_the_limit_drops_whole_bullets_from_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "HISTORY_NOTES_LIMIT", 400)
    bullets = "\n".join(f"- **Change {n}.** " + "Words go here. " * 6 for n in range(20))
    (tmp_path / "CHANGELOG.md").write_text(f"## [0.8.4] - 2026-11-27\n\n{bullets}\n")
    digest = release.installed_notes("v0.8.4", tmp_path)
    kept = [line for line in digest.splitlines() if line.startswith("- ")]
    assert 0 < len(kept) < 20 and len(digest) <= 400
    assert kept == [f"- **Change {n}.** Words go here." for n in range(len(kept))]  # none cut halfway


def test_repo_url_must_be_https_or_file():
    from dom.config import UpdateConfig

    for url in ("ext::sh -c touch% /tmp/x", "-uhttps://example.com/x.git", "ssh://example.com/x.git", "http://example.com/x"):
        with pytest.raises(ValueError):
            UpdateConfig(repo_url=url)
    assert UpdateConfig(repo_url="https://example.com/dom.git")


def test_layout_is_found_from_the_running_release(layout):
    lay, _ = layout
    assert release.Layout.installed(lay.releases / "c0ffee0").root == lay.root
    with pytest.raises(release.DeployError, match="release folder"):
        release.Layout.installed(lay.releases / "v0.6.0")  # not what app points at
    with pytest.raises(release.DeployError, match="release folder"):
        release.Layout.installed(lay.root.parent / "checkout")


class FakeSystemd:
    """Starting the service "runs" whatever app points at; a healthy release writes the ready marker."""

    def __init__(self, layout, home, healthy=lambda base: True):
        self.layout, self.home, self.healthy = layout, home, healthy
        self.calls: list[tuple[str, ...]] = []
        self.pid = 100
        self.active = True

    def __call__(self, args):
        self.calls.append(tuple(args))
        if args[0] == "stop":
            self.active = False
        elif args[0] == "start":
            self.active, self.pid = True, self.pid + 1
            base = self.layout.app.resolve()
            if self.healthy(base):
                release.mark_ready(self.home, base=base, pid=self.pid)
        elif args[0] == "show":
            return f"ActiveState={'active' if self.active else 'inactive'}\nMainPID={self.pid if self.active else 0}\n"
        return ""


def run_deploy(lay, home, repo, systemd, **kwargs):
    events = []
    clock = iter(range(0, 100_000, 5))
    defaults = dict(
        sync=lambda path: events.append(("sync", path)),
        backup=lambda h: events.append(("backup", lay.app.resolve(), systemd.active)) or h / "backup.tar.gz",
        systemctl=systemd, sleep=lambda seconds: None, monotonic=lambda: next(clock), health_seconds=60,
        notify=lambda h, text: events.append(("notify", text)),
    )
    result = release.deploy(lay, home, repo.as_uri(), "v0.10.0", **{**defaults, **kwargs})
    return result, events


def test_deploy_stages_backs_up_switches_and_prunes(repo, layout):
    lay, home = layout
    previous = lay.app.resolve()
    systemd = FakeSystemd(lay, home)
    result, events = run_deploy(lay, home, repo, systemd, expect=git(repo, "rev-parse", "v0.10.0^{commit}"))
    candidate = lay.releases / "v0.10.0"
    assert result.ok, result.message
    assert "v0.10.0" in result.message
    assert lay.app.resolve() == candidate and (candidate / "marker.txt").read_text() == "v0.10.0\n"
    assert (candidate / "version.txt").read_text() == "v0.10.0"  # what dom/_version.py relies on
    assert lay.app.readlink() == Path("releases/v0.10.0")  # relative, like the documented steps
    assert ("sync", candidate) in events
    assert ("backup", previous, False) in events  # the old release, with the bot stopped
    verbs = [call[0] for call in systemd.calls if call[0] != "show"]
    assert verbs.index("stop") < verbs.index("start")
    assert {path.name for path in lay.releases.iterdir()} == {"v0.10.0", "c0ffee0"}
    state = release.read_state(home)
    assert state["status"] == "done" and state["ok"] and "v0.10.0" in state["message"]


def test_unhealthy_release_is_rolled_back_and_nothing_is_pruned(repo, layout):
    lay, home = layout
    previous = lay.app.resolve()
    systemd = FakeSystemd(lay, home, healthy=lambda base: base == previous)
    result, events = run_deploy(lay, home, repo, systemd)
    assert not result.ok and "rolled back" in result.message
    assert lay.app.resolve() == previous
    assert {path.name for path in lay.releases.iterdir()} >= {"c0ffee0", "v0.6.0", "0ld0ld0"}
    assert [call[0] for call in systemd.calls].count("start") == 2
    assert ("reset-failed", "dom.service") in systemd.calls  # a crash loop may have hit the start limit
    assert not any(event[0] == "notify" for event in events)  # the old release is up and reports it
    assert release.read_state(home)["ok"] is False


def test_owner_is_messaged_directly_when_even_the_rollback_is_down(repo, layout):
    lay, home = layout
    systemd = FakeSystemd(lay, home, healthy=lambda base: False)
    result, events = run_deploy(lay, home, repo, systemd)
    assert not result.ok and "restore" in result.message
    assert ("notify", result.message) in events


def test_staging_failure_never_stops_the_bot(repo, layout):
    lay, home = layout
    systemd = FakeSystemd(lay, home)

    def broken(path):
        raise subprocess.CalledProcessError(1, ["uv", "sync"])

    result, events = run_deploy(lay, home, repo, systemd, sync=broken)
    assert not result.ok and "uv sync" in result.message
    assert systemd.calls == [] and lay.app.resolve() == lay.releases / "c0ffee0"
    assert not (lay.releases / "v0.10.0").exists()


def test_backup_failure_restarts_the_current_release_without_switching(repo, layout):
    lay, home = layout
    systemd = FakeSystemd(lay, home)

    def broken(h):
        raise OSError("disk full")

    result, _ = run_deploy(lay, home, repo, systemd, backup=broken)
    assert not result.ok and "backup" in result.message.lower()
    assert lay.app.resolve() == lay.releases / "c0ffee0"
    assert systemd.calls[-2:] == [("reset-failed", "dom.service"), ("start", "dom.service")]


def test_moved_tag_or_bad_ref_is_refused_before_any_change(repo, layout):
    lay, home = layout
    systemd = FakeSystemd(lay, home)
    result, _ = run_deploy(lay, home, repo, systemd, expect="a" * 40)
    assert not result.ok and "changed" in result.message
    assert systemd.calls == [] and not (lay.releases / "v0.10.0").exists()
    with pytest.raises(ValueError, match="vX.Y.Z"):
        release.deploy(lay, home, repo.as_uri(), "v99.0.0^{}")
    result = release.deploy(lay, home, repo.as_uri(), "v99.0.0", systemctl=systemd)  # a branch, not a tag
    assert not result.ok and "no release tag" in result.message and systemd.calls == []


def test_the_running_release_is_not_redeployed(repo, layout):
    lay, home = layout
    systemd = FakeSystemd(lay, home)
    run_deploy(lay, home, repo, systemd)
    systemd.calls.clear()
    result, _ = run_deploy(lay, home, repo, systemd)
    assert not result.ok and "already running" in result.message and systemd.calls == []


def test_one_deploy_at_a_time(repo, layout):
    lay, home = layout
    with release.deploy_lock(lay):
        with pytest.raises(release.DeployError, match="already running"):
            with release.deploy_lock(lay):
                pass


def test_a_refused_second_deploy_leaves_the_running_deploys_state_alone(repo, layout):
    lay, home = layout
    running = {"status": "pending", "tag": "v0.10.0"}
    with release.deploy_lock(lay):
        release.write_state(home, running)
        result, events = run_deploy(lay, home, repo, FakeSystemd(lay, home))
    assert not result.ok and "already running" in result.message and events == []
    assert release.read_state(home) == running


def test_launch_runs_the_deploy_in_its_own_transient_unit(layout, monkeypatch):
    lay, home = layout
    commands = []
    monkeypatch.setattr(release.subprocess, "run", lambda args, **kw: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    offer = release.Release("v0.10.0", "b" * 40, "notes")
    release.launch(lay, home, offer, python=Path("/opt/dom/python"))
    [command] = commands
    assert command[:2] == ["systemd-run", "--user"] and "--unit=dom-update" in command
    assert command[command.index("--") + 1:] == [
        "/opt/dom/python", "-m", "dom", "deploy", str(home), "v0.10.0", "--root", str(lay.root), "--expect", "b" * 40,
    ]
    assert release.read_state(home)["status"] == "pending"


async def test_result_is_sent_once_and_kept_until_sent(layout):
    _, home = layout
    release.write_state(home, {"status": "pending"})
    sent = []

    async def send(text):
        sent.append(text)

    async def no_wait(seconds):
        release.write_state(home, {"status": "done", "ok": True, "message": "Updated to v0.10.0."})

    async def offline(text):
        raise OSError("offline")

    assert not await release.deliver_result(home, offline, sleep=no_wait, timeout=10)
    assert release.read_state(home)["status"] == "done"
    assert await release.deliver_result(home, send, sleep=no_wait, timeout=10)
    assert not await release.deliver_result(home, send, sleep=no_wait, timeout=10)
    assert sent == ["Updated to v0.10.0."] and release.read_state(home) is None


async def test_successful_update_records_installed_release_notes_once(make_app, monkeypatch):
    from dom.telegram import owner
    from test_owner_events import events

    app = make_app()
    state = {"status": "done", "ok": True, "tag": "v0.10.0", "message": "Updated."}
    monkeypatch.setattr(release, "read_state", lambda home: state)
    monkeypatch.setattr(release, "installed_notes", lambda tag: "## [0.10.0]\n\n- Full release section")

    async def deliver(home, send, **kwargs):
        await send("Updated to v0.10.0.")
        return True

    monkeypatch.setattr(release, "deliver_result", deliver)
    for _ in range(2):
        owner.watch_update(app)
        await asyncio.gather(*tuple(owner._update_reports))
        await asyncio.sleep(0)
    notes = [row["text"] for row in await events(app)]
    assert notes == ["## [0.10.0]\n\n- Full release section"]


async def test_failed_update_records_no_release_notes(make_app, monkeypatch):
    from dom.telegram import owner
    from test_owner_events import events

    app = make_app()
    state = {"status": "done", "ok": False, "tag": "v0.10.0", "message": "Rolled back."}
    monkeypatch.setattr(release, "read_state", lambda home: state)
    monkeypatch.setattr(
        release, "installed_notes", lambda tag: pytest.fail("failed update must not read or record release notes"),
    )

    async def deliver(home, send, **kwargs):
        await send("Rolled back to v0.9.0.")
        return True

    monkeypatch.setattr(release, "deliver_result", deliver)
    owner.watch_update(app)
    await asyncio.gather(*tuple(owner._update_reports))
    await asyncio.sleep(0)
    assert [row["text"] for row in await events(app)] == ["Rolled back to v0.9.0."]


def test_ready_marker_records_this_process(layout):
    _, home = layout
    release.mark_ready(home, base=Path("/x/releases/v1.0.0"), pid=7)
    assert json.loads((home / release.READY).read_text()) == {"base": "/x/releases/v1.0.0", "pid": 7}
