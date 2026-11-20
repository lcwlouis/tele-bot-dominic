"""More than one desktop container: hosts, limits, the memory floor, and per-container state."""

import asyncio
import json

import httpx
import pytest

from dom.config import Config, DesktopConfig, DesktopHost
from dom.desktop.client import SandboxClient, SandboxError
from dom.desktop.sandbox import (
    CONTAINER_MEMORY_MB, Host, LocalSandbox, RemoteSandbox, container_name, desktop_profile_path,
)
from test_desktop_sandbox import BRIDGE, GATEWAY, NETWORK, PROBES, Process


class Docker:
    """A docker CLI with many containers: tracks which are running, and records every call."""

    def __init__(self):
        self.calls = []
        self.running = set()

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        command = args[1]
        if command == "image":
            return Process(0, b"")
        if command == "network":
            return Process(0, json.dumps(NETWORK).encode())
        if command == "ps":
            return Process(0, "\n".join(sorted(self.running)).encode())
        if command == "container":
            name = args[-1]
            return Process(0, b"true") if name in self.running else Process(1, b"")
        if command == "run" and "--rm" in args:
            return Process(0, b"")
        if command == "run":
            self.running.add(args[args.index("--name") + 1])
        elif command in ("stop", "rm"):
            self.running.discard(args[-1])
        return Process(0, b"")

    def started(self):
        return [call for call in self.calls if call[1] == "run" and "--rm" not in call]

    def probes(self):
        return [call for call in self.calls if call[1] == "run" and "--rm" in call]


def config(tmp_path, **host):
    host = {"name": "pi", "mode": "local", "max_containers": 2} | host
    return Config(
        home=tmp_path / "home", desktop_mode="local",
        desktop=DesktopConfig(lan_probes=PROBES, hosts=[DesktopHost(**host)]),
    )


def sandbox(tmp_path, name, host, docker, http, clock=None, **host_config):
    options = {"clock": clock} if clock else {}
    return LocalSandbox(config(tmp_path, **host_config), run=docker, http=http, name=name, host=host, **options)


