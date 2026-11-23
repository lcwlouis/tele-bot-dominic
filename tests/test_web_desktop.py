import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from dom.config import WebConfig
from dom.desktop.client import SandboxClient
from dom.web.app import create_app
from dom.web.auth import Auth
from dom.desktop.worker import DesktopWorker
from test_desktop_worker import adopt, call, wait_status, wait_task, worker_for
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN, access_keys, assertion
from tghelpers import OWNER


def dashboard(app, monkeypatch):
    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)
    app.config = app.config.model_copy(update={"web": WebConfig(dev_token_env="DOM_TEST_TOKEN")})
    return create_app(app, Auth(app.config))


async def test_viewer_is_local_and_nothing_sandbox_is_served(make_app, monkeypatch):
    app = make_app()
    await worker_for(app)
    sent = []

    def upstream(request):
        sent.append(request)
        return httpx.Response(200, text="<script>steal()</script>", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        app.sandbox.client = SandboxClient("https://example.org", "sandbox-key", http)
        web = dashboard(app, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            path = "/desktop/vnc/index.html"
            assert (await client.get(path)).status_code == 403
            page = await client.get(path, headers=auth)
            assert page.status_code == 200
            assert "steal" not in page.text
            assert 'src="/static/desktop-viewer.js"' in page.text
            assert "<script>" not in page.text.replace('<script type="module" src', "")
            assert page.headers["x-frame-options"] == "SAMEORIGIN"
            assert page.headers["cache-control"] == "no-store"
            csp = page.headers["content-security-policy"]
            assert "default-src 'self'" in csp and "unsafe" not in csp
            assert "sandbox-key" not in str(page.headers)
            for other in ("app/ui.js", "core/rfb.js", "health", "app/%2e%2e/index.html", "vendor/pako/x.js"):
                assert (await client.get(f"/desktop/vnc/{other}", headers=auth)).status_code == 404
            assert (await client.head(path, headers=auth)).status_code in (200, 405)
            for asset in ("core/rfb.js", "vendor/pako/lib/zlib/inflate.js", "LICENSE.txt"):
                served = await client.get(f"/static/novnc/{asset}", headers=auth)
                assert served.status_code == 200, asset
            js = await client.get("/static/novnc/core/rfb.js", headers=auth)
            assert "javascript" in js.headers["content-type"]
            viewer = await client.get("/static/desktop-viewer.js", headers=auth)
            assert "/static/novnc/core/rfb.js" in viewer.text
    assert sent == []
    for route in web.routes:
        assert "{asset" not in getattr(route, "path", "")
        assert "GET" not in (getattr(route, "methods", None) or ()) or route.path != "/desktop/vnc/{asset:path}"


@pytest.mark.parametrize("origin", [ORIGIN, "https://attacker.example.org", None])
@pytest.mark.parametrize("authorized", [True, False])
async def test_websocket_auth_origin_and_relay(make_app, access_keys, monkeypatch, origin, authorized):
    app = make_app()
    (key, jwk), _ = access_keys

    async def keys(url):
        return {"keys": [jwk]}

    app.config = app.config.model_copy(update={"web": WebConfig(access_team="example-team", access_aud="example-audience")})
    adopt(app, DesktopWorker(app, FakeSandbox()))
    web = create_app(app, Auth(app.config, keys))
    calls = []

    @asynccontextmanager
    async def connect(url, *, additional_headers, subprotocols, **kwargs):
        calls.append((url, additional_headers, subprotocols))
        yield FakeUpstream()

    monkeypatch.setattr("dom.web.desktop.connect_upstream", connect)
    headers = assertion(key, "first") if authorized else {}
    headers.update({"X-Forwarded-Proto": "https", "Referer": ORIGIN, "Cookie": "secret=value"})
    if origin is not None:
        headers["Origin"] = origin
    headers.update({"Host": httpx.URL(ORIGIN).host})
    incoming = asyncio.Queue()
    outgoing = asyncio.Queue()
    path = "/desktop/vnc/websockify"
    scope = {
        "type": "websocket", "scheme": "ws", "path": path, "root_path": "",
        "query_string": b"", "subprotocols": ["binary"],
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    }
    await incoming.put({"type": "websocket.connect"})
    async with asyncio.timeout(2):
        session = asyncio.create_task(web(scope, incoming.get, outgoing.put))
        try:
            first = await outgoing.get()
            if authorized and origin == ORIGIN:
                assert first["type"] == "websocket.accept"
                assert first["subprotocol"] == "binary"
                await incoming.put({"type": "websocket.receive", "bytes": b"hello"})
                assert await outgoing.get() == {"type": "websocket.send", "bytes": b"echo:hello"}
                await incoming.put({"type": "websocket.disconnect", "code": 1000})
                assert calls == [("wss://example.org/websockify", {"Authorization": "Bearer sandbox-key"}, ["binary"])]
            else:
                assert first["type"] == "websocket.close"
                assert first["code"] == 1008
                assert calls == []
            await session
        finally:
            if not session.done():
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)


class FakeSandbox:
    def __init__(self):
        self.client = SandboxClient("https://example.org", "sandbox-key", None)

    def status(self):
        return "ready"

    def touch(self):
        pass


class FakeUpstream:
    subprotocol = "binary"

    def __init__(self):
        self.frames = asyncio.Queue()

    async def send(self, data):
        await self.frames.put(b"echo:" + data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.frames.get()


async def test_dashboard_done_resumes_task_with_csrf(make_app, monkeypatch):
    app = make_app()
    worker = await worker_for(
        app, call("handoff", reason="Sign in <here>"), call("finish", result="Done"), {"content": "invalid"},
    )
    web = dashboard(app, monkeypatch)
    task_id = await worker.enqueue(OWNER, "Example")
    row = await wait_status(app, task_id, "waiting_owner")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        desk = (await client.get("/api/work/desk", headers=auth)).json()
        assert desk["status"] == "ready" and desk["task"]["status"] == "waiting_owner"
        assert desk["task"]["error"] == "Sign in <here>" and desk["handoff_host"] is None
        assert desk["viewer"] == "/desktop/vnc/index.html"
        worker.handoff_host = "login.example.org"
        assert (await client.get("/api/work/desk", headers=auth)).json()["handoff_host"] == "login.example.org"
        done, body = "/api/work/desk/done", {"task_id": task_id, "steps": row["steps"]}
        assert (await client.post(done, json=body, headers=POST_HEADERS)).status_code == 403
        assert (await client.post(done, json=body, headers=auth)).status_code == 403
        assert (await client.post(done, json=body, headers=auth | {"HX-Request": "true"})).status_code == 403
        assert (await client.post(done, json=body, headers=auth | POST_HEADERS | {"Origin": "https://attacker.example.org"})).status_code == 403
        resumed = await client.post(done, json=body, headers=auth | POST_HEADERS)
        assert resumed.status_code == 200 and resumed.json()["message"] == "Task resumed."
    await wait_task(worker)
    assert any("Resumed desktop task" in row.text for row in await app.store.get_recent_chat_window(str(OWNER), 10))


async def test_start_does_not_hold_the_worker_lock_while_the_computer_boots(make_app, monkeypatch):
    app = make_app()
    worker = await worker_for(app)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def ensure_running():
        entered.set()
        await gate.wait()
        return app.sandbox

    app.sandbox.ensure_running = ensure_running
    web = dashboard(app, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        request = asyncio.create_task(client.post(
            "/api/work/desk/computer", json={"action": "start"}, headers={"Authorization": f"Bearer {TOKEN}"} | POST_HEADERS,
        ))
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(1):
            await worker.control.acquire()
        worker.control.release()
        gate.set()
        assert (await request).status_code == 200


async def test_runtime_error_in_the_websocket_relay_closes_cleanly(make_app, monkeypatch):
    app = make_app()
    adopt(app, DesktopWorker(app, FakeSandbox()))
    web = dashboard(app, monkeypatch)

    @asynccontextmanager
    async def connect(url, **kwargs):
        yield FakeUpstream()

    async def broken(websocket, upstream, sandbox):
        raise RuntimeError('Cannot call "send" once a close message has been sent.')

    monkeypatch.setattr("dom.web.desktop.connect_upstream", connect)
    monkeypatch.setattr("dom.web.desktop._from_upstream", broken)
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    scope = {
        "type": "websocket", "scheme": "ws", "path": "/desktop/vnc/websockify", "root_path": "", "query_string": b"",
        "subprotocols": ["binary"], "headers": [
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"origin", b"http://dashboard.example.org"), (b"host", b"dashboard.example.org"),
        ],
    }
    await incoming.put({"type": "websocket.connect"})
    async with asyncio.timeout(2):
        await web(scope, incoming.get, outgoing.put)
    sent = [await outgoing.get() for _ in range(outgoing.qsize())]
    assert sent[-1]["type"] == "websocket.close" and sent[-1]["code"] == 1011


# ---- several containers ---------------------------------------------------------------------------------

from dom.config import DesktopConfig, DesktopHost
from dom.desktop.fleet import DesktopFleet


class Computer(FakeSandbox):
    error = None

    def __init__(self, url, state="ready"):
        self.client = SandboxClient(url, "key-" + url, None)
        self.state = state
        self.started = 0

    def status(self):
        return self.state

    async def ensure_running(self):
        self.started += 1
        self.state = "ready"
        return self.client


def two_containers(app):
    app.config = app.config.model_copy(update={
        "desktop_mode": "local",
        "desktop": DesktopConfig(hosts=[DesktopHost(name="pi", mode="local", max_containers=2)]),
    })
    computers = {"default": Computer("https://main.example.org"), "shop": Computer("https://shop.example.org", "stopped")}
    fleet = DesktopFleet(app, sandbox_factory=lambda host, name: computers[name])
    fleet.lane("shop")
    app.desktop_worker = fleet
    return fleet, computers


async def test_the_desk_lists_every_container_and_shows_the_chosen_one(make_app, monkeypatch):
    app = make_app()
    fleet, computers = two_containers(app)
    computers["shop"].state = "ready"
    task = await app.store.enqueue_desktop_task(OWNER, "read the shop manuals", "shop")
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        main = (await client.get("/api/work/desk", headers=auth)).json()
        assert main["status"] == "ready" and main["task"] is None and main["container"] == "default"
        assert {item["name"]: item["selected"] for item in main["containers"]} == {"default": True, "shop": False}
        assert main["hosts"] == [{"name": "pi", "running": 2, "max_containers": 2, "memory_floor": "512 MB"}]
        shop = (await client.get("/api/work/desk?container=shop", headers=auth)).json()
        assert shop["container"] == "shop" and shop["task"]["id"] == task and shop["task"]["brief"] == "read the shop manuals"
        assert shop["viewer"] == "/desktop/vnc/index.html?container=shop"
        assert (await client.get("/api/work/desk?container=nope", headers=auth)).status_code == 404
        assert (await client.get("/api/work/desk?container=Bad Name", headers=auth)).status_code == 404
        viewer = await client.get("/desktop/vnc/index.html?container=shop", headers=auth)
        assert "shop" in viewer.text and "<script>" not in viewer.text.replace('<script type="module" src', "")
        assert 'data-back-url="/app/work?container=shop"' in viewer.text
        # A phone gets a keyboard from a real field, and a password manager can fill it in password mode.
        assert 'name="viewport"' in viewer.text
        assert 'id="type-text"' in viewer.text and 'autocomplete="username"' in viewer.text
        assert 'id="type-secret" type="checkbox"' in viewer.text


async def test_start_and_done_act_on_the_chosen_container(make_app, monkeypatch):
    app = make_app()
    fleet, computers = two_containers(app)
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}"} | POST_HEADERS
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        assert (await client.post("/api/work/desk/computer", json={"action": "start", "container": "shop"}, headers=headers)).status_code == 200
        assert (computers["shop"].started, computers["default"].started) == (1, 0)
        assert (await client.post("/api/work/desk/computer", json={"action": "start", "container": "nope"}, headers=headers)).status_code == 404


async def test_the_viewer_socket_relays_to_the_chosen_container(make_app, monkeypatch):
    app = make_app()
    fleet, computers = two_containers(app)
    computers["shop"].state = "ready"
    web = dashboard(app, monkeypatch)
    urls = []

    @asynccontextmanager
    async def connect(url, *, additional_headers, **kwargs):
        urls.append((url, additional_headers))
        yield FakeUpstream()

    monkeypatch.setattr("dom.web.desktop.connect_upstream", connect)

    async def open_socket(query):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        scope = {
            "type": "websocket", "scheme": "ws", "path": "/desktop/vnc/websockify", "root_path": "",
            "query_string": query, "subprotocols": ["binary"], "headers": [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"origin", b"http://dashboard.example.org"), (b"host", b"dashboard.example.org"),
            ],
        }
        await incoming.put({"type": "websocket.connect"})
        session = asyncio.create_task(web(scope, incoming.get, outgoing.put))
        first = await asyncio.wait_for(outgoing.get(), 2)
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(session, 2)
        return first

    assert (await open_socket(b"container=shop"))["type"] == "websocket.accept"
    assert urls == [("wss://shop.example.org/websockify", computers["shop"].client.auth_headers())]
    assert (await open_socket(b"container=nope"))["type"] == "websocket.close"  # unknown: nothing is relayed
    assert len(urls) == 1
    assert (await open_socket(b"container=default"))["type"] == "websocket.accept"
    assert urls[-1][0] == "wss://main.example.org/websockify"


