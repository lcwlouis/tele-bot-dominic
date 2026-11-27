"""Desktop tasks for groups and friends (E111): the owner's card, the isolated container, the limits, what goes back."""

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from dom import guest_desktop
from dom.config import Config, DesktopConfig, DesktopHost, GuestDesktopConfig, ModelEndpoint
from dom.desktop import network
from dom.desktop import worker as worker_module
from dom.desktop.client import SandboxError
from dom.desktop.fleet import DesktopFleet
from dom.desktop.sandbox import DEFAULT, GUEST, LocalSandbox, Refused, desktop_profile_path
from dom.desktop.worker import GUEST_PARTIAL, GUEST_SYSTEM_PROMPT, OwnRun, Run
from dom.llm import ChatClient
from dom.tools import REQUEST_GUEST_DESKTOP, TurnCtx, enabled_tools
from dom.usage import UsageRecorder
from test_desktop_sandbox import FakeDocker, local_config
from test_desktop_worker import Model, call
from test_selfwork import Box, guard_reply  # noqa: F401  (guard_reply: the fake's network guard answer)
from tghelpers import FRIEND, GROUP, OWNER

BRIEF = "find the opening hours of the botanic garden"
SAY = {"content": json.dumps({"no_response": False, "bubbles": [{"text": "It opens at 9, closes at 6."}]})}
STUDIO = DesktopHost(name="studio", mode="remote", url="https://studio.example.org", remote_isolation_confirmed=True, guest=True)
PI = DesktopHost(name="pi", mode="local")


class Container(Box):
    """A fake container that holds a slot on its host, as a real one does, and obeys the host's admission check."""

    def __init__(self, host, name, mode="local"):
        super().__init__()
        self.host, self.name, self.state, self.port, self.remote = host, name, "stopped", None, mode == "remote"
        host.register(self)

    def __getattribute__(self, name):
        if name in ("reset_profile", "stop") and object.__getattribute__(self, "remote"):
            raise AttributeError(name)  # Dom neither stops a remote sandbox nor resets its profile
        return super().__getattribute__(name)

    def status(self):
        return self.state

    async def ensure_running(self):
        async with self.host.start_lock:
            await self.host.admit(self)
        self.state = "ready"
        return self

    async def free_memory_mb(self):
        return self.host.free_memory()

    async def stop(self):
        self.stops += 1
        self.state = "stopped"


def guest_app(make_app, *replies, hosts=(), free=8000, **guest):
    app = make_app()
    app.config = app.config.model_copy(update={
        "desktop_mode": "local",
        "desktop": DesktopConfig(lan_probes=["192.0.2.1:443"], hosts=list(hosts), guest=GuestDesktopConfig(**guest)),
    })
    app.client = Model(*replies)
    app.boxes = {}

    def build(host, name):
        app.boxes[name] = Container(host, name, host.config.mode)
        return app.boxes[name]

    app.desktop_worker = DesktopFleet(app, free_memory=lambda: free, sandbox_factory=build)
    return app


def ask(app, chat=GROUP, sender=FRIEND, scope="group"):
    return TurnCtx(scope, chat, sender, app.home, app.store, OWNER, app=app)


async def approve(app, chat=GROUP, title="Book club"):
    await app.store.set_chat_auth_status(chat, "approved", title=title)


async def request(app, brief=BRIEF, **options):
    return await REQUEST_GUEST_DESKTOP.handler({"brief": brief}, ask(app, **options))


async def cards(app):
    """Wait for the owner's card, and return the pending permission request it belongs to."""
    async with asyncio.timeout(2):
        while not (pending := await app.store.pending_desktop_permission_requests()):
            await asyncio.sleep(0.001)
    return pending[0]


async def settled(app, chat=GROUP, count=1):
    async with asyncio.timeout(2):
        while len(app.bot.of("send", chat)) < count:
            await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)
    return [call[2] for call in app.bot.of("send", chat)]


async def guest_task(app):
    async with asyncio.timeout(2):
        while not (rows := [row for row in await app.store.desktop_tasks(OWNER) if row["guest_chat_id"] is not None]):
            await asyncio.sleep(0.001)
    return rows[0]


