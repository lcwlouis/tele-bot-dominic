"""Self-directed work (W13): planning, announcement, cancel, gating, budgets, the network guard, the dashboard."""

import asyncio
import ipaddress
import json
import shlex
from contextlib import nullcontext
from datetime import timedelta

import pytest
from aiogram.types import CallbackQuery, User

from dom import selfwork
from dom.config import DesktopConfig, SelfWorkConfig
from dom.desktop import network
from dom.desktop.client import SandboxError
from dom.desktop.fleet import DesktopFleet
from dom.desktop.sandbox import LocalSandbox, desktop_profile_path
from dom.desktop.worker import OWN_SYSTEM_PROMPT
from dom.selfwork import CONTAINER, GUARD_KEY, parse_plan, parse_task
from dom.telegram.handlers import handle_callback
from test_desktop_worker import Model, call
from test_web_pages import browser  # noqa: F401  (fixture)
from test_web_auth import POST_HEADERS
from tghelpers import FRIEND, OWNER, tg

PNG = b"\x89PNG\r\n\x1a\nfake"
GATEWAY = str(ipaddress.IPv4Network(DesktopConfig().subnet)[1])
TASK = {
    "goal": "Read about tide pools", "why": "Curious after a chat about beaches",
    "tools": ["browse", "read_page", "save_screenshot"], "sites": ["en.wikipedia.org"], "minutes": 10, "steps": 8,
}
PRACTICE = {"goal": "Practise list comprehensions", "why": "To get better", "tools": ["run_python"], "sites": [], "minutes": 5, "steps": 4}


def guard_reply(command, *, lan="timeout", control="open", sampled="timeout"):
    """What the guard script prints inside the container, for the given outcomes."""
    spec = json.loads(shlex.split(command)[-1])
    results = {spec["control"]: control}
    for target in spec["targets"]:
        results[target] = sampled if target in network.SAMPLED else lan
    for port in spec["gateway_ports"]:
        results[f"{GATEWAY}:{port}"] = lan
    return {"exit_code": 0, "stdout": json.dumps({"results": results, "gateway": GATEWAY}), "stderr": ""}


class Box:
    """A fake local sandbox and its client in one."""

    def __init__(self):
        self.url = "about:blank"
        self.commands, self.navigated, self.files = [], [], {}
        self.resets = self.stops = 0
        self.guard = {}
        self.after_click = None

    def status(self):
        return "ready"

    async def ensure_running(self):
        return self

    def in_use(self):
        return nullcontext()

    def touch(self):
        pass

    def redact(self, text):
        return text

    async def reset_profile(self):
        self.resets += 1

    async def stop(self):
        self.stops += 1

    async def bash(self, command):
        self.commands.append(command)
        if "/proc/net/route" in command:
            return guard_reply(command, **self.guard)
        return {"exit_code": 0, "stdout": "ran", "stderr": ""}

    async def browser_tabs(self):
        return [{"index": 0, "url": self.url, "title": "", "is_active": True}]

    async def current_url(self):
        return self.url

    async def navigate(self, url):
        self.url = url
        self.navigated.append(url)
        return {}

    async def markdown(self):
        return {"markdown": "Tide pools are rocky pools by the sea."}

    async def click(self, selector_or_index):
        if self.after_click:
            self.url = self.after_click
        return {}

    async def fill(self, selector, text):
        return {}

    async def screenshot(self):
        return PNG

    async def upload_file(self, path, data):
        self.files[path] = data

    async def download_file(self, path, max_bytes=None):
        return self.files[path]

    async def write_file(self, path, content):
        return {}


class Idle:
    def status(self):
        return "stopped"


def self_app(make_app, *replies, **settings):
    app = make_app()
    app.config = app.config.model_copy(update={
        "desktop_mode": "local", "desktop": DesktopConfig(lan_probes=["192.0.2.1:443"]),
        "self_work": SelfWorkConfig(**settings),
    })
    app.client = Model(*replies)
    app.box = Box()
    app.desktop_worker = DesktopFleet(app, sandbox_factory=lambda host, name: app.box if name == CONTAINER else Idle())
    return app


@pytest.fixture(autouse=True)
def no_host_lookup(monkeypatch):
    monkeypatch.setattr(network, "host_address", lambda control: "192.0.2.5")


def plan_reply(*tasks, announcement="Got curious tonight. Tomorrow I want to poke at a couple of things."):
    return {"role": "assistant", "content": json.dumps({"announcement": announcement, "tasks": list(tasks)})}