# ---- the Desk's containers, states and host limits ----------------------------------------------------


async def test_the_desk_is_off_and_the_old_pages_and_partials_are_gone(make_app, monkeypatch):
    app = make_app()
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        assert (await client.get("/api/work/desk")).status_code == 403
        desk = (await client.get("/api/work/desk", headers=auth)).json()
        assert desk["enabled"] is False and desk["status"] == "off" and desk["containers"] == [] and desk["viewer"] == ""
        for gone in ("/desktop/tile", "/desktop/state", "/desktop/history"):
            assert (await client.get(gone, headers=auth)).status_code == 404
        for gone in ("/desktop/computer/start", "/desktop/tasks/1/delete", "/desktop/tasks/1/1/done"):
            assert (await client.post(gone, headers=auth | POST_HEADERS)).status_code == 403  # no longer a write the dashboard accepts


async def test_the_desk_shows_containers_states_briefs_and_host_limits(make_app, monkeypatch):
    app = make_app()
    app.config = app.config.model_copy(update={
        "desktop_mode": "local",
        "desktop": DesktopConfig(hosts=[
            DesktopHost(name="pi", mode="local", max_containers=2, min_free_memory_mb=512),
            DesktopHost(
                name="studio", mode="remote", url="https://studio.example.org",
                remote_isolation_confirmed=True, min_free_memory_mb=16384,
            ),
        ]),
    })
    computers = {
        "default": Computer("https://main.example.org", "ready"),
        "booting": Computer("https://boot.example.org", "starting"),
        "full": Computer("https://full.example.org", "stopped"),
        "studio": Computer("https://studio.example.org", "ready"),
    }
    computers["full"].error = "host pi already runs its limit of 2 desktop containers (default, booting)"
    fleet = DesktopFleet(app, sandbox_factory=lambda host, name: computers[name])
    for name in ("booting", "full", "studio"):
        fleet.lane(name)
    app.desktop_worker = fleet

    long_brief = "Check the <quarterly> report and export the summary table from every regional tab into a single spreadsheet for review"
    await app.store.enqueue_desktop_task(OWNER, long_brief, "default")
    handoff_id = await app.store.enqueue_desktop_task(OWNER, "Sign in to the supplier portal", "studio")
    await app.store.update_desktop_task(handoff_id, OWNER, ("queued",), "waiting_owner", error="Sign in required")

    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        desk = (await client.get("/api/work/desk", headers=auth)).json()
        items = {item["name"]: item for item in desk["containers"]}
        assert set(items) == {"default", "booting", "full", "studio"}
        assert items["default"]["host"] == "pi" and items["studio"]["host"] == "studio"
        assert items["default"]["state"] == "running" and items["booting"]["state"] == "starting"
        assert items["studio"]["state"] == "waiting for you" and items["full"]["state"] == "refused"
        # The brief is cut to one short line; the text is data, shown by the app as text.
        assert items["default"]["detail"].startswith("Check the <quarterly> report") and items["default"]["detail"].endswith("…")
        assert long_brief not in json.dumps(desk) or len(items["default"]["detail"]) < len(long_brief)
        assert items["studio"]["detail"] == "Sign in to the supplier portal"
        assert desk["hosts"] == [
            {"name": "pi", "running": 2, "max_containers": 2, "memory_floor": "512 MB"},
            {"name": "studio", "running": 1, "max_containers": 1, "memory_floor": "16 GB"},
        ]

        # A state change shows on the next read.
        computers["booting"].state = "stopped"
        computers["booting"].error = None
        computers["default"].state = "stopped"
        again = (await client.get("/api/work/desk", headers=auth)).json()
        assert {item["name"]: item["state"] for item in again["containers"]}["default"] == "off"
        assert again["hosts"][0]["running"] == 0