async def finished(app, task_id, status="done"):
    row = None
    try:
        async with asyncio.timeout(2):
            while (row := next(r for r in await app.store.desktop_tasks(OWNER) if r["id"] == task_id))["status"] != status:
                await asyncio.sleep(0.001)
    except TimeoutError:
        raise AssertionError(f"task {task_id} never became {status}: {row}") from None
    return row


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(network, "host_address", lambda control: "192.0.2.5")
    monkeypatch.setattr(worker_module, "GUEST_POLL_SECONDS", 0.01)


# ---- the card flow ---------------------------------------------------------------------------------------------------


async def test_a_request_sends_the_owner_a_card_with_allow_once_and_reject_and_never_allow_always(make_app):
    app = guest_app(make_app, call("finish", result="Open 9 to 6."), SAY)
    await approve(app)
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book club")

    assert "Asked the owner" in await request(app)
    pending = await cards(app)
    [(_, owner, text, kwargs)] = app.bot.of("send", OWNER)
    assert owner == OWNER and not app.bot.of("send", GROUP)  # the asker hears nothing from the card
    assert "Chat: Book club (group)" in text and "Asked by: someone in the chat" in text
    assert BRIEF in text and "untrusted" in text and "Limits: 15 steps, 5 minutes" in text
    buttons = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert buttons == ["Allow once", "Reject"]
    perms = app.desktop_permissions
    assert await perms.decide(pending["id"], "always", via="dashboard") == (False, "this one is always asked")
    assert (await perms.get_policies())["guest_desktop"] == "ask"
    with pytest.raises(ValueError):
        await perms.set_policy("guest_desktop", "allow")  # there is no standing yes
    assert await app.store.desktop_tasks(OWNER) == []  # nothing runs until Allow once


async def test_allow_once_runs_the_task_and_only_a_written_answer_reaches_the_chat(make_app):
    app = guest_app(make_app, call("browse", url="https://garden.example.org"), call("read_page"),
                    call("finish", result="Open 9 to 6. See https://garden.example.org/hours", files=["/home/gem/outbox/a.png"]), SAY)
    await approve(app)
    await request(app)
    pending = await cards(app)
    assert await app.desktop_permissions.decide(pending["id"], "once", via="dashboard") == (True, "✅ Allowed once")

    task = await guest_task(app)
    assert (task["guest_chat_id"], task["guest_chat_title"], task["container"]) == (GROUP, "Book club", GUEST)
    assert task["chat_id"] == OWNER  # the owner's row: the Work page shows it, cancel and delete work as usual
    row = await finished(app, task["id"])
    assert row["result"] == "Open 9 to 6. See https://garden.example.org/hours"

    sent = await settled(app)
    assert sent == ["It opens at 9, closes at 6."]
    assert {call[0] for call in app.bot.calls if call[1] == GROUP} == {"send", "action"}  # no photo, document or file
    notices = [text for _, _, text, _ in app.bot.of("send", OWNER)]
    assert any(text.startswith(f"Done: guest task #{task['id']} for Book club") for text in notices)
    assert app.boxes[GUEST].stops >= 1 and app.boxes[GUEST].files == {}  # the container (and its profile) is stopped afterwards


async def test_reject_and_an_unanswered_card_both_end_in_a_kind_vague_no(make_app):
    app = guest_app(make_app, SAY)
    await approve(app)
    await request(app)
    await app.desktop_permissions.decide((await cards(app))["id"], "reject", via="dashboard")
    assert await settled(app) == ["It opens at 9, closes at 6."]  # in his voice, with no reason
    assert await app.store.desktop_tasks(OWNER) == []
    assert "Allowed" not in "".join(text for _, _, text, _ in app.bot.of("send", GROUP))

    expired = guest_app(make_app)  # no model reply to write one: the plain fallback
    await approve(expired)
    expired.script.contents.clear()
    guest_desktop.APPROVAL_TIMEOUT, original = 0.05, guest_desktop.APPROVAL_TIMEOUT
    try:
        await request(expired)
        assert await settled(expired) == [guest_desktop.NO]
    finally:
        guest_desktop.APPROVAL_TIMEOUT = original
    assert await expired.store.desktop_tasks(OWNER) == []
    assert await expired.store.pending_desktop_permission_requests() == []


