"""The security-review fixes for the shared workspace: hard links, unbounded PDF parsing, the backup walk, the size cap
the bind mount skips, an unwritable mount, and a document that arrives after the reply."""

import os
import tarfile

import pytest

from dom import backup, workspace
from dom.files_tools import OWNER_FILES_TOOL, SHARED_FILES_TOOL
from dom.config import WorkspaceConfig
from dom.workspace import Tree, WorkspaceError, ensure_workspace
from test_workspace import ctx, shared_home
from tghelpers import FRIEND, GROUP, OWNER


@pytest.fixture
def tree(tmp_path):
    return Tree(ensure_workspace(tmp_path))


# ---- 1. hard links ----------------------------------------------------------------------------------------


def test_a_file_with_several_links_is_never_read(tree, tmp_path):
    (tree.root / "mine.txt").write_text("owner only")
    os.link(tree.root / "mine.txt", tree.root / "shared" / "menu.md")  # the desktop puts it where chats can see it
    for rel in ("mine.txt", "shared/menu.md"):
        for read in (tree.read_text, tree.read_bytes, tree.stat):
            with pytest.raises(WorkspaceError, match="several links"):
                read(rel)
    (tree.root / "plain.txt").write_text("fine")
    assert tree.read_text("plain.txt")["text"] == "fine"


async def test_a_hard_linked_file_stays_out_of_every_chats_reach(make_app):
    app = make_app()
    shared_home(app)
    shared = app.home.root / "workspace" / "shared"
    os.link(app.home.root / "workspace" / "mine.txt", shared / "leak.md")
    os.link(shared / "inbox" / str(FRIEND) / "theirs.txt", shared / "inbox" / str(GROUP) / "stolen.txt")
    app.config = app.config.model_copy(update={"workspace": WorkspaceConfig(share_with_chats=True)})
    group = ctx(app, "group", GROUP, FRIEND)
    for path in ("leak.md", f"inbox/{GROUP}/stolen.txt"):
        reply = await SHARED_FILES_TOOL.handler({"action": "read", "path": path}, group)
        assert reply.startswith("error:") and "several links" in reply and "owner only" not in reply and "from a friend" not in reply
    owner = await OWNER_FILES_TOOL.handler({"action": "read", "path": "shared/leak.md"}, ctx(app))
    assert owner.startswith("error:") and "copy it to read it" in owner


async def test_the_files_page_will_not_serve_a_hard_linked_file(make_app, browser):
    app = make_app()
    folder = ensure_workspace(app.home.root)
    (folder / "a.txt").write_text("x")
    os.link(folder / "a.txt", folder / "b.txt")
    async with browser(app) as client:
        for route in ("raw", "preview"):
            response = await client.get(f"/api/files/{route}", params={"path": "b.txt"})
            assert response.status_code == 422 and "several links" in response.json()["detail"]


# ---- 3 and 5. the backup walk ---------------------------------------------------------------------------------


def make_home(tmp_path):
    from dom.store import Store

    home = tmp_path / "home"
    (home / "memory").mkdir(parents=True)
    (home / "memory" / "MEMORY.md").write_text("m")
    (home / "config.toml").write_text('timezone = "UTC"\n')
    Store(home, owner_id=OWNER).close()
    return home, ensure_workspace(home)


def archived(home, tmp_path):
    archive = backup.backup(home, destination=tmp_path / "backups")
    with tarfile.open(archive) as tar:
        return {member.name: member for member in tar.getmembers()}, archive


def test_backup_archives_only_single_link_regular_files_of_the_workspace(tmp_path):
    home, folder = make_home(tmp_path)
    (folder / "keep.txt").write_text("kept")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.txt").write_text("deep")
    (folder / "link").symlink_to("/etc/hosts")
    os.mkfifo(folder / "pipe")
    os.link(folder / "keep.txt", folder / "twin.txt")  # two names: neither is archived
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (folder / "dirlink").symlink_to(outside)
    members, _ = archived(home, tmp_path)
    assert "workspace/sub/deep.txt" in members and "workspace/sub" in members
    for skipped in ("workspace/keep.txt", "workspace/twin.txt", "workspace/link", "workspace/pipe", "workspace/dirlink"):
        assert skipped not in members
    assert not any("secret" in name for name in members)