async def test_the_desk_handles_a_zero_floor_a_multiline_brief_an_error_and_an_unclaimed_dom(make_app, monkeypatch):
    app = make_app()
    app.config = app.config.model_copy(update={
        "desktop_mode": "remote",
        "desktop": DesktopConfig(hosts=[
            DesktopHost(
                name="studio", mode="remote", url="https://studio.example.org",
                remote_isolation_confirmed=True, min_free_memory_mb=0,
            ),
        ]),
    })
    computer = Computer("https://studio.example.org", "error")
    computer.error = "remote health check failed"
    fleet = DesktopFleet(app, sandbox_factory=lambda host, name: computer)
    app.desktop_worker = fleet
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        desk = (await client.get("/api/work/desk", headers=auth)).json()
        assert desk["status"] == "error" and desk["error"] == "remote health check failed"
        assert desk["hosts"] == [{"name": "studio", "running": 0, "max_containers": 1, "memory_floor": ""}]

        computer.state = "ready"
        computer.error = None
        await app.store.enqueue_desktop_task(OWNER, "Line one\n\n  Line two", "studio")
        desk = (await client.get("/api/work/desk", headers=auth)).json()
        assert desk["containers"][0]["detail"] == "Line one Line two"

        monkeypatch.setattr(app.store, "owner_id", None)
        assert (await client.get("/api/work/desk", headers=auth)).status_code == 409