async def planned(app, *tasks):
    now = app.dispatch.now()
    return await app.store.add_self_tasks(
        [parse_task(task, 20, 25) for task in tasks], now - timedelta(hours=4), now - timedelta(hours=1),
    )


async def run_next(app):
    await app.self_work.tick()
    if app.self_work._job is not None:
        await asyncio.wait_for(asyncio.shield(app.self_work._job), 2)


async def status(app, task_id):
    return next(row for row in await app.store.self_tasks() if row["id"] == task_id)


# ---- planning and announcement ----------------------------------------------------------------------


def test_plan_validation_keeps_only_safe_well_formed_tasks():
    tasks = [
        TASK,
        {**TASK, "tools": ["handoff"]},  # not a self-directed tool
        {**TASK, "tools": ["propose_skill"]},
        {**TASK, "sites": ["192.0.2.1"]},  # an IP literal
        {**TASK, "sites": [network.SAMPLED[2].partition(":")[0]]},  # a LAN address
        {**TASK, "sites": ["printer" + ".local"]},  # an mDNS name
        {**TASK, "sites": []},  # browser tools need sites
        {**PRACTICE, "sites": ["example.org"]},  # sites need browser tools
        {**TASK, "minutes": 0},
        {**TASK, "goal": ""},
    ]
    assert parse_plan({"announcement": "hi", "tasks": tasks}, 20, 25) == ("hi", [parse_task(TASK, 20, 25)])
    clamped = parse_task({**TASK, "minutes": 500, "steps": 99, "sites": ["www.Example.org"]}, 20, 25)
    assert (clamped["minutes"], clamped["steps"], clamped["sites"]) == (20, 25, ["example.org"])
    assert len(parse_plan({"tasks": [PRACTICE] * 5}, 20, 25)[1]) == 3
    assert parse_plan({"tasks": "nope"}, 20, 25) is None


async def test_plan_announces_tasks_in_the_owner_dm_with_cancel_buttons(make_app):
    app = self_app(make_app, plan_reply(TASK, PRACTICE), delay_hours=3)
    await app.self_work.plan()
    rows = sorted(await app.store.self_tasks(), key=lambda row: row["id"])
    assert [row["goal"] for row in rows] == [TASK["goal"], PRACTICE["goal"]]
    assert all(row["status"] == "planned" for row in rows)
    now = app.dispatch.now()
    assert rows[0]["not_before"] == (now + timedelta(hours=3)).isoformat()
    sends = app.bot.of("send", OWNER)
    assert len(sends) == 1 and not app.bot.of("send", FRIEND)
    _, _, text, kwargs = sends[0]
    assert text.startswith("Got curious tonight.")
    assert "en.wikipedia.org" in text and "none, offline" in text and "up to 10 min and 8 steps" in text
    buttons = [row[0].callback_data for row in kwargs["reply_markup"].inline_keyboard]
    assert buttons == [f"own:cancel:{row['id']}" for row in rows]
    request = app.client.requests[0][0]
    assert "<soul>" in request[1]["content"] and "Never plan logins" in request[0]["content"]


async def test_a_new_plan_expires_the_previous_one_and_inactive_setups_do_not_plan(make_app):
    app = self_app(make_app, plan_reply(PRACTICE))
    [old] = await planned(app, TASK)
    await app.self_work.plan()
    assert (await status(app, old["id"]))["status"] == "expired"

    off = self_app(make_app, enabled=False)  # the same home and database
    before = await off.store.self_tasks()
    await off.self_work.plan()
    assert off.client.requests == [] and await off.store.self_tasks() == before
    remote = make_app()
    remote.config = remote.config.model_copy(update={"desktop_mode": "remote"})
    remote.client = Model()
    await remote.self_work.plan()
    assert remote.client.requests == []


async def test_an_empty_plan_sends_nothing(make_app):
    app = self_app(make_app, plan_reply())
    await app.self_work.plan()
    assert app.bot.of("send", OWNER) == [] and await app.store.self_tasks() == []


# ---- cancel -------------------------------------------------------------------------------------------


def cancel_query(task_id, user=OWNER, chat=OWNER):
    return CallbackQuery(
        id="q", from_user=User(id=user, is_bot=False, first_name="O"), chat_instance="c",
        message=tg(chat, sender=OWNER, text="plan"), data=f"own:cancel:{task_id}",
    )