def test_backup_filters_apply_to_the_home_only_never_inside_the_workspace(tmp_path):
    home, folder = make_home(tmp_path)
    for name in ("state.db-notes.txt", "report.bak.txt", ".claim", "state.db"):
        (folder / name).write_text("mine")
    (home / "state.db.bak").write_text("skipped at the top level")
    members, _ = archived(home, tmp_path)
    for name in ("state.db-notes.txt", "report.bak.txt", ".claim", "state.db"):
        assert f"workspace/{name}" in members
    assert "state.db.bak" not in members


def test_a_file_swapped_for_a_link_after_the_listing_is_not_followed(tmp_path, monkeypatch):
    home, folder = make_home(tmp_path)
    (folder / "swap.txt").write_text("harmless")
    (tmp_path / "outside.txt").write_text("OUTSIDE-MARKER-9f3")
    real = os.scandir

    def scandir(path):
        entries = list(real(path))  # the walk has now seen the names
        victim = folder / "swap.txt"
        if isinstance(path, int) and not victim.is_symlink():  # the walker's own listing, by descriptor
            victim.unlink()
            victim.symlink_to(tmp_path / "outside.txt")  # ... and the desktop swaps one
        return iter_entries(entries)

    class iter_entries(list):
        def __enter__(self):
            return iter(self)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(os, "scandir", scandir)
    members, archive = archived(home, tmp_path)
    monkeypatch.undo()
    assert "workspace/swap.txt" not in members
    with tarfile.open(archive) as tar:
        leaked = [m.name for m in tar.getmembers() if m.isfile() and b"OUTSIDE-MARKER-9f3" in tar.extractfile(m).read()]
        assert leaked == []


def test_backup_stops_at_the_cap_and_skips_oversize_files(tmp_path, monkeypatch):
    home, folder = make_home(tmp_path)
    monkeypatch.setattr(workspace, "MAX_TOTAL_BYTES", 100)
    monkeypatch.setattr(workspace, "MAX_FILE_BYTES", 60)
    for age, name in enumerate(("a.txt", "b.txt", "c.txt")):
        (folder / name).write_bytes(name[:1].encode() * 50)
        os.utime(folder / name, (1_000_000 + age, 1_000_000 + age))  # c is the newest
    (folder / "huge.bin").write_bytes(b"h" * 70)  # over the per-file limit
    members, _ = archived(home, tmp_path)
    files = {name: member for name, member in members.items() if name.startswith("workspace/") and member.isfile()}
    assert sorted(files) == ["workspace/b.txt", "workspace/c.txt"]  # the newest two; the oldest is what does not fit
    assert sum(member.size for member in files.values()) == 100


def test_the_backup_cap_keeps_the_newest_files_whatever_their_names_and_logs_what_it_skipped(tmp_path, monkeypatch, caplog):
    home, folder = make_home(tmp_path)
    monkeypatch.setattr(workspace, "MAX_TOTAL_BYTES", 60)
    (folder / "sub").mkdir()
    (folder / "a-old.txt").write_bytes(b"o" * 50)
    (folder / "sub" / "m-mid.txt").write_bytes(b"m" * 50)
    (folder / "z-new.txt").write_bytes(b"n" * 50)
    for name, when in (("a-old.txt", 1_000_000), ("sub/m-mid.txt", 2_000_000), ("z-new.txt", 3_000_000)):
        os.utime(folder / name, (when, when))
    with caplog.at_level("WARNING", logger="dom.backup"):
        members, _ = archived(home, tmp_path)
    files = sorted(name for name, member in members.items() if name.startswith("workspace/") and member.isfile())
    assert files == ["workspace/z-new.txt"]
    skipped = " ".join(record.getMessage() for record in caplog.records)
    assert "2 files over the cap" in skipped and "a-old.txt" in skipped and "m-mid.txt" in skipped


def test_backup_leaves_out_only_the_staging_files_the_workspace_makes(tmp_path):
    home, folder = make_home(tmp_path)
    (folder / ".incoming-user").mkdir()
    (folder / ".incoming-user" / "valuable.txt").write_text("keep me")
    (folder / ".incoming-notes").write_text("made by the desktop")
    (folder / ".incoming-0123456789ab").write_text("half-written")  # the exact name `_stage` makes
    (folder / "sub").mkdir()
    (folder / "sub" / ".incoming-fedcba987654").write_text("half-written")
    (folder / ".incoming-aabbccddeeff").mkdir()  # a folder is never a staging file
    (folder / ".incoming-aabbccddeeff" / "inside.txt").write_text("keep me too")
    members, _ = archived(home, tmp_path)
    assert "workspace/.incoming-user/valuable.txt" in members and "workspace/.incoming-notes" in members
    assert "workspace/.incoming-aabbccddeeff/inside.txt" in members
    assert "workspace/.incoming-0123456789ab" not in members and "workspace/sub/.incoming-fedcba987654" not in members