# ---- phone remote-desktop viewer (v0.8.2 W3) ------------------------------------------------------------


class MockSandboxClient:
    def __init__(self, *, url="https://login.example.org/signin", password_result=False):
        self.url = url
        self.password_result = password_result
        self.resolutions = []
        self.clipboard_clears = 0
        self.password_calls = 0

    def redact(self, text):
        return text

    async def current_url(self):
        return self.url

    async def set_browser_resolution(self, resolution, *, timeout=10):
        self.resolutions.append((resolution, timeout))
        return {"resolution": resolution}

    async def is_password_focused(self, *, timeout=5):
        self.password_calls += 1
        return self.password_result

    async def clear_clipboard(self, *, timeout=5):
        self.clipboard_clears += 1


class ViewerSandbox:
    def __init__(self, client):
        self.client = client
        self._state = "ready"
        self.touches = 0
        self.stopped = 0

    def status(self):
        return self._state

    async def ensure_running(self):
        self._state = "ready"
        return self.client

    def in_use(self):
        from contextlib import nullcontext
        return nullcontext()

    def touch(self):
        self.touches += 1

    async def stop(self):
        self.stopped += 1
        self._state = "stopped"


class RemoteViewerSandbox:
    def __init__(self, client):
        self.client = client
        self._state = "ready"
        self.touches = 0

    def status(self):
        return self._state

    async def ensure_running(self):
        self._state = "ready"
        return self.client

    def in_use(self):
        from contextlib import nullcontext
        return nullcontext()

    def touch(self):
        self.touches += 1