async def test_only_groups_and_friends_may_ask_and_a_chat_may_not_flood_the_owner(make_app):
    app = guest_app(make_app, SAY)
    await approve(app)
    assert "only for groups and friends" in await request(app, chat=OWNER, scope="owner_dm")
    assert "owner is here" in await request(app, sender=OWNER)
    assert "must be 1 to 300" in await request(app, brief="x" * 301)
    assert "must be 1 to 300" in await request(app, brief="  ")
    assert (await request(app)).startswith("Asked the owner")
    assert "already asked" in await request(app)  # one card per chat
    assert len(app.bot.of("send", OWNER)) <= 1


async def test_the_tool_is_offered_only_in_groups_and_friend_chats_and_never_beside_the_owners_secrets(make_app):
    app = guest_app(make_app)
    group = {tool.name for tool in await enabled_tools(app, "group")}
    friend = {tool.name for tool in await enabled_tools(app, "friend_dm")}
    owner = {tool.name for tool in await enabled_tools(app, "owner_dm")}
    assert "request_guest_desktop" in group and "request_guest_desktop" in friend and "request_guest_desktop" not in owner
    for scope_tools in (group, friend):
        assert not scope_tools & {"request_secret", "start_desktop_task", "message_chat"}
    await app.desktop_permissions.set_policy("guest_desktop", "never")
    assert "request_guest_desktop" not in {tool.name for tool in await enabled_tools(app, "group")}
    assert "off right now" in await request(app)
    off = guest_app(make_app, enabled=False)
    assert "request_guest_desktop" not in {tool.name for tool in await enabled_tools(off, "group")}


# ---- privacy of the brief ----------------------------------------------------------------------------------------------


async def test_a_brief_that_repeats_private_notes_or_a_credential_is_refused_in_code(make_app, monkeypatch):
    app = guest_app(make_app)
    await approve(app)
    private = app.home.root / "private"
    private.mkdir(exist_ok=True)
    (private / "NOTES.md").write_text("The owner keeps a safe behind the blue painting\n")
    leaked = await request(app, brief="check where the owner keeps a safe behind the blue painting please")
    assert "repeats something private" in leaked
    monkeypatch.setenv("MODEL_API_KEY_TEST", "sk-live-0123456789abcdef")
    secret_app = guest_app(make_app)
    secret_app.config = secret_app.config.model_copy(update={"models": {"chat": ModelEndpoint(
        base_url="https://llm.test/v1", model="m", api_key_env="MODEL_API_KEY_TEST", input=("text",))}})
    await approve(secret_app)
    assert "repeats something private" in await request(secret_app, brief="look up sk-live-0123456789abcdef online")
    assert app.bot.of("send", OWNER) == [] and await app.store.pending_desktop_permission_requests() == []


async def test_the_worker_sees_only_the_fixed_prompt_and_the_brief(make_app):
    app = guest_app(make_app, call("finish", result="Open 9 to 6."), SAY)
    await approve(app)
    task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    await finished(app, task_id)
    system, user = app.client.requests[0][0]
    assert system == {"role": "system", "content": GUEST_SYSTEM_PROMPT}
    assert user == {"role": "user", "content": BRIEF}
    offered = {schema["function"]["name"] for schema in app.client.requests[0][1]}
    assert offered == {"browse", "read_page", "list_elements", "click", "fill", "press", "scroll", "finish"}


# ---- isolation ---------------------------------------------------------------------------------------------------------