def test_the_name_the_workspace_stages_with_is_the_name_the_backup_leaves_out(tmp_path):
    tree = Tree(ensure_workspace(tmp_path))
    with tree._directory(()) as parent:
        name = tree._stage(parent, b"x")
    assert workspace.is_staging(name)
    assert not workspace.is_staging(".incoming-notes") and not workspace.is_staging("incoming-0123456789ab")


# ---- 2. PDF extraction runs in a killable, limited process ---------------------------------------------------------

import asyncio
import time
import zlib

from test_workspace import make_pdf


def spy_on_spawns(monkeypatch):
    spawned = []
    real = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await real(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return spawned


def sleeping_worker(tmp_path, monkeypatch, seconds=0.4):
    script = tmp_path / "sleeper.py"
    script.write_text("import sys, time\nsys.stdin.buffer.read()\ntime.sleep(60)\n")
    monkeypatch.setattr(workspace, "PDF_WORKER", script)
    monkeypatch.setattr(workspace, "PDF_SECONDS", seconds)


async def test_a_pdf_is_read_in_a_separate_process(tree, monkeypatch):
    spawned = spy_on_spawns(monkeypatch)
    (tree.root / "report.pdf").write_bytes(make_pdf())
    result = await workspace.read_document(tree, "report.pdf")
    assert "Quarterly figures" in result["text"] and result["next_offset"] is None
    assert len(spawned) == 1 and spawned[0].pid != os.getpid()


async def test_pdf_text_comes_back_bounded_even_from_a_long_document(tree, monkeypatch):
    monkeypatch.setattr(workspace, "READ_CHARS", 5)
    (tree.root / "long.pdf").write_bytes(make_pdf("Quarterly figures and more"))
    first = await workspace.read_document(tree, "long.pdf")
    assert first["text"] == "Quart" and first["next_offset"] == 5
    assert (await workspace.read_document(tree, "long.pdf", 5))["text"] == "erly "


async def test_a_pdf_that_hangs_is_killed_at_the_wall_clock_limit(tree, tmp_path, monkeypatch):
    sleeping_worker(tmp_path, monkeypatch)
    spawned = spy_on_spawns(monkeypatch)
    (tree.root / "hang.pdf").write_bytes(make_pdf())
    started = time.monotonic()
    with pytest.raises(WorkspaceError, match="too long"):
        await workspace.read_document(tree, "hang.pdf")
    assert time.monotonic() - started < 5
    assert spawned[0].returncode is not None  # reaped: nothing is left running


async def test_cancelling_a_pdf_read_kills_the_process(tree, tmp_path, monkeypatch):
    sleeping_worker(tmp_path, monkeypatch, seconds=30)
    spawned = spy_on_spawns(monkeypatch)
    (tree.root / "hang.pdf").write_bytes(make_pdf())
    task = asyncio.create_task(workspace.read_document(tree, "hang.pdf"))
    async with asyncio.timeout(5):
        while not spawned:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned[0].returncode is not None


async def test_a_big_pdf_is_refused_before_anything_parses_it(tree, monkeypatch):
    spawned = spy_on_spawns(monkeypatch)
    monkeypatch.setattr(workspace, "MAX_PDF_BYTES", 100)
    (tree.root / "big.pdf").write_bytes(make_pdf() + b"\n%" + b"x" * 200)
    with pytest.raises(WorkspaceError, match="larger than"):
        await workspace.read_document(tree, "big.pdf")
    assert spawned == []


def pathological(kind):
    """A PDF whose page is built to cost a parser: nesting a hundred thousand deep, or a stream that inflates to
    hundreds of megabytes from a few hundred kilobytes."""
    if kind == "nested":
        stream = b"[" * 100_000 + b"]" * 100_000
        compressed = stream
        filter_ = b""
    else:
        packer = zlib.compressobj()
        chunks = [packer.compress(b"BT " + b" " * 1_000_000) for _ in range(1)]
        chunks += [packer.compress(b" " * 1_000_000) for _ in range(300)]
        chunks.append(packer.flush())
        compressed = b"".join(chunks)
        filter_ = b"/Filter/FlateDecode"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 100]/Contents 4 0 R>>",
        b"<</Length %d%s>>\nstream\n" % (len(compressed), filter_) + compressed + b"\nendstream",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % at for at in offsets)
    return out + b"trailer\n<</Root 1 0 R/Size 5>>\nstartxref\n%d\n%%%%EOF\n" % table