async def test_viewer_routes_require_owner_auth_and_csrf(make_app, monkeypatch):
    app = make_app()
    mock_client = MockSandboxClient()
    sandbox = ViewerSandbox(mock_client)
    adopt(app, DesktopWorker(app, sandbox))
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    json_origin = {"Origin": ORIGIN}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        # Unauthenticated requests are rejected on every new route.
        for path in ("/desktop/viewer/status", "/desktop/viewer/password-focus"):
            assert (await client.get(path)).status_code == 403
        for path, body in (
            ("/desktop/viewer/screen", {"mode": "phone"}),
            ("/desktop/viewer/done", {"task_id": 1, "steps": 1}),
        ):
            assert (await client.post(path, json=body, headers=json_origin)).status_code == 403

        # Authenticated POSTs still enforce same-origin, Origin/HX-Request, JSON content-type, and method.
        for path, body in (
            ("/desktop/viewer/screen", {"mode": "phone"}),
            ("/desktop/viewer/done", {"task_id": 1, "steps": 1}),
        ):
            for bad_headers in (
                {},
                {"Origin": "https://attacker.example.org"},
                {"Referer": ORIGIN},
                {"HX-Request": "true"},
            ):
                assert (await client.post(path, json=body, headers=auth | bad_headers)).status_code == 403
            assert (await client.post(path, data={"mode": "phone"}, headers=auth | json_origin)).status_code == 403
            assert (await client.put(path, json=body, headers=auth | json_origin)).status_code == 403

        # Unknown container rejected with 404.
        assert (await client.get("/desktop/viewer/status?container=nope", headers=auth)).status_code == 404
        assert (await client.get("/desktop/viewer/password-focus?container=nope", headers=auth)).status_code == 404
        assert (await client.post("/desktop/viewer/screen?container=nope", json={"mode": "phone"}, headers=auth | json_origin)).status_code == 404

        # Unclaimed Dom rejected with 409 on every new route.
        monkeypatch.setattr(app.store, "owner_id", None)
        assert (await client.get("/desktop/viewer/status", headers=auth)).status_code == 409
        assert (await client.get("/desktop/viewer/password-focus", headers=auth)).status_code == 409
        assert (await client.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=auth | json_origin)).status_code == 409
        assert (await client.post("/desktop/viewer/done", json={"task_id": 1, "steps": 1}, headers=auth | json_origin)).status_code == 409


@pytest.mark.parametrize("payload,expected", [
    ({"success": True, "data": True}, True),
    ({"success": True, "data": {"result": True}}, True),
    ({"success": True, "data": {"value": True}}, True),
    ({"success": True, "data": False}, False),
    ({"success": True, "data": "true"}, False),
    ({"success": True, "data": 1}, False),
    ({"success": True, "data": {"result": "true", "extra": "<script>alert(1)</script>"}}, False),
    ({"success": True, "data": {"result": 1}}, False),
    ({"success": False, "message": "boom"}, False),
])
async def test_password_focus_endpoint_treats_sandbox_as_hostile_and_returns_only_boolean(
    make_app, monkeypatch, payload, expected,
):
    app = make_app()
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SandboxClient("https://sandbox.example.org", "secret-key", http)
        sandbox = ViewerSandbox(client)
        adopt(app, DesktopWorker(app, sandbox))
        web = dashboard(app, monkeypatch)
        auth = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
            resp = await browser.get("/desktop/viewer/password-focus", headers=auth)
            assert resp.status_code == 200
            data = resp.json()
            assert list(data.keys()) == ["password"]
            assert type(data["password"]) is bool
            assert data["password"] is expected
    assert len(seen) == 1
    assert seen[0][0] == "POST" and seen[0][1] == "/v1/browser/page/evaluate"


async def test_password_focus_rejects_oversized_compressed_or_timed_out_sandbox_response(make_app, monkeypatch):
    app = make_app()

    def oversized(request):
        return httpx.Response(200, content=b'{"success":true,"data":"' + b"x" * 5000 + b'"}')

    def compressed(request):
        return httpx.Response(200, content=b'{"success":true,"data":true}', headers={"content-encoding": "gzip"})

    for handler in (oversized, compressed):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SandboxClient("https://sandbox.example.org", "secret-key", http)
            adopt(app, DesktopWorker(app, ViewerSandbox(client)))
            web = dashboard(app, monkeypatch)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
                resp = await browser.get("/desktop/viewer/password-focus", headers={"Authorization": f"Bearer {TOKEN}"})
                assert resp.status_code == 200
                assert resp.json() == {"password": False}

    # Hanging sandbox evaluation is bounded by a timeout and fails closed to False.
    async def hang(*, timeout=5):
        await asyncio.sleep(10)
        return True

    mock_client = MockSandboxClient()
    mock_client.is_password_focused = hang
    adopt(app, DesktopWorker(app, ViewerSandbox(mock_client)))
    monkeypatch.setattr("dom.web.desktop.asyncio.timeout", lambda _: asyncio.timeout(0.05))
    web = dashboard(app, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        resp = await browser.get("/desktop/viewer/password-focus", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
        assert resp.json() == {"password": False}


async def test_phone_mode_resize_while_idle_and_refused_while_worker_active(make_app, monkeypatch):
    app = make_app()
    mock_client = MockSandboxClient()
    sandbox = ViewerSandbox(mock_client)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_model():
        entered.set()
        await release.wait()
        return call("finish", result="Done")

    worker = await worker_for(app, slow_model, {"content": "invalid"})
    worker.sandbox = sandbox
    app.desktop_worker.lanes[app.desktop_worker.default].sandbox = sandbox
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        # Idle container can switch to phone mode (800x600) and back to desktop (1280x1024).
        r1 = await browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers)
        assert r1.status_code == 200
        assert r1.json() == {"ok": True, "mode": "phone", "resolution": "800x600", "phone_mode": True}
        assert mock_client.resolutions == [("800x600", 10)]

        # Invalid payloads are rejected.
        assert (await browser.post("/desktop/viewer/screen", json={"mode": "portrait"}, headers=headers)).status_code == 422
        assert (await browser.post("/desktop/viewer/screen", json={"mode": "phone", "extra": 1}, headers=headers)).status_code == 422

        # Starting a new task automatically restores 1280x1024 before the worker runs, and blocks resize while running.
        await worker.enqueue(OWNER, "Check site")
        await asyncio.wait_for(entered.wait(), 2)
        assert mock_client.resolutions[-1] == ("1280x1024", 10)
        assert worker.screen_resolution == "1280x1024"

        blocked = await browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers)
        assert blocked.status_code == 409

        release.set()
        await wait_task(worker)

        # Idle again: can switch to phone mode and back to desktop.
        assert (await browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers)).status_code == 200
        r2 = await browser.post("/desktop/viewer/screen", json={"mode": "desktop"}, headers=headers)
        assert r2.status_code == 200
        assert r2.json() == {"ok": True, "mode": "desktop", "resolution": "1280x1024", "phone_mode": False}