async def test_cancel_from_the_owner_dm_only(make_app):
    app = self_app(make_app)
    [row] = await planned(app, TASK)
    await handle_callback(app, cancel_query(row["id"], user=FRIEND))
    assert (await status(app, row["id"]))["status"] == "planned"
    await handle_callback(app, cancel_query(row["id"]))
    assert (await status(app, row["id"]))["status"] == "cancelled"
    assert app.bot.of("answer")[-1][2] == "Cancelled"
    await run_next(app)
    assert app.client.requests == [] and app.box.resets == 0


async def test_cancel_stops_a_running_task(make_app):
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Future()

    app = self_app(make_app, blocked)
    [row] = await planned(app, TASK)
    await app.self_work.tick()
    await asyncio.wait_for(started.wait(), 2)
    assert (await status(app, row["id"]))["status"] == "running"
    assert await app.self_work.cancel(row["id"])
    assert (await status(app, row["id"]))["status"] == "cancelled"
    assert app.box.stops >= 1 and not app.self_work.busy()


# ---- delay and idle gating ----------------------------------------------------------------------------


async def test_a_task_waits_for_its_delay_and_for_dom_to_be_awake_and_idle(make_app):
    app = self_app(make_app, call("finish", result="Done"))
    now = app.dispatch.now()
    [row] = await app.store.add_self_tasks([parse_task(PRACTICE, 20, 25)], now, now + timedelta(hours=3))
    await run_next(app)
    assert (await status(app, row["id"]))["status"] == "planned"  # the delay has not passed

    app.dispatch.now.advance(hours=3)
    await app.dispatch.presence.sleep(OWNER, app.dispatch._habits())
    assert await app.self_work.wait_reason() == "asleep"
    await run_next(app)
    assert (await status(app, row["id"]))["status"] == "planned"

    await app.dispatch.presence.wake(OWNER, app.dispatch._habits())
    assert await app.self_work.wait_reason() == "in a conversation"  # online in the owner's DM
    await app.store.set_presence(OWNER, online_until=None, is_sleeping=False, sleep_until=None)
    async with app.locks[FRIEND]:
        assert await app.self_work.wait_reason() == "in a conversation"  # a turn is running somewhere
    app.desktop_worker.lane().worker._stopping = 1
    assert await app.self_work.wait_reason() == "the desktop is busy"
    app.desktop_worker.lane().worker._stopping = 0

    assert await app.self_work.wait_reason() is None
    await run_next(app)
    assert (await status(app, row["id"]))["status"] == "done"


async def test_unstarted_tasks_expire_after_a_day_and_restart_fails_running_ones(make_app):
    app = self_app(make_app)
    [row, other] = await planned(app, TASK, PRACTICE)
    await app.store.update_self_task(other["id"], ("planned",), "running")
    app.dispatch.now.advance(hours=21)
    await app.self_work.tick()
    assert (await status(app, row["id"]))["status"] == "expired"
    assert (await status(app, other["id"]))["status"] == "failed"
    assert (await status(app, other["id"]))["error"] == "Dom restarted"

    off = self_app(make_app, enabled=False)
    [stale] = await planned(off, PRACTICE)
    off.dispatch.now.advance(hours=21)
    await off.self_work.tick()
    assert (await status(off, stale["id"]))["status"] == "expired" and off.client.requests == []


# ---- the run: tools, sites, budgets, results ------------------------------------------------------------


async def test_a_finished_task_keeps_screenshots_for_the_dashboard_and_sends_one_short_summary(make_app):
    app = self_app(
        make_app,
        call("browse", url="https://en.wikipedia.org/wiki/Tide_pool"), call("read_page"),
        call("save_screenshot", name="pools"),
        call("finish", result="Tide pools hold anemones. " + "x" * 600, files=[f"/home/gem/outbox/{CONTAINER}-pools.png"]),
    )
    [row] = await planned(app, TASK)
    await run_next(app)
    done = await status(app, row["id"])
    assert done["status"] == "done" and done["steps"] == 4
    assert json.loads(done["files_json"]) == [f"{CONTAINER}-pools.png"]
    assert (selfwork.task_files(app.home.root, row["id"]) / f"{CONTAINER}-pools.png").read_bytes() == PNG
    assert app.box.resets == 1 and app.box.stops >= 1  # a fresh profile, and the container stopped after
    sends = [call for call in app.bot.calls if call[0] == "send"]
    assert len(sends) == 1 and sends[0][1] == OWNER
    assert sends[0][2].startswith(f"Finished my own task #{row['id']}") and len(sends[0][2]) < 500
    assert "Screenshots are on the dashboard." in sends[0][2]
    messages, schemas, _ = app.client.requests[0]
    assert messages[0]["content"] == OWN_SYSTEM_PROMPT
    assert "en.wikipedia.org" in messages[1]["content"]
    assert sorted(schema["function"]["name"] for schema in schemas) == sorted([*TASK["tools"], "finish"])
    assert json.loads(await app.store.kv_get(GUARD_KEY))["ok"] is True