@pytest.mark.parametrize("kind", ["nested", "inflating"])
async def test_a_pathological_pdf_ends_quickly_with_an_error_or_text_and_leaves_nothing_running(tree, monkeypatch, kind):
    spawned = spy_on_spawns(monkeypatch)
    monkeypatch.setattr(workspace, "PDF_SECONDS", 20)
    (tree.root / "bad.pdf").write_bytes(pathological(kind))
    started = time.monotonic()
    try:
        await workspace.read_document(tree, "bad.pdf")
    except WorkspaceError:
        pass  # a refusal is the expected end; so is an empty result
    assert time.monotonic() - started < 25
    assert spawned and all(process.returncode is not None for process in spawned)


# ---- 4. the size cap the bind mount skips ----------------------------------------------------------------------


@pytest.fixture
def small_cap(monkeypatch):
    monkeypatch.setattr(workspace, "MAX_TOTAL_BYTES", 10)
    monkeypatch.setattr(workspace, "USAGE_TTL", 0)  # measure each time
    workspace._measured.clear()


def fill(app, size=20):
    (ensure_workspace(app.home.root) / "fill.bin").write_bytes(b"x" * size)  # as the desktop's container can


async def test_going_over_the_cap_tells_the_owner_once_with_a_link_and_again_only_after_room_came_back(make_app, small_cap):
    app = make_app()
    app.config = app.config.model_copy(update={"web": app.config.web.model_copy(update={"public_url": "https://dom.example.test"})})
    assert await workspace.check_cap(app) is False and app.bot.of("send", OWNER) == []
    fill(app)
    assert await workspace.check_cap(app) is True
    assert await workspace.check_cap(app) is True  # still over: no second notice
    notices = app.bot.of("send", OWNER)
    assert len(notices) == 1 and "workspace is full" in notices[0][2]
    assert notices[0][3]["disable_notification"] is True and "/app/work/files" in str(notices[0][3]["reply_markup"])
    (app.home.root / "workspace" / "fill.bin").unlink()
    assert await workspace.check_cap(app) is False
    fill(app)
    await workspace.check_cap(app)
    assert len(app.bot.of("send", OWNER)) == 2


async def test_a_running_desktop_task_is_stopped_when_the_workspace_overflows(make_app, small_cap):
    from dom.home import Home
    from test_desktop_worker import call, task_row, wait_task, worker_for

    app = make_app()
    app.home = Home(app.home.root / "home")
    worker = await worker_for(
        app, call("upload_file", selector="#file", path="/tmp/a.txt"), call("finish", result="done"),
        {"content": '{"bubbles":[{"text":"ok"}]}'},
    )
    worker.sandbox.workspace_mounted = True
    fill(app)  # the container filled it while the task ran
    task_id = await worker.enqueue(OWNER, "Do something")
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert row["status"] == "failed" and "workspace is full" in row["error"]


@pytest.fixture
def cached_cap(monkeypatch):
    """A small cap with the real five-second measurement cache, so a stale measurement can show."""
    monkeypatch.setattr(workspace, "MAX_TOTAL_BYTES", 10)
    workspace._measured.clear()


async def test_a_write_just_before_the_task_finishes_is_caught_by_the_final_check(make_app, cached_cap, monkeypatch):
    import dom.desktop.worker as worker_module
    from dom.home import Home
    from test_desktop_worker import call, task_row, wait_task, worker_for

    app = make_app()
    app.home = Home(app.home.root / "home")
    worker = await worker_for(app, call("finish", result="done"), {"content": '{"bubbles":[{"text":"ok"}]}'})
    worker.sandbox.workspace_mounted = True
    assert await workspace.check_cap(app) is False  # measured under the cap a moment ago: the cache is warm
    real = worker_module.download_results

    async def late_download(client, files):
        fill(app)  # the container's last download lands as the task ends, after the last step's check
        return await real(client, files)

    monkeypatch.setattr(worker_module, "download_results", late_download)
    task_id = await worker.enqueue(OWNER, "Do something")
    await wait_task(worker)
    assert (await task_row(app, task_id))["status"] == "done"  # the check is soft: the task's result stands
    assert sum("workspace is full" in str(sent[2]) for sent in app.bot.of("send", OWNER)) == 1