@pytest.mark.parametrize("end_by", ["viewer_done", "timeout", "crash"])
async def test_phone_mode_restores_1280x1024_and_clears_clipboard_on_done_timeout_or_crash(
    make_app, monkeypatch, end_by,
):
    app = make_app()
    mock_client = MockSandboxClient(url="https://accounts.example.org/login")
    sandbox = ViewerSandbox(mock_client)
    replies = [call("handoff", reason="Sign in please")]
    if end_by == "viewer_done":
        replies.extend([call("finish", result="Signed in"), {"content": "invalid"}])
    else:
        replies.append({"content": "invalid"})
    worker = await worker_for(app, *replies, handoff_seconds=2 if end_by != "timeout" else 1.0)
    worker.sandbox = sandbox
    worker._needs_screen_restore = False
    app.desktop_worker.lanes[app.desktop_worker.default].sandbox = sandbox
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    headers = auth | {"Origin": ORIGIN}

    task_id = await worker.enqueue(OWNER, "Sign in task")
    row = await wait_status(app, task_id, "waiting_owner")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        status = (await browser.get("/desktop/viewer/status", headers=auth)).json()
        assert status["live_host"] == "accounts.example.org"
        assert status["can_resize"] is True
        assert status["handoff"]["task_id"] == task_id
        assert status["handoff"]["steps"] == row["steps"]
        assert status["handoff"]["reason"] == "Sign in please"
        assert isinstance(status["handoff"]["remaining_seconds"], int)

        # Viewer HTML contains Done, host, countdown, key bar, clipboard buttons, phone mode, no inline scripts/styles.
        viewer = await browser.get("/desktop/vnc/index.html", headers=auth)
        assert viewer.status_code == 200
        assert 'id="done-btn"' in viewer.text
        assert 'id="countdown"' in viewer.text
        assert 'id="phone-mode-btn"' in viewer.text
        assert 'id="copy-btn"' in viewer.text and 'id="paste-btn"' in viewer.text
        assert 'id="mode-scroll"' in viewer.text
        assert 'id="keybar"' in viewer.text
        assert "<style" not in viewer.text and " style=" not in viewer.text
        assert "<script>" not in viewer.text.replace('<script type="module" src', "")

        # Enable phone mode during the handoff.
        resized = await browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers)
        assert resized.status_code == 200
        assert worker.screen_resolution == "800x600"

        if end_by == "viewer_done":
            done = await browser.post(
                "/desktop/viewer/done",
                json={"task_id": task_id, "steps": row["steps"]},
                headers=headers,
            )
            assert done.status_code == 200
            assert done.json()["ok"] is True
        elif end_by == "crash":
            # Simulate an unexpected exception inside _wait_for_owner while phone mode is active.
            async def boom(*args, **kwargs):
                raise RuntimeError("unexpected crash")

            worker._job.cancel()
            await asyncio.gather(worker._job, return_exceptions=True)
            await app.store.update_desktop_task(task_id, OWNER, ("waiting_owner",), "running")
            worker.screen_resolution = "800x600"
            worker._needs_screen_restore = True
            monkeypatch.setattr(app.dispatch, "desktop_handoff", boom)
            with pytest.raises(RuntimeError, match="unexpected crash"):
                await worker._wait_for_owner(task_id, OWNER, row["steps"], "Sign in", "accounts.example.org", app.dispatch.now())

    if end_by != "crash":
        await wait_task(worker)

    assert worker.screen_resolution == "1280x1024"
    assert mock_client.resolutions == [("800x600", 10), ("1280x1024", 10)]
    assert mock_client.clipboard_clears >= 1