async def test_tools_outside_the_plan_and_sites_outside_the_plan_are_refused(make_app):
    app = self_app(
        make_app,
        call("handoff", reason="log in please"),  # not offered: no logins
        call("run_shell", command="curl http://example.org"),  # not in this plan
        call("browse", url="https://shop.example.com/"),  # not a planned site
        call("browse", url="https://en.wikipedia.org/wiki/Tide_pool"),
        call("finish", result="Read a little."),
    )
    [row] = await planned(app, TASK)
    await run_next(app)
    assert (await status(app, row["id"]))["status"] == "done"
    results = [m["content"] for m in app.client.requests[-1][0] if m["role"] == "tool"]
    assert "Tool error: invalid arguments" in results[0] and "Tool error: invalid arguments" in results[1]
    assert "Blocked: that site is not in this task" in results[2]
    assert app.box.navigated == ["https://en.wikipedia.org/wiki/Tide_pool"]
    assert not any("/proc/net/route" not in command for command in app.box.commands)  # no shell ran
    assert len(app.bot.of("send", OWNER)) == 1  # no handoff message


async def test_a_page_that_leaves_the_plan_is_closed(make_app):
    app = self_app(
        make_app,
        call("browse", url="https://en.wikipedia.org/wiki/Tide_pool"), call("click", selector_or_index=3),
        call("finish", result="Ok"),
    )
    app.box.after_click = "https://tracker.example.net/landing"
    [row] = await planned(app, {**TASK, "tools": ["browse", "click"]})
    await run_next(app)
    results = [m["content"] for m in app.client.requests[-1][0] if m["role"] == "tool"]
    assert "so it was closed" in results[1]
    assert app.box.navigated[-1] == "about:blank" and app.box.url == "about:blank"
    assert (await status(app, row["id"]))["status"] == "done"


async def test_code_runs_offline_and_fill_takes_only_short_search_text(make_app):
    app = self_app(
        make_app, call("run_python", code="print(1)"),
        call("fill", selector="#password", text="hunter2"), call("fill", selector="#q", text="x" * 200),
        call("fill", selector="#q", text="tide pools"), call("finish", result="Practised"),
    )
    [row] = await planned(app, {**PRACTICE, "tools": ["run_python", "browse", "fill"], "sites": ["duckduckgo.com"], "steps": 9})
    await run_next(app)
    [python] = [command for command in app.box.commands if "python3 /tmp/" in command]
    assert "unshare --net --map-root-user -- sh -c" in python
    results = [m["content"] for m in app.client.requests[-1][0] if m["role"] == "tool"]
    assert "Blocked: fill" in results[1] and "Blocked: fill" in results[2] and "Blocked" not in results[3]


@pytest.mark.parametrize("over", ["steps", "time"])
async def test_step_and_time_budgets_stop_the_task(make_app, over):
    async def slow():
        await asyncio.sleep(5)

    replies = [call("read_page")] * 3 if over == "steps" else [slow]
    app = self_app(make_app, *replies)
    task = {**TASK, "steps": 2} if over == "steps" else TASK
    [row] = await planned(app, task)
    if over == "time":  # a budget of a few milliseconds
        await app.store._run(lambda: app.store._conn.execute("UPDATE self_tasks SET minutes = 0.0005 WHERE id = ?", (row["id"],)))
    await run_next(app)
    stopped = await status(app, row["id"])
    assert stopped["status"] == "failed"
    assert stopped["error"] == ("step budget reached" if over == "steps" else "time budget reached")
    assert app.bot.of("send", OWNER)[-1][2].startswith(f"Stopped my own task #{row['id']}")
    if over == "steps":
        assert len(app.client.requests) == 2