async def test_a_new_desktop_task_is_refused_from_a_fresh_measurement_not_a_cached_one(make_app, cached_cap):
    from test_desktop_fleet import fleet_for

    app = make_app()
    fleet = fleet_for(app)
    assert await workspace.check_cap(app) is False  # the cache now says there is room
    fill(app)
    with pytest.raises(ValueError, match="workspace is full"):
        await fleet.enqueue(OWNER, "one")


async def test_the_files_page_reports_fullness_from_a_fresh_measurement(make_app, cached_cap, browser):
    app = make_app()
    async with browser(app) as client:
        assert (await client.get("/api/files/list")).json()["full"] is False  # warms the cache
        fill(app)
        assert (await client.get("/api/files/list")).json()["full"] is True


async def test_a_task_without_the_mount_ignores_the_workspace_size(make_app, small_cap):
    from dom.home import Home
    from test_desktop_worker import call, task_row, wait_task, worker_for

    app = make_app()
    app.home = Home(app.home.root / "home")
    worker = await worker_for(app, call("finish", result="done"), {"content": '{"bubbles":[{"text":"ok"}]}'})
    worker.sandbox.workspace_mounted = False
    fill(app)
    task_id = await worker.enqueue(OWNER, "Do something")
    await wait_task(worker)
    assert (await task_row(app, task_id))["status"] == "done"
    assert app.bot.of("send", OWNER) == [] or "workspace is full" not in app.bot.of("send", OWNER)[0][2]


async def test_no_new_desktop_work_starts_while_the_workspace_is_over_its_cap(make_app, small_cap):
    from test_desktop_fleet import fleet_for, settle

    app = make_app()
    fleet = fleet_for(app)
    fill(app)
    with pytest.raises(ValueError, match="workspace is full"):
        await fleet.enqueue(OWNER, "one")
    assert await app.store.desktop_tasks(OWNER) == []
    (app.home.root / "workspace" / "fill.bin").unlink()
    await fleet.enqueue(OWNER, "two")
    await settle(app, fleet)


async def test_the_file_tools_and_the_page_refuse_writes_over_the_cap_but_still_delete(make_app, small_cap, browser):
    app = make_app()
    fill(app)
    assert (await OWNER_FILES_TOOL.handler({"action": "write", "path": "n.txt", "content": "hi"}, ctx(app))).startswith("error:")
    async with browser(app) as client:
        body = {"folder": "", "name": "a.txt", "data": "aGk="}
        sent = await client.post("/api/files/upload", json=body, headers={"Origin": str(client.base_url).rstrip("/")})
        assert sent.status_code == 422 and "full" in sent.json()["detail"]
    assert (await OWNER_FILES_TOOL.handler({"action": "delete", "path": "fill.bin"}, ctx(app))).startswith("ok")
    assert (await OWNER_FILES_TOOL.handler({"action": "write", "path": "n.txt", "content": "hi"}, ctx(app))).startswith("ok")


def test_the_measuring_walk_is_bounded_and_counts_a_flood_of_files_as_over_the_cap(tree, monkeypatch):
    monkeypatch.setattr(workspace, "MAX_WALK", 3)
    for number in range(5):
        (tree.root / f"{number}.txt").write_text("")
    assert tree.used_bytes() > workspace.MAX_TOTAL_BYTES


def test_a_measurement_is_reused_for_a_few_seconds(tmp_path, monkeypatch):
    ensure_workspace(tmp_path)
    workspace._measured.clear()
    (tmp_path / "workspace" / "a").write_bytes(b"123")
    assert workspace.measure(tmp_path) == 3
    (tmp_path / "workspace" / "b").write_bytes(b"4567")
    assert workspace.measure(tmp_path) == 3 and workspace.measure(tmp_path, fresh=True) == 7


def test_the_doctor_reports_the_workspace_against_its_cap(tmp_path, monkeypatch):
    from dom import doctor

    assert doctor._workspace(tmp_path).status == "ok"
    folder = ensure_workspace(tmp_path)
    (folder / "a.bin").write_bytes(b"x" * 50)
    monkeypatch.setattr(workspace, "MAX_TOTAL_BYTES", 100)
    assert doctor._workspace(tmp_path).status == "ok" and "0 MB of 0 MB" in doctor._workspace(tmp_path).detail
    (folder / "b.bin").write_bytes(b"x" * 40)
    assert doctor._workspace(tmp_path).status == "warn"
    (folder / "c.bin").write_bytes(b"x" * 40)
    over = doctor._workspace(tmp_path)
    assert over.status == "fail" and "cap" in over.detail