async def test_viewer_screen_resize_does_not_hold_worker_lock(make_app, monkeypatch):
    app = make_app()
    mock_client = MockSandboxClient()
    entered, gate = asyncio.Event(), asyncio.Event()

    async def slow_resize(resolution, *, timeout=10):
        entered.set()
        await gate.wait()
        mock_client.resolutions.append((resolution, timeout))
        return {"resolution": resolution}

    mock_client.set_browser_resolution = slow_resize
    sandbox = ViewerSandbox(mock_client)
    worker = adopt(app, DesktopWorker(app, sandbox))
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        req = asyncio.create_task(browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers))
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(1):
            await worker.control.acquire()
        worker.control.release()
        gate.set()
        resp = await req
        assert resp.status_code == 200


async def test_remote_sandbox_crash_recovery_restores_resolution_and_clears_clipboard(make_app):
    app = make_app()
    mock_client = MockSandboxClient()
    sandbox = RemoteViewerSandbox(mock_client)
    worker = adopt(app, DesktopWorker(app, sandbox))
    task_id = await app.store.enqueue_desktop_task(OWNER, "Remote login", "default")
    await app.store.update_desktop_task(task_id, OWNER, ("queued",), "waiting_owner", error="Sign in")
    await worker.recover()
    assert mock_client.resolutions == [("1280x1024", 10)]
    assert mock_client.clipboard_clears == 1


async def test_sandbox_client_resolution_and_clipboard_api_calls(caplog):
    from dom.desktop.client import SandboxError

    seen = []
    exit_code = 0
    stderr = ""

    def handler(request):
        body = request.read().decode()
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/v1/bash/exec":
            return httpx.Response(200, json={"success": True, "data": {"exit_code": exit_code, "stdout": "", "stderr": stderr}})
        return httpx.Response(200, json={"success": True, "data": {"ok": True}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SandboxClient("https://sandbox.example.org", "secret-key", http)
        await client.set_browser_resolution("800x600", timeout=5)
        await client.set_browser_resolution("1280x1024", timeout=5)
        await client.clear_clipboard(timeout=5)
        with pytest.raises(ValueError, match="unsupported"):
            await client.set_browser_resolution("390x844")

        # Non-zero exit code (e.g. missing tool or no X11 display) must raise SandboxError, not be swallowed.
        exit_code = 127
        stderr = "neither xsel nor xclip is installed"
        with pytest.raises(SandboxError, match="clipboard clear failed"):
            await client.clear_clipboard(timeout=5)

    assert seen[0] == ("POST", "/v1/browser/config", '{"resolution":{"width":800,"height":600}}')
    assert seen[1] == ("POST", "/v1/browser/config", '{"resolution":{"width":1280,"height":1024}}')
    assert seen[2][0] == "POST" and seen[2][1] == "/v1/bash/exec"
    cmd = seen[2][2]
    assert "xsel -bc" in cmd and "xsel -pc" in cmd
    assert "xsel -bp" not in cmd and "|| true" not in cmd and ":90" not in cmd
    assert "/tmp/.X11-unix/X*" in cmd


async def test_resize_race_handoff_ends_while_resize_in_flight(make_app, monkeypatch):
    app = make_app()
    mock_client = MockSandboxClient(url="https://accounts.example.org/login")
    in_phone_resize, release_phone_resize = asyncio.Event(), asyncio.Event()

    async def gated_resize(resolution, *, timeout=10):
        mock_client.resolutions.append((resolution, timeout))
        if resolution == "800x600":
            in_phone_resize.set()
            await release_phone_resize.wait()
        return {"resolution": resolution}

    mock_client.set_browser_resolution = gated_resize
    sandbox = ViewerSandbox(mock_client)
    worker = await worker_for(
        app, call("handoff", reason="Sign in"), call("finish", result="Done"), {"content": "invalid"},
    )
    worker.sandbox = sandbox
    worker._needs_screen_restore = False
    app.desktop_worker.lanes[app.desktop_worker.default].sandbox = sandbox
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}

    task_id = await worker.enqueue(OWNER, "Race test")
    row = await wait_status(app, task_id, "waiting_owner")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        resize_req = asyncio.create_task(browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers))
        await asyncio.wait_for(in_phone_resize.wait(), 2)

        # Owner finishes handoff while phone-mode resize is still in flight; resume must not block on _screen_lock.
        done = await asyncio.wait_for(
            browser.post("/desktop/viewer/done", json={"task_id": task_id, "steps": row["steps"]}, headers=headers),
            timeout=2,
        )
        assert done.status_code == 200 and done.json()["ok"] is True

        release_phone_resize.set()
        resize_resp = await resize_req
        assert resize_resp.status_code == 409

    await wait_task(worker)
    assert worker.screen_resolution == "1280x1024"
    assert mock_client.resolutions == [("800x600", 10), ("1280x1024", 10)]