# ---- the network guard ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("guard", "reason"), [
    ({"lan": "open"}, "can reach 192.0.2.1:443"),
    ({"lan": "refused"}, "can reach"),
    ({"sampled": "refused"}, f"can reach {network.SAMPLED[0]}"),
    ({"control": "timeout"}, "proves nothing"),
])
async def test_an_unverified_guard_refuses_the_task_and_tells_the_owner(make_app, guard, reason):
    app = self_app(make_app, call("finish", result="never"))
    app.box.guard = guard
    [row] = await planned(app, TASK)
    await run_next(app)
    refused = await status(app, row["id"])
    assert refused["status"] == "refused" and reason in refused["error"]
    assert app.client.requests == [] and app.box.navigated == []  # no model call, no page
    [send] = app.bot.of("send", OWNER)
    assert send[2].startswith(f"I didn't start my own task #{row['id']}") and reason in send[2]
    record = json.loads(await app.store.kv_get(GUARD_KEY))
    assert record["ok"] is False and reason in record["detail"]


async def test_guard_probes_known_lan_services_the_host_and_the_gateway():
    class Client:
        async def bash(self, command):
            self.spec = json.loads(shlex.split(command)[-1])
            return guard_reply(command)

    from dom.config import Config, WebConfig
    config = Config(home=".", desktop=DesktopConfig(lan_probes=["192.0.2.1:443"]), web=WebConfig(port=8090))
    client = Client()
    assert await network.verify_inside(client, config, address="192.0.2.5") is None
    assert {"192.0.2.1:443", "192.0.2.5:22", "192.0.2.5:8090", *network.SAMPLED} <= set(client.spec["targets"])
    assert client.spec["gateway_ports"] == [22, 8090] and client.spec["control"] == "1.1.1.1:443"
    assert network.judge({"1.1.1.1:443": "open", "a:1": "unreachable"}, "1.1.1.1:443", ["a:1"], ()) == (
        "the sandbox can reach a:1 on the local network"
    )

    class Broken:
        async def bash(self, command):
            return {"exit_code": 1, "stdout": "", "stderr": "python3: not found"}

    assert "could not run" in await network.verify_inside(Broken(), config, address="192.0.2.5")
    empty = config.model_copy(update={"desktop": DesktopConfig()})
    assert "lan_probes" in await network.verify_inside(client, empty)


async def test_the_default_profile_is_never_reset_and_a_named_one_is_emptied(make_app):
    app = make_app()
    config = app.config.model_copy(update={"desktop_mode": "local"})
    default = LocalSandbox(config)
    with pytest.raises(SandboxError):
        await default.reset_profile()
    named = LocalSandbox(config, name=CONTAINER)
    named._status = "stopped"

    async def no_docker(*args, **kwargs):
        return 1, b""

    named._docker = no_docker
    profile = desktop_profile_path(config, CONTAINER)
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("session")
    await named.reset_profile()
    assert not profile.exists()
    assert desktop_profile_path(config).name != profile.name


# ---- dashboard -----------------------------------------------------------------------------------------------


async def test_dashboard_shows_the_plan_results_and_guard_and_cancels(make_app, browser):  # noqa: F811
    app = self_app(make_app)
    [row, other] = await planned(app, TASK, PRACTICE)
    await app.store.update_self_task(other["id"], ("planned",), "running")
    await app.store.update_self_task(other["id"], ("running",), "done", result="Wrote three <b>examples</b>", files=["shot.png"])
    folder = selfwork.task_files(app.home.root, other["id"])
    folder.mkdir(parents=True)
    (folder / "shot.png").write_bytes(PNG)
    await app.store.kv_set(GUARD_KEY, json.dumps({"ok": True, "at": app.dispatch.now().isoformat(), "detail": "verified"}))
    async with browser(app) as client:
        page = (await client.get("/own-work")).text
        assert "Read about tide pools" in page and "en.wikipedia.org" in page and "verified from inside the sandbox" in page
        assert "Wrote three &lt;b&gt;examples&lt;/b&gt;" in page
        assert f'hx-post="/own-work/{row["id"]}/cancel"' in page
        assert f'hx-post="/own-work/{other["id"]}/cancel"' not in page
        assert f'src="/own-work/{other["id"]}/files/shot.png"' in page
        image = await client.get(f"/own-work/{other['id']}/files/shot.png")
        assert image.status_code == 200 and image.content == PNG and image.headers["content-type"] == "image/png"
        assert (await client.get(f"/own-work/{other['id']}/files/..%2Fsecret.png")).status_code == 404
        assert "Own work" in (await client.get("/")).text  # the home view shows an upcoming plan
        response = await client.post(f"/own-work/{row['id']}/cancel", headers=POST_HEADERS)
        assert response.status_code == 200 and f"Cancelled task #{row['id']}." in response.text
        assert (await client.post(f"/own-work/{row['id']}/cancel")).status_code == 403  # not an htmx same-origin post
    assert (await status(app, row["id"]))["status"] == "cancelled"