# ---- 7. a mount the desktop's user can't use ------------------------------------------------------------------------


async def start_sandbox(tmp_path, docker, name="default"):
    import httpx

    from dom.config import Config, DesktopConfig
    from dom.desktop.sandbox import LocalSandbox
    from test_desktop_sandbox import PROBES

    config = Config(home=tmp_path / "home", desktop_mode="local", desktop=DesktopConfig(lan_probes=PROBES))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"success": True, "data": None})))
    sandbox = LocalSandbox(config, run=docker, http=http, name=name)
    await sandbox.ensure_running()
    return sandbox, config


def run_calls(docker):
    return [call for call in docker.started()]


async def test_the_workspace_is_mounted_when_the_containers_user_owns_the_folder(tmp_path):
    from test_desktop_sandbox import FakeDocker

    docker = FakeDocker()
    sandbox, config = await start_sandbox(tmp_path, docker)
    started = run_calls(docker)[0]
    assert f"{(tmp_path / 'home' / 'workspace').resolve()}:/workspace" in started and sandbox.workspace_mounted
    assert workspace.mount_problem(config.home) is None
    assert ((tmp_path / "home" / "workspace").stat().st_mode & 0o777) == 0o700  # still owner-only
    await sandbox.close()


@pytest.mark.parametrize(("probe", "expected"), [
    ((0, b"1000\n"), "its user is uid 1000, the folder belongs to uid"),  # another user: no mount
    ((0, b"0\n"), "its user is uid 0"),  # root writes, but its files would not be Dom's to read or delete
    ((1, f"{os.getuid()}\n".encode()), "can't write to the workspace"),  # the same user, but the write failed
    ((125, b""), "its user is uid unknown"),  # the check itself could not run
])
async def test_an_unusable_mount_is_left_out_and_explained(tmp_path, caplog, probe, expected):
    from test_desktop_sandbox import FakeDocker

    docker = FakeDocker()
    docker.workspace_probe = probe
    sandbox, config = await start_sandbox(tmp_path, docker)
    started = run_calls(docker)[0]
    assert not any(":/workspace" in part for part in started) and not sandbox.workspace_mounted
    assert sandbox.status() == "ready"  # the desktop still works, without the workspace
    problem = workspace.mount_problem(config.home)
    assert problem and expected in problem and "can't write to the workspace" in problem
    assert "can't write to the workspace" in caplog.text and "chown" in caplog.text
    await sandbox.close()


async def test_a_later_start_that_can_mount_clears_the_problem(tmp_path):
    from test_desktop_sandbox import FakeDocker

    docker = FakeDocker()
    docker.workspace_probe = (0, b"1000\n")
    sandbox, config = await start_sandbox(tmp_path, docker)
    assert workspace.mount_problem(config.home)
    await sandbox.stop()
    docker.workspace_probe = None
    await sandbox.ensure_running()
    assert workspace.mount_problem(config.home) is None and sandbox.workspace_mounted
    await sandbox.close()


async def test_the_self_directed_container_is_neither_probed_nor_mounted(tmp_path):
    from dom.desktop.sandbox import SELF_WORK
    from test_desktop_sandbox import FakeDocker

    docker = FakeDocker()
    sandbox, config = await start_sandbox(tmp_path, docker, name=SELF_WORK)
    assert not [call for call in docker.calls if "--entrypoint" in call]
    assert not any(":/workspace" in part for part in run_calls(docker)[0]) and not sandbox.workspace_mounted
    await sandbox.close()


def test_the_doctor_explains_an_unusable_mount(tmp_path):
    from dom import doctor

    ensure_workspace(tmp_path)
    workspace.set_mount_problem(tmp_path, "the desktop can't write to the workspace: its user is uid 1000, the folder belongs to uid 501")
    check = doctor._workspace(tmp_path)
    assert check.status == "warn" and "uid 1000" in check.detail and "chown" in check.detail