@pytest.fixture
async def http():
    def reply(request):
        return httpx.Response(200, json={"success": True, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        yield client


def test_container_names_keep_the_default_and_prefix_the_rest(tmp_path):
    assert container_name("default") == "dom-desktop"
    assert container_name("shop") == "dom-desktop-shop"
    cfg = config(tmp_path)
    home = tmp_path / "home"
    assert desktop_profile_path(cfg) == (tmp_path / "desktop-profile")
    assert desktop_profile_path(cfg, "shop") == tmp_path / "desktop-profile-shop"
    assert not desktop_profile_path(cfg, "shop").is_relative_to(home)


async def test_two_containers_get_their_own_name_port_profile_and_firewall_probe(tmp_path, http):
    docker = Docker()
    host = Host(DesktopHost(name="pi", mode="local", max_containers=2), free_memory=lambda: 8000)
    first = sandbox(tmp_path, "default", host, docker, http)
    second = sandbox(tmp_path, "shop", host, docker, http)
    await first.ensure_running()
    await second.ensure_running()
    run_one, run_two = docker.started()
    assert run_one[run_one.index("--name") + 1] == "dom-desktop"
    assert run_two[run_two.index("--name") + 1] == "dom-desktop-shop"
    assert "127.0.0.1:18080:8080" in run_one and "127.0.0.1:18081:8080" in run_two
    assert f"{tmp_path}/desktop-profile:/home/gem/.config/browser" in run_one
    assert f"{tmp_path}/desktop-profile-shop:/home/gem/.config/browser" in run_two
    assert len(docker.probes()) == 2  # the LAN block is proven for every container, not once
    assert second.client.base_url.endswith(":18081")
    await first.stop()
    third = sandbox(tmp_path, "docs", host, docker, http)
    await third.ensure_running()
    assert "127.0.0.1:18080:8080" in docker.started()[-1]  # a freed port is reused
    for each in (first, second, third):
        await each.close()


async def test_a_host_refuses_a_container_past_its_limit_without_poisoning_the_sandbox(tmp_path, http):
    docker = Docker()
    host = Host(DesktopHost(name="pi", mode="local"), free_memory=lambda: 8000)  # one container
    first = sandbox(tmp_path, "default", host, docker, http, max_containers=1)
    second = sandbox(tmp_path, "shop", host, docker, http, max_containers=1)
    await first.ensure_running()
    with pytest.raises(SandboxError, match=r"host pi already runs its limit of 1 .*default"):
        await second.ensure_running()
    assert second.status() == "stopped" and "limit of 1" in second.error  # a refusal is not a failure state
    assert len(docker.started()) == 1
    await first.stop()
    await second.ensure_running()  # the slot is free now
    assert second.status() == "ready" and second.error is None
    await first.close()
    await second.close()


async def test_a_host_refuses_to_breach_its_memory_floor(tmp_path, http):
    docker = Docker()
    free = [CONTAINER_MEMORY_MB + 600 - 1]
    host = Host(DesktopHost(name="pi", mode="local", min_free_memory_mb=600), free_memory=lambda: free[0])
    box = sandbox(tmp_path, "default", host, docker, http, min_free_memory_mb=600)
    with pytest.raises(SandboxError, match=r"not enough free memory on host pi: 2199 MB free, 2200 MB needed"):
        await box.ensure_running()
    assert docker.started() == [] and box.status() == "stopped"
    free[0] += 1
    await box.ensure_running()
    assert box.status() == "ready"
    await box.close()


async def test_an_unreadable_memory_figure_refuses_when_a_floor_is_set(tmp_path, http):
    host = Host(DesktopHost(name="pi", mode="local"), free_memory=lambda: None)
    box = sandbox(tmp_path, "default", host, Docker(), http)
    with pytest.raises(SandboxError, match="could not read free memory on host pi"):
        await box.ensure_running()


async def test_a_zero_floor_does_not_read_memory(tmp_path, http):
    def unreadable():
        raise AssertionError("memory was read")

    host = Host(DesktopHost(name="pi", mode="local", min_free_memory_mb=0), free_memory=unreadable)
    box = sandbox(tmp_path, "default", host, Docker(), http, min_free_memory_mb=0)
    await box.ensure_running()
    await box.close()


async def test_concurrent_starts_cannot_both_pass_the_limit(tmp_path, http):
    docker = Docker()
    host = Host(DesktopHost(name="pi", mode="local"), free_memory=lambda: 8000)
    boxes = [sandbox(tmp_path, name, host, docker, http, max_containers=1) for name in ("default", "a", "b")]
    results = await asyncio.gather(*(box.ensure_running() for box in boxes), return_exceptions=True)
    assert sum(isinstance(result, SandboxClient) for result in results) == 1
    assert len(docker.started()) == 1
    for box in boxes:
        await box.close()


async def test_each_container_stops_on_its_own_idle_timeout(tmp_path, http):
    docker = Docker()
    now = [0]
    host = Host(DesktopHost(name="pi", mode="local", max_containers=2), free_memory=lambda: 8000)
    busy = sandbox(tmp_path, "default", host, docker, http, clock=lambda: now[0])
    idle = sandbox(tmp_path, "shop", host, docker, http, clock=lambda: now[0])
    await busy.ensure_running()
    await idle.ensure_running()
    now[0] = 300
    busy.touch()
    now[0] = 700  # idle_minutes is 10: shop last used at 0, default at 300
    await idle.stop_if_idle()
    await busy.stop_if_idle()
    assert (idle.status(), busy.status()) == ("stopped", "ready")
    assert docker.running == {"dom-desktop"}
    await busy.close()
    await idle.close()


async def test_a_remote_host_checks_memory_through_the_sandbox_api(tmp_path):
    commands = []
    free = ["20000"]

    def reply(request):
        if request.url.path == "/v1/bash/exec":
            commands.append(json.loads(request.content)["command"])
            return httpx.Response(200, json={"success": True, "data": {"exit_code": 0, "stdout": free[0] + "\n"}})
        return httpx.Response(200, json={"success": True, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        host = Host(DesktopHost(name="studio", mode="remote", url="https://example.org", min_free_memory_mb=16384))
        remote = RemoteSandbox(
            "https://example.org", "key", http=http, isolation_confirmed=True, name="studio", host=host,
        )
        assert await remote.ensure_running() is remote.client
        assert "MemAvailable" in commands[0]
        free[0] = "16383"
        with pytest.raises(SandboxError, match=r"not enough free memory on host studio: 16383 MB free, 16384 MB needed"):
            await remote.ensure_running()
        assert remote.status() == "stopped" and remote.client is None
        free[0] = "not a number"
        with pytest.raises(SandboxError, match="could not read free memory on host studio"):
            await remote.ensure_running()


# ---- the fleet: named containers, routed tasks, per-container state ------------------------------------

from contextlib import nullcontext
from datetime import datetime, timezone

from dom.config import WebConfig
from dom.desktop.fleet import DesktopFleet
from dom.desktop.worker import DesktopWorker
from test_desktop_worker import call
from tghelpers import OWNER


class Box:
    """A sandbox stand-in registered with its host, like the real ones."""

    memory_mb = 1600
    port = None
    error = None
    client = None

    def __init__(self, host, name, free=8000):
        self.name, self.host, self._status, self.free = name, host, "stopped", free
        self.stopped = 0
        host.register(self)

    def status(self):
        return self._status

    async def ensure_running(self):
        self._status = "ready"
        return self

    async def stop(self):
        self.stopped += 1
        self._status = "stopped"

    async def free_memory_mb(self):
        return self.free

    def in_use(self):
        return nullcontext()

    def touch(self):
        pass

    def redact(self, text):
        return text


class Gate:
    """A model that finishes each task once the gate opens, so tasks can overlap."""

    def __init__(self):
        self.open = asyncio.Event()
        self.reports = []

    async def complete(self, messages, tools, *, once=False):
        if once:  # the report that tells the owner how a task went
            self.reports.append(messages)
            return {"content": '{"bubbles": [{"text": "ok"}]}'}
        await self.open.wait()
        return call("finish", result="done")


def fleet_for(app, *hosts, free=8000, **options):
    names = {"pi": DesktopHost(name="pi", mode="local", max_containers=2)}
    configs = [names.get(h, h) if isinstance(h, str) else h for h in hosts] or [names["pi"]]
    app.config = app.config.model_copy(update={
        "desktop_mode": "local", "desktop": DesktopConfig(lan_probes=PROBES, hosts=configs),
    })
    app.client = Gate()
    boxes = {}

    def factory(host, name):
        boxes[name] = Box(host, name, free)
        return boxes[name]

    fleet = DesktopFleet(app, free_memory=lambda: free, sandbox_factory=factory, **options)
    app.desktop_worker = fleet
    fleet.boxes = boxes
    return fleet


async def active(app):
    return {row["container"]: row for row in await app.store.desktop_tasks(OWNER, active=True)}


async def settle(app, fleet):
    app.client.open.set()
    for lane in list(fleet.lanes.values()):
        if lane.worker._job is not None:
            await asyncio.wait_for(asyncio.shield(lane.worker._job), 2)


async def test_tasks_run_on_separate_containers_at_the_same_time(make_app):
    app = make_app()
    fleet = fleet_for(app)
    first = await fleet.enqueue(OWNER, "look at prices")
    second = await fleet.enqueue(OWNER, "read the manual", "manuals")
    assert {c: r["id"] for c, r in (await active(app)).items()} == {"default": first, "manuals": second}
    with pytest.raises(ValueError, match="already on something in manuals"):
        await fleet.enqueue(OWNER, "again", "manuals")
    with pytest.raises(ValueError, match="already on something$"):
        await fleet.enqueue(OWNER, "again")
    assert [c.name for c in fleet.containers()] == ["default", "manuals"]
    await settle(app, fleet)
    assert {row["status"] for row in await app.store.desktop_tasks(OWNER)} == {"done"}


async def test_a_full_host_refuses_before_queueing_anything(make_app):
    app = make_app()
    fleet = fleet_for(app)  # pi allows two
    await fleet.enqueue(OWNER, "one")
    await fleet.enqueue(OWNER, "two", "second")
    with pytest.raises(ValueError, match=r"host pi already runs its limit of 2 desktop containers \(default, second\)"):
        await fleet.enqueue(OWNER, "three", "third")
    assert len(await app.store.desktop_tasks(OWNER)) == 2
    assert "third" not in fleet.lanes  # the refused container leaves nothing behind
    await settle(app, fleet)


async def test_a_memory_floor_refuses_at_enqueue_with_the_numbers(make_app):
    app = make_app()
    fleet = fleet_for(app, free=2000)  # 1600 for the container + the default 512 floor needed
    with pytest.raises(ValueError, match=r"not enough free memory on host pi: 2000 MB free, 2112 MB needed"):
        await fleet.enqueue(OWNER, "one")
    assert await app.store.desktop_tasks(OWNER) == []
    fleet.hosts[0].free_memory = lambda: 2112
    for box in fleet.boxes.values():
        box.free = 2112
    await fleet.enqueue(OWNER, "one")
    await settle(app, fleet)


async def test_one_default_container_behaves_as_before(make_app):
    app = make_app()
    fleet = fleet_for(app, DesktopHost(name="default", mode="local"))
    assert fleet.available()
    task = await fleet.enqueue(OWNER, "look")
    assert (await active(app))["default"]["id"] == task
    with pytest.raises(ValueError, match="already on something$"):
        await fleet.enqueue(OWNER, "look again")
    with pytest.raises(ValueError, match="limit of 1 desktop container "):
        await fleet.enqueue(OWNER, "look elsewhere", "other")
    await settle(app, fleet)


@pytest.mark.parametrize("name", ["Shop", "a b", "../x", "x" * 25, "-a", ""])
async def test_container_names_are_checked(make_app, name):
    app = make_app()
    fleet = fleet_for(app)
    with pytest.raises(ValueError, match="container name"):
        await fleet.enqueue(OWNER, "x", name)


async def test_a_remote_host_is_its_own_named_container(make_app):
    app = make_app()
    studio = DesktopHost(name="studio", mode="remote", url="https://example.org", remote_isolation_confirmed=True)
    fleet = fleet_for(app, DesktopHost(name="pi", mode="local"), studio)
    await fleet.enqueue(OWNER, "render", "studio")
    assert (await active(app))["studio"]["status"] in ("queued", "running")
    assert fleet.lanes["studio"].host.config.name == "studio"
    await settle(app, fleet)
    local_only = fleet_for(make_app(), studio)
    with pytest.raises(ValueError, match="no local host"):
        await local_only.enqueue(OWNER, "x", "other")


async def test_cancel_and_resume_find_the_task_s_container(make_app):
    app = make_app()
    fleet = fleet_for(app)
    first = await fleet.enqueue(OWNER, "one")
    second = await fleet.enqueue(OWNER, "two", "shop")
    assert await fleet.cancel(OWNER, second)
    rows = {row["id"]: row for row in await app.store.desktop_tasks(OWNER)}
    assert rows[second]["status"] == "cancelled" and rows[first]["status"] in ("queued", "running")
    assert fleet.boxes["shop"].stopped == 1 and fleet.boxes["default"].stopped == 0
    assert not await fleet.cancel(OWNER, second)
    assert not await fleet.resume(OWNER, 999, 1)
    await settle(app, fleet)


async def test_finished_extra_containers_leave_the_registry_but_the_default_stays(make_app):
    app = make_app()
    fleet = fleet_for(app)
    await fleet.enqueue(OWNER, "one", "shop")
    await settle(app, fleet)
    await fleet.boxes["shop"].stop()
    assert [c.name for c in fleet.containers()] == ["default"]


async def test_recover_restarts_each_containers_queued_task_and_fails_the_rest(make_app):
    app = make_app()
    fleet = fleet_for(app)
    store = app.store
    queued = await store.enqueue_desktop_task(OWNER, "queued", "default")
    running = await store.enqueue_desktop_task(OWNER, "running", "shop")
    await store.update_desktop_task(running, OWNER, ("queued",), "running")
    await fleet.recover()
    rows = {row["id"]: row for row in await store.desktop_tasks(OWNER)}
    assert rows[running]["status"] == "failed" and rows[running]["error"] == "Dom restarted"
    assert rows[queued]["status"] in ("queued", "running")
    assert fleet.boxes["shop"].stopped == 1  # a leftover container is removed before anything else
    await settle(app, fleet)
    assert (await store.desktop_tasks(OWNER, container="default"))[0]["status"] == "done"


async def test_handoff_and_report_name_a_non_default_container(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"web": WebConfig(public_url="https://example.org")})
    observed = datetime(2026, 1, 1, 6, 5, tzinfo=timezone.utc)
    await app.dispatch.desktop_handoff(1, 2, OWNER, "Sign in", "example.org", observed, "shop")
    await app.dispatch.desktop_handoff(2, 3, OWNER, "Sign in", "example.org", observed)
    shop, main = app.bot.of("send", OWNER)
    assert shop[2].startswith("Dom needs you to log in on the shop desktop (browser was on example.org at 06:05): Sign in")
    assert shop[3]["reply_markup"].inline_keyboard[0][0].url == "https://example.org/desktop?container=shop"
    assert main[2].startswith("Dom needs you to log in (browser was on")
    assert main[3]["reply_markup"].inline_keyboard[0][0].url == "https://example.org/desktop"
    row = {"id": 7, "chat_id": OWNER, "status": "done", "result": "all good", "error": None, "container": "shop",
           "started_at": None, "created_at": "2026-01-01T00:00:00+00:00"}
    app.client = Gate()
    await app.dispatch.report_desktop_task(row)
    assert "desktop task #7 on the shop desktop finished" in str(app.client.reports[0])


async def test_a_screenshot_file_carries_its_container_s_name(make_app):
    app = make_app()
    fleet = fleet_for(app)
    uploads = []

    async def upload(path, content):
        uploads.append(path)

    lane = fleet.lane("shop")
    lane.sandbox.screenshot = lambda: asyncio.sleep(0, b"png")
    lane.sandbox.upload_file = upload
    app.client = Gate()
    app.client.open.set()
    assert await lane.worker._tool(lane.sandbox, "save_screenshot", {"name": "front page"}) == {
        "path": "/home/gem/outbox/shop-front_page.png",
    }
    default = fleet.lane("default")
    default.sandbox.screenshot = lane.sandbox.screenshot
    default.sandbox.upload_file = upload
    await default.worker._tool(default.sandbox, "save_screenshot", {"name": "front page"})
    assert uploads == ["/home/gem/outbox/shop-front_page.png", "/home/gem/outbox/front_page.png"]


async def test_recover_fails_a_task_whose_container_has_no_host_now(make_app):
    app = make_app()
    studio = DesktopHost(name="studio", mode="remote", url="https://example.org", remote_isolation_confirmed=True)
    fleet = fleet_for(app, studio)  # no local host any more
    task = await app.store.enqueue_desktop_task(OWNER, "gone", "shop")
    await fleet.recover()
    row = (await app.store.desktop_tasks(OWNER))[0]
    assert row["id"] == task and row["status"] == "failed" and "no local host" in row["error"]
    assert await app.store.enqueue_desktop_task(OWNER, "free again", "shop") is not None


# ---- reports to the owner ---------------------------------------------------------------------------------

async def test_capabilities_and_self_inspect_report_containers_and_limits(make_app):
    from dom.capabilities import describe, desktop_hosts, desktop_status

    app = make_app()
    fleet = fleet_for(app)
    assert desktop_status(app, "owner_dm") == "available (starts on use)"  # one container: as before
    await fleet.enqueue(OWNER, "look", "shop")
    assert desktop_status(app, "owner_dm") == "available (starts on use); other containers: shop stopped"
    assert "desktop: available (starts on use); other containers: shop" in describe(app, "owner_dm")
    assert desktop_status(app, "group") == "off"
    (host,) = desktop_hosts(app)
    assert host["name"] == "pi" and host["max_containers"] == 2 and host["min_free_memory_mb"] == 512
    assert {c["name"] for c in host["containers"]} == {"default", "shop"}
    await settle(app, fleet)


async def test_owner_task_list_and_status_name_the_container(make_app):
    from dom.telegram import owner

    app = make_app()
    fleet = fleet_for(app)
    await fleet.enqueue(OWNER, "look at prices")
    await fleet.enqueue(OWNER, "read manuals", "shop")
    await owner.show_tasks(app)
    text = app.bot.of("send", OWNER)[-1][2]
    assert "· queued · look at prices" in text or "· running · look at prices" in text
    assert "on shop · " in text and "read manuals" in text
    view = await owner.render_status(app)
    assert "Desktop task #2 on shop" in view.plain and "Desktop task #1 ·" in view.plain
    await settle(app, fleet)


async def test_recover_removes_every_leftover_container_of_a_previous_process(make_app, tmp_path):
    app = make_app()
    docker = Docker()
    docker.running = {"dom-desktop", "dom-desktop-old"}
    app.config = config(tmp_path).model_copy(update={"home": app.config.home})
    app.client = Gate()
    async with httpx.AsyncClient() as http:
        fleet = DesktopFleet(
            app, sandbox_factory=lambda host, name: LocalSandbox(app.config, run=docker, http=http, name=name, host=host),
        )
        app.desktop_worker = fleet
        await fleet.recover()
        assert docker.running == set()
        assert [c.name for c in fleet.containers()] == ["default"]  # the stray's lane is forgotten once it is gone
        assert ("docker", "stop", "dom-desktop-old") in docker.calls
        await fleet.close()


def test_doctor_checks_every_desktop_host(tmp_path):
    from dom.doctor import _desktop, _unknown_config_keys

    def check(*hosts, **settings):
        return _desktop(Config(
            home=tmp_path / "home", desktop_mode="remote",
            desktop=DesktopConfig(hosts=list(hosts), **settings),
        ))

    bad = check(DesktopHost(name="studio", mode="remote"))
    assert bad.status == "fail" and "desktop host studio's url" in bad.detail
    unconfirmed = check(DesktopHost(name="studio", mode="remote", url="https://example.org"))
    assert unconfirmed.status == "fail" and "remote_isolation_confirmed" in unconfirmed.detail
    good = check(DesktopHost(name="studio", mode="remote", url="https://example.org", remote_isolation_confirmed=True))
    assert good.status == "ok"
    raw = {"desktop": {"hosts": [{"name": "pi", "mode": "local", "max_container": 2}]}}
    assert _unknown_config_keys(raw) == ["desktop.hosts[0].max_container"]


def test_free_memory_is_read_from_meminfo(tmp_path):
    from dom.desktop.sandbox import read_free_memory_mb

    info = tmp_path / "meminfo"
    info.write_text("MemTotal:        3886712 kB\nMemFree: 100 kB\nMemAvailable:    2653164 kB\n")
    assert read_free_memory_mb(str(info)) == 2590
    info.write_text("MemTotal: 1 kB\n")
    assert read_free_memory_mb(str(info)) is None
    info.write_text("MemAvailable: lots\n")
    assert read_free_memory_mb(str(info)) is None
    assert read_free_memory_mb(str(tmp_path / "missing")) is None


async def test_two_tasks_run_end_to_end_on_two_real_sandboxes(make_app, tmp_path):
    app = make_app()
    docker = Docker()
    app.config = config(tmp_path).model_copy(update={"home": app.config.home})
    app.client = Gate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "data": []}),
    )) as http:
        fleet = DesktopFleet(
            app, free_memory=lambda: 8000,
            sandbox_factory=lambda host, name: LocalSandbox(app.config, run=docker, http=http, name=name, host=host),
        )
        app.desktop_worker = fleet
        first = await fleet.enqueue(OWNER, "one")
        second = await fleet.enqueue(OWNER, "two", "shop")
        for _ in range(200):  # both containers are up before either task may finish
            if len(docker.started()) == 2:
                break
            await asyncio.sleep(0.01)
        assert docker.running == {"dom-desktop", "dom-desktop-shop"} and len(docker.probes()) == 2
        assert [c.status() for c in fleet.containers()] == ["ready", "ready"]
        await settle(app, fleet)
        rows = {row["id"]: row for row in await app.store.desktop_tasks(OWNER)}
        assert (rows[first]["status"], rows[second]["status"]) == ("done", "done")
        await fleet.close()
        assert docker.running == set()