async def run_container(tmp_path, name):
    docker = FakeDocker()

    def reply(request):
        return httpx.Response(200, json={"success": True, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        sandbox = LocalSandbox(local_config(tmp_path), run=docker, http=http, name=name)
        await sandbox.ensure_running()
        await sandbox.close()
    return docker, sandbox


async def test_the_guest_container_has_no_profile_no_logins_and_no_mounts_of_the_owners(tmp_path):
    docker, guest = await run_container(tmp_path, GUEST)
    [args] = docker.started()
    assert "-v" not in args and not any("/home/gem" in arg or "/workspace" in arg or str(tmp_path) in arg for arg in args)
    assert not desktop_profile_path(local_config(tmp_path), GUEST).exists()  # no profile directory, on disk or anywhere
    assert docker.probes()  # the firewall is still proven before every start
    assert "--security-opt" in args and "--pids-limit" in args and "--memory" in args and not guest.workspace_mounted

    docker, _ = await run_container(tmp_path, DEFAULT)  # for contrast: the owner's container mounts both
    [args] = docker.started()
    assert sum(arg == "-v" for arg in args) == 2
    assert any(arg.endswith(":/home/gem/.config/browser") for arg in args) and any(arg.endswith(":/workspace") for arg in args)


async def test_a_guest_task_has_no_saved_logins_no_secrets_and_no_handoff_in_code(make_app):
    app = guest_app(make_app, call("saved_logins", site="example.org"), call("fill_saved_login", login_id=1), call("handoff", reason="log in"),
                    call("finish", result="Could not log in."), SAY)
    await approve(app)
    await app.desktop_permissions.save_login(site="example.org", label="x", username="u", password="p")
    task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    await finished(app, task_id)
    replies = [message["content"] for message in app.client.requests[3][0] if message["role"] == "tool"]
    assert len(replies) == 3 and all("Tool error" in text and "example.org" not in text for text in replies)  # none ran

    # even if a tool were ever wired in, the owner-task wrappers refuse a guest run
    worker = app.desktop_worker.lanes[GUEST].worker
    run = Run(1, OWNER, "t", "p", "b", frozenset(), 5, 1e12, step=None, tool=None, guest=True, usage_chat=GROUP)
    for name in ("saved_logins", "fill_saved_login", "upload_file", "handoff"):
        assert (await worker._owner_tool(run, None, name, {"login_id": 1}))[:8] == "Blocked:"
    assert (await worker._fill_saved_login(run, None, {"login_id": 1}))[:8] == "Blocked:"
    assert "owner" in await request_secret(app, ask(app))


async def request_secret(app, ctx):
    from dom.tools import REQUEST_SECRET_TOOL

    return await REQUEST_SECRET_TOOL.handler({"label": "x", "reason": "y"}, ctx)


async def test_a_guest_browser_reads_only_public_pages_with_links_and_search(make_app):
    app = guest_app(make_app)
    worker = app.desktop_worker.guest_lane().worker
    box, own = Container(app.desktop_worker.hosts[0], "x"), OwnRun(None)
    for url in ("http://192.0.2.7/", "https://router.local/", "file:///etc/passwd", "https://a.example.org:8443/", "https://u@evil.example/"):
        assert (await worker._own_tool(own, box, "browse", {"url": url}))[:8] == "Blocked:"
    assert await worker._own_tool(own, box, "browse", {"url": "https://garden.example.org/hours"}) == {}
    assert (await worker._own_tool(own, box, "fill", {"selector": "#password", "text": "x"}))[:8] == "Blocked:"
    assert (await worker._own_tool(own, box, "click", {"selector_or_index": "button"}))[:8] == "Blocked:"
    box.url = "http://10.0.0.1/"  # a page that redirected itself off the public web is closed before it is read
    box.navigated.clear()
    await worker._own_tool(own, box, "read_page", {})
    assert box.navigated == ["about:blank"]


async def test_a_never_site_rule_still_applies_to_a_guest_task(make_app):
    app = guest_app(make_app)
    await app.desktop_permissions.set_site_rule("blocked.example.org", "visit_site", "never")
    worker, box = app.desktop_worker.guest_lane().worker, Container(app.desktop_worker.hosts[0], "x")
    assert "never opens" in await worker._own_tool(OwnRun(None), box, "browse", {"url": "https://blocked.example.org/"})


async def test_no_files_from_the_task_leave_it(make_app):
    app = guest_app(make_app, call("save_screenshot"), call("finish", result="Open 9 to 6.", files=["/home/gem/outbox/a.png"]), SAY)
    await approve(app)
    task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    await finished(app, task_id)
    await settled(app)
    assert not app.bot.of("document") and not any(call[0] in ("photo", "document") for call in app.bot.calls)
    assert "Tool error" in app.client.requests[1][0][-1]["content"]  # no screenshots are offered to a guest task


# ---- where it runs, and one at a time ------------------------------------------------------------------------------------


async def test_a_host_kept_for_guests_runs_guest_tasks_first_and_never_the_owners(make_app):
    app = guest_app(make_app, hosts=[PI, STUDIO])
    fleet = app.desktop_worker
    assert fleet.guest_lane().name == "studio" and fleet.default == DEFAULT
    for container in ("studio", GUEST):
        with pytest.raises(ValueError, match="kept for tasks that groups and friends ask for"):
            await fleet.enqueue(OWNER, "look at my account", container)
    assert fleet.lane().name == DEFAULT and not fleet.for_guests(fleet.lane())

    only = guest_app(make_app, hosts=[STUDIO])  # a guest host alone is never the owner's default
    assert only.desktop_worker.default == DEFAULT and only.desktop_worker.guest_lane().name == "studio"
    with pytest.raises(ValueError):
        await only.desktop_worker.enqueue(OWNER, "x")


async def test_without_a_guest_host_the_local_host_runs_a_guest_container_of_its_own(make_app):
    app = guest_app(make_app)
    assert app.desktop_worker.guest_lane().name == GUEST and app.desktop_worker.has_guest_host()
    remote = guest_app(make_app, hosts=[DesktopHost(name="lab", mode="remote", url="https://lab.example.org", remote_isolation_confirmed=True)])
    assert remote.desktop_worker.guest_lane() is None and not remote.desktop_worker.has_guest_host()
    with pytest.raises(ValueError, match="no desktop host"):
        await remote.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")


def test_only_a_remote_host_can_be_kept_for_guests():
    with pytest.raises(ValueError, match="only a remote host"):
        DesktopHost(name="pi", mode="local", guest=True)


async def test_a_guest_task_queues_behind_an_owner_task_and_runs_when_it_ends(make_app):
    app = guest_app(make_app, call("finish", result="Open 9 to 6."), SAY)
    await approve(app)
    fleet = app.desktop_worker
    owner_lane = fleet.lane()
    await owner_lane.sandbox.ensure_running()
    owner_lane.worker._job = asyncio.ensure_future(asyncio.sleep(30))  # the owner's task is working

    task_id = await fleet.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    await asyncio.sleep(0.1)
    assert (await finished(app, task_id, "queued"))["status"] == "queued"  # waiting, not failed and not refused
    assert app.client.requests == []
    with pytest.raises(ValueError, match="on a task for someone else"):
        await fleet.enqueue_guest("another", FRIEND, "A friend", "Sam")  # one guest task at a time

    owner_lane.worker._job.cancel()
    owner_lane.sandbox.state = "stopped"
    row = await finished(app, task_id)
    assert row["status"] == "done" and row["result"] == "Open 9 to 6."


async def test_a_guest_task_stops_an_idle_owner_container_instead_of_waiting_out_its_timer(make_app):
    app = guest_app(make_app, call("finish", result="Open 9 to 6."), SAY)
    await approve(app)
    fleet = app.desktop_worker
    owner_lane = fleet.lane()
    await owner_lane.sandbox.ensure_running()  # ready, with no task
    task_id = await fleet.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    assert (await finished(app, task_id))["status"] == "done"
    assert owner_lane.sandbox.stops == 1  # its logins stay in its profile on disk


async def test_an_owner_task_is_refused_while_a_guest_task_has_the_desktop(make_app):
    app = guest_app(make_app, lambda: asyncio.sleep(30))
    await approve(app)
    fleet = app.desktop_worker
    task_id = await fleet.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    async with asyncio.timeout(2):
        while app.boxes.get(GUEST) is None or app.boxes[GUEST].status() != "ready":
            await asyncio.sleep(0.001)
    with pytest.raises(ValueError, match="task for a chat"):
        await fleet.enqueue(OWNER, "my own errand")
    assert await fleet.cancel(OWNER, task_id)
    assert (await finished(app, task_id, "cancelled"))["status"] == "cancelled"


async def test_a_guest_task_gives_up_when_the_desktop_never_frees_up(make_app):
    app = guest_app(make_app, SAY)
    await approve(app)
    app.config = app.config.model_copy(update={"desktop": app.config.desktop.model_copy(
        update={"guest": GuestDesktopConfig(queue_minutes=0.01)})})
    fleet = app.desktop_worker
    owner_lane = fleet.lane()
    await owner_lane.sandbox.ensure_running()
    owner_lane.worker._job = asyncio.ensure_future(asyncio.sleep(30))
    task_id = await fleet.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    row = await finished(app, task_id, "failed")
    assert "the desktop stayed busy" in row["error"]
    assert await settled(app) == ["It opens at 9, closes at 6."]  # the chat hears a kind failure, in his voice
    owner_lane.worker._job.cancel()


# ---- the memory floor ----------------------------------------------------------------------------------------------------


async def test_the_free_memory_floor_holds_a_guest_task_back_until_there_is_room(make_app):
    free = [1000]
    app = guest_app(make_app, call("finish", result="Open 9 to 6."), SAY)
    await approve(app)
    app.desktop_worker.hosts[0].free_memory = lambda: free[0]
    lane = app.desktop_worker.guest_lane()
    reason = await lane.worker.guest_admit()
    assert "not enough free memory on host default: 1000 MB free, 2112 MB needed" in reason  # 1600 + the 512 floor
    task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    await asyncio.sleep(0.1)
    assert (await finished(app, task_id, "queued"))["status"] == "queued" and app.client.requests == []
    free[0] = 4000
    assert (await finished(app, task_id))["status"] == "done"


async def test_the_pi_runs_one_desktop_at_a_time():
    host = Container(__import__("dom.desktop.sandbox", fromlist=["Host"]).Host(PI, free_memory=lambda: 8000), "a")
    other = Container(host.host, GUEST)
    await host.ensure_running()
    with pytest.raises(Refused, match="Tasks for a chat run only while no other desktop container is running"):
        await other.ensure_running()
    await host.stop()
    await other.ensure_running()
    assert "task for a chat" in await host.host.refusal(host)


# ---- the limits --------------------------------------------------------------------------------------------------------


async def test_the_guest_step_limit_is_enforced_and_a_partial_report_still_counts(make_app):
    app = guest_app(make_app, *[call("read_page")] * 5, call("finish", result="Only found the front page."), SAY, max_steps=5)
    await approve(app)
    task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    row = await finished(app, task_id)
    assert (row["steps"], row["error"], row["result"]) == (5, GUEST_PARTIAL, "Only found the front page.")
    assert await settled(app) == ["It opens at 9, closes at 6."]

    spent = guest_app(make_app, *[call("read_page")] * 5, SAY, max_steps=5)  # nothing from the wrap-up call: it fails
    await approve(spent)
    row = await finished(spent, await spent.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam"), "failed")
    assert "step limit" in row["error"]


async def test_spend_per_task_and_per_day_stops_the_next_model_call_and_counts_for_that_chat(make_app):
    app = guest_app(make_app, task_cost_cap_usd=0.25, daily_cost_cap_usd=0.4)
    await approve(app)
    await approve(app, FRIEND, "Pat")
    now = datetime.now(timezone.utc)
    started = (now - timedelta(minutes=1)).isoformat()
    assert await app.guest_desktop.pause(GROUP, started) is None
    await app.store.record_usage(now.isoformat(), "desktop", "m", "example.org", GROUP, 0, 0, 0.3, 0)
    assert await app.guest_desktop.pause(GROUP, started) == "task cost cap of $0.25 reached"
    assert await app.guest_desktop.pause(FRIEND, started) is None  # another chat's spend is its own
    later = (now + timedelta(minutes=1)).isoformat()  # a new task today: the day's budget is what is left of it
    assert await app.guest_desktop.pause(GROUP, later) is None
    await app.store.record_usage(now.isoformat(), "desktop", "m", "example.org", GROUP, 0, 0, 0.2, 0)
    assert await app.guest_desktop.pause(GROUP, later) == "this chat's desktop budget for today is used"
    assert "budget for today is used" in await request(app)  # and it is not even offered the owner's card
    assert app.bot.of("send", OWNER) == []
    await app.store.set_chat_auth_status(GROUP, "blocked")
    assert await app.guest_desktop.pause(GROUP, later) == "model calls are not allowed"


async def test_the_task_cost_cap_stops_a_running_guest_task_and_its_calls_are_kind_desktop_for_the_chat(make_app):
    app = guest_app(make_app, task_cost_cap_usd=0.15)
    await approve(app)

    def respond(request):
        body = json.loads(request.content)
        reply = call("read_page") if body.get("tools") else {"content": "invalid"}
        return httpx.Response(200, json={"choices": [{"message": reply}], "usage": {"cost": 0.1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = ChatClient(app.config.models["chat"], None, http, usage=UsageRecorder(app.store))
        task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
        row = await finished(app, task_id, "failed")
        await settled(app)
    assert row["error"] == "task cost cap of $0.15 reached"
    rows = await app.store._run(lambda: app.store._conn.execute("SELECT kind, chat_id FROM usage ORDER BY id").fetchall())
    assert {tuple(r) for r in rows} == {("desktop", GROUP)} and len(rows) >= 2  # two task calls, then the written reply


async def test_the_tool_and_the_worker_respect_the_area_spend_cap(make_app):
    app = guest_app(make_app)
    await approve(app)
    app.config = app.config.model_copy(update={"area_spend_caps_usd": {"desktop": 0.01}})
    await app.dispatch.usage.record("desktop", "mock", "example.org", {"cost": 0.5})
    assert await app.guest_desktop.pause(GROUP, datetime.now(timezone.utc).isoformat()) == "paused by a spend cap"


# ---- what goes back to the chat ------------------------------------------------------------------------------------------


async def test_the_answer_has_no_links_and_never_repeats_private_notes(make_app):
    linked = {"content": json.dumps({"bubbles": [{"text": "Open 9 to 6, see https://garden.example.org/hours or www.garden.example.org"}]})}
    app = guest_app(make_app, call("finish", result="x"), linked)
    await approve(app)
    await finished(app, await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam"))
    [text] = await settled(app)
    assert "http" not in text and "www" not in text and text.startswith("Open 9 to 6")

    private = app.home.root / "private"
    private.mkdir(exist_ok=True)
    (private / "NOTES.md").write_text("The owner keeps a safe behind the blue painting\n")
    leak = {"content": json.dumps({"bubbles": [{"text": "FYI the owner keeps a safe behind the blue painting, by the way"}]})}
    other = guest_app(make_app, call("finish", result="x"), leak)
    await approve(other)
    await finished(other, await other.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam"))
    assert await settled(other) == [guest_desktop.COULD_NOT]


async def test_a_failed_or_cancelled_task_tells_the_chat_nothing_about_why(make_app):
    app = guest_app(make_app, lambda: asyncio.sleep(0, result=None), SAY)  # the first model call fails
    await approve(app)
    task_id = await app.desktop_worker.enqueue_guest(BRIEF, GROUP, "Book club", "Sam")
    await finished(app, task_id, "failed")
    [text] = await settled(app)
    assert text == "It opens at 9, closes at 6."
    owner = " ".join(text for _, _, text, _ in app.bot.of("send", OWNER))
    assert f"Stopped: guest task #{task_id} for Book club" in owner  # the owner is told, with the reason


async def test_a_restart_fails_a_running_guest_task_and_the_chat_hears_it_did_not_work_out(make_app):
    app = guest_app(make_app, SAY)
    await approve(app)
    store = app.store
    task_id = await store.enqueue_desktop_task(OWNER, BRIEF, GUEST, guest=(GROUP, "Book club", "Sam"))
    await store.update_desktop_task(task_id, OWNER, ("queued",), "running")
    await app.desktop_worker.recover()
    row = await finished(app, task_id, "failed")
    assert row["error"] == "Dom restarted"
    assert await settled(app) == ["It opens at 9, closes at 6."]


async def test_the_dashboard_cannot_start_the_computer_kept_for_guests(make_app):
    from dom.web.desktop import run_computer_action

    app = guest_app(make_app)
    lane = app.desktop_worker.guest_lane()
    assert "kept for tasks that groups and friends ask for" in await run_computer_action(app, lane, "start")
    assert lane.status() == "stopped"
    owner = app.desktop_worker.lane()
    assert await run_computer_action(app, owner, "start") == "Computer ready."