async def test_the_files_page_shows_why_the_desktop_has_no_workspace(make_app, browser):
    app = make_app()
    ensure_workspace(app.home.root)
    async with browser(app) as client:
        assert (await client.get("/api/files/list")).json()["mount_problem"] is None
        workspace.set_mount_problem(app.home.root, "the desktop can't write to the workspace: uid 1000 vs 501")
        assert "uid 1000 vs 501" in (await client.get("/api/files/list")).json()["mount_problem"]
        assert (await client.get("/api/files/list", params={"area": "home"})).json()["mount_problem"] is None


# ---- 6. a document that is slow to arrive ---------------------------------------------------------------------------


def slow_document(app, monkeypatch, gate):
    async def download(file_id, *, destination):
        await gate.wait()
        destination.write(b"the long awaited text")
        return destination

    monkeypatch.setattr(app.bot, "download", download, raising=False)


def sent_texts(app, chat=OWNER):
    return [call[2] for call in app.bot.of("send", chat)]


async def test_a_turn_with_a_document_waits_for_it_longer_than_other_media(make_app, monkeypatch):
    from dom import dispatch
    from test_workspace import document
    from test_usage import online
    from dom.telegram.handlers import handle_message
    from tghelpers import drain_dispatch, tg

    app = make_app()
    monkeypatch.setattr(dispatch, "MEDIA_WAIT_SECONDS", 0.05)  # a photo would be given up on by now
    gate = asyncio.Event()
    slow_document(app, monkeypatch, gate)
    asyncio.get_running_loop().call_later(0.4, gate.set)
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption="read this", document=document("plan.txt")))
    await drain_dispatch(app.dispatch)
    prompt = json_prompt(app)
    assert "inbox/plan.txt" in prompt and "still arriving" not in prompt
    assert len(app.script.requests) == 1 and (app.home.root / "workspace" / "inbox" / "plan.txt").exists()


def json_prompt(app):
    import json

    return json.dumps(app.script.requests)


def read_the_file_on_the_second_turn(app, monkeypatch, path="inbox/plan.txt"):
    """The model's second request asks for the file with the `files` tool, as the real model would; the rest is the script."""
    import json

    import httpx

    from tghelpers import ModelScript

    real = ModelScript.__call__

    def respond(script, request):
        body = json.loads(request.content)
        if script is app.script and len(script.requests) == 1 and "tools" in body:
            script.requests.append(body)
            call = {"id": "c1", "type": "function", "function": {"name": "files", "arguments": json.dumps({"action": "read", "path": path})}}
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})
        return real(script, request)

    monkeypatch.setattr(ModelScript, "__call__", respond)


async def test_a_document_still_arriving_is_said_so_and_when_it_lands_dom_takes_a_real_turn_once(make_app, monkeypatch):
    from dom import dispatch
    from test_workspace import document, stored_media
    from test_usage import online
    from dom.telegram.handlers import handle_message
    from tghelpers import drain_dispatch, tg

    app = make_app()
    monkeypatch.setattr(dispatch, "DOCUMENT_WAIT_SECONDS", 0.05)
    gate = asyncio.Event()
    slow_document(app, monkeypatch, gate)
    read_the_file_on_the_second_turn(app, monkeypatch)
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption="answer the questions in this", document=document("plan.txt")))
    for _ in range(200):  # the turn goes ahead without the file, then waits for its slot
        if app.script.requests:
            break
        await asyncio.sleep(0.01)
    assert "the file is still arriving" in json_prompt(app)
    assert len(app.script.requests) == 1
    gate.set()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 3  # the first turn, then one more turn (the file tool call and its answer), no more
    late = app.script.requests[1]
    text = str(late["messages"])
    assert "answer the questions in this" in text  # the original request, not an excerpt of the file
    assert "was still arriving when you answered it" in text and "inbox/plan.txt" in text
    assert "the long awaited text" not in text  # the file is read through the tool, not pasted into the prompt
    assert "files" in [tool["function"]["name"] for tool in late["tools"]]
    assert "the long awaited text" in str(app.script.requests[2]["messages"])  # the tool read it
    assert (await stored_media(app, OWNER, 1))["saved"] == "inbox/plan.txt"
    events = [row.text for row in await app.store.get_recent_chat_window(OWNER) if row.role == "event"]
    assert any("plan.txt finished arriving" in text for text in events)
    assert len(sent_texts(app)) == 2  # the reply, then the answer from the second turn
    assert app.dispatch._landing_missed == set()