async def test_resize_race_task_starts_while_idle_resize_in_flight(make_app, monkeypatch):
    app = make_app()
    mock_client = MockSandboxClient()
    in_phone_resize, release_phone_resize = asyncio.Event(), asyncio.Event()

    async def gated_resize(resolution, *, timeout=10):
        mock_client.resolutions.append((resolution, timeout))
        if resolution == "800x600":
            in_phone_resize.set()
            await release_phone_resize.wait()
        return {"resolution": resolution}

    mock_client.set_browser_resolution = gated_resize
    sandbox = ViewerSandbox(mock_client)
    worker = await worker_for(app, call("finish", result="Done"), {"content": "invalid"})
    worker.sandbox = sandbox
    worker._needs_screen_restore = False
    app.desktop_worker.lanes[app.desktop_worker.default].sandbox = sandbox
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        resize_req = asyncio.create_task(browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers))
        await asyncio.wait_for(in_phone_resize.wait(), 2)

        # A new task starts while the idle phone-mode resize is still in flight.
        await asyncio.wait_for(worker.enqueue(OWNER, "Run task"), timeout=2)
        await asyncio.sleep(0.02)

        release_phone_resize.set()
        resize_resp = await resize_req
        assert resize_resp.status_code == 409

    await wait_task(worker)
    assert worker.screen_resolution == "1280x1024"
    assert mock_client.resolutions == [("800x600", 10), ("1280x1024", 10)]


async def test_concurrent_screen_toggles_are_serialized(make_app, monkeypatch):
    app = make_app()
    mock_client = MockSandboxClient()
    active = 0
    max_active = 0
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    order = []

    async def slow_resize(resolution, *, timeout=10):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(("start", resolution))
        if resolution == "800x600":
            first_entered.set()
            await release_first.wait()
        order.append(("end", resolution))
        active -= 1
        return {"resolution": resolution}

    mock_client.set_browser_resolution = slow_resize
    sandbox = ViewerSandbox(mock_client)
    worker = adopt(app, DesktopWorker(app, sandbox))
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as browser:
        r1 = asyncio.create_task(browser.post("/desktop/viewer/screen", json={"mode": "phone"}, headers=headers))
        await asyncio.wait_for(first_entered.wait(), 2)
        r2 = asyncio.create_task(browser.post("/desktop/viewer/screen", json={"mode": "desktop"}, headers=headers))
        await asyncio.sleep(0.02)
        # Second toggle must wait for first; worker.control must remain unlocked.
        assert max_active == 1
        async with asyncio.timeout(1):
            await worker.control.acquire()
        worker.control.release()

        release_first.set()
        resp1, resp2 = await asyncio.gather(r1, r2)
        assert resp1.status_code == 200 and resp2.status_code == 200

    assert max_active == 1
    assert order == [("start", "800x600"), ("end", "800x600"), ("start", "1280x1024"), ("end", "1280x1024")]
    assert worker.screen_resolution == "1280x1024"


async def test_local_sandbox_idle_stop_resets_worker_screen_state(make_app):
    from dom.desktop.sandbox import LocalSandbox

    app = make_app()
    app.config = app.config.model_copy(update={"desktop_mode": "local"})
    now = [100.0]

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"true\n", b""

    async def fake_run(*args, **kwargs):
        return FakeProc()

    sandbox = LocalSandbox(app.config, run=fake_run, clock=lambda: now[0])
    sandbox._status = "ready"
    sandbox.client = MockSandboxClient()
    fleet = DesktopFleet(app, sandbox_factory=lambda host, name: sandbox)
    worker = fleet.lane("default").worker
    await worker.set_screen_mode(True)
    assert worker.screen_resolution == "800x600"

    # Advance past idle timeout and trigger idle stop: worker.screen_reset() runs via on_stop.
    now[0] += app.config.desktop.idle_minutes * 60 + 1
    await sandbox.stop_if_idle()
    assert sandbox.status() == "stopped"
    assert worker.screen_resolution == "1280x1024"
    assert worker._needs_screen_restore is False
    await sandbox.close()





async def test_a_stop_during_a_phone_resize_leaves_the_viewer_on_the_desktop_size(make_app, monkeypatch):
    """E60: the container stops (idle timer or Stop) while a phone-mode resize is in flight. The resize must not write
    800x600 back over the reset, or the viewer keeps showing phone mode for a desktop that restarts at 1280x1024."""
    from dom.desktop.client import SandboxError
    from dom.desktop.worker import DesktopWorker, ScreenBusy

    app = make_app()
    mock_client = MockSandboxClient()
    in_resize, release = asyncio.Event(), asyncio.Event()

    async def gated_resize(resolution, *, timeout=10):
        if sandbox.status() == "stopped":
            raise SandboxError("desktop is not running")  # a stopped container cannot be resized back either
        mock_client.resolutions.append((resolution, timeout))
        in_resize.set()
        await release.wait()
        return {"resolution": resolution}

    mock_client.set_browser_resolution = gated_resize
    sandbox = ViewerSandbox(mock_client)
    worker = DesktopWorker(app, sandbox)
    worker._needs_screen_restore = False
    resize = asyncio.create_task(worker.set_screen_mode(True))
    await asyncio.wait_for(in_resize.wait(), 2)
    sandbox._state = "stopped"
    worker.screen_reset()  # what the sandbox's on_stop calls
    release.set()
    with pytest.raises(ScreenBusy):
        await resize
    assert worker.screen_resolution == "1280x1024"