async def test_a_turn_that_already_had_the_file_means_no_late_turn(make_app, monkeypatch):
    from dom import dispatch
    from test_workspace import document
    from test_usage import online
    from dom.telegram.handlers import handle_message
    from tghelpers import drain_dispatch, tg

    app = make_app()
    monkeypatch.setattr(dispatch, "DOCUMENT_WAIT_SECONDS", 0.05)
    gate = asyncio.Event()
    slow_document(app, monkeypatch, gate)
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption="read this", document=document("plan.txt")))
    while len(sent_texts(app)) < 1:  # the first turn answered without the file
        await asyncio.sleep(0.01)
    busy = app.dispatch._turn_slot(OWNER)  # a turn is running in the chat
    await busy.__aenter__()
    try:
        await handle_message(app, tg(OWNER, OWNER, text="so?", msg_id=2))  # waits for the slot first
        await asyncio.sleep(0.3)
        gate.set()  # the document lands; the late turn queues behind the other
        while app.dispatch._landing_pending:
            await asyncio.sleep(0.01)
    finally:
        await busy.__aexit__(None, None, None)
    await drain_dispatch(app.dispatch)
    turns = [str(request["messages"]) for request in app.script.requests]
    assert len(turns) == 2  # the first turn and the "so?" turn, which found the file saved: nothing more
    assert "inbox/plan.txt" in turns[1] and "still arriving" not in turns[1] and "was still arriving when you" not in turns[1]
    assert app.dispatch._landing_missed == set()


async def test_the_late_turn_keeps_the_topic_and_the_chats_scope(make_app, monkeypatch):
    from dom import dispatch
    from test_workspace import document
    from dom.telegram.handlers import handle_message
    from tghelpers import drain_dispatch, tg
    from test_usage import online

    app = make_app()
    app.config = app.config.model_copy(update={"workspace": WorkspaceConfig(share_with_chats=True)})
    monkeypatch.setattr(dispatch, "DOCUMENT_WAIT_SECONDS", 0.05)
    gate = asyncio.Event()
    slow_document(app, monkeypatch, gate)
    read_the_file_on_the_second_turn(app, monkeypatch, path=f"inbox/{GROUP}/plan.txt")
    await online(app, GROUP)
    message = tg(GROUP, FRIEND, text=None, caption="@dombot answer the questions in this", document=document("plan.txt"),
                 chat_type="supergroup", is_topic_message=True, message_thread_id=7)
    await handle_message(app, message)
    for _ in range(200):
        if app.script.requests:
            break
        await asyncio.sleep(0.01)
    gate.set()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 3
    late = app.script.requests[1]
    assert "answer the questions in this" in str(late["messages"])
    shared = next(tool for tool in late["tools"] if tool["function"]["name"] == "files")["function"]
    assert "write" not in str(shared["parameters"])  # a group gets the read-only files tool, never the owner's
    sends = app.bot.of("send", GROUP)
    assert len(sends) == 2 and all(call[3].get("message_thread_id") == 7 for call in sends)
    assert "the long awaited text" in str(app.script.requests[2]["messages"])


async def test_a_document_that_lands_before_the_turn_needs_no_follow_up(make_app, monkeypatch):
    from test_workspace import document, serve_document
    from test_usage import online
    from dom.telegram.handlers import handle_message
    from tghelpers import drain_dispatch, tg

    app = make_app()
    serve_document(app, monkeypatch)
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, caption="read this", document=document("plan.txt")))
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1 and "still arriving" not in json_prompt(app)
    assert len(sent_texts(app)) == 1
    assert app.dispatch._landing_missed == set() and app.dispatch._landing_pending == {}


async def test_a_document_that_never_lands_gets_no_follow_up(make_app, monkeypatch):
    from dom import dispatch
    from test_workspace import document
    from test_usage import online
    from dom.telegram.handlers import handle_message
    from tghelpers import drain_dispatch, tg

    app = make_app()
    monkeypatch.setattr(dispatch, "DOCUMENT_WAIT_SECONDS", 0.05)
    gate = asyncio.Event()

    async def broken(file_id, *, destination):
        await gate.wait()
        raise OSError("telegram is down")

    monkeypatch.setattr(app.bot, "download", broken, raising=False)
    await online(app, OWNER)
    await handle_message(app, tg(OWNER, OWNER, text=None, document=document("plan.txt")))
    for _ in range(200):
        if app.script.requests:
            break
        await asyncio.sleep(0.01)
    gate.set()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1  # nothing to read, so nothing to follow up on
    assert app.dispatch._landing_missed == set()
