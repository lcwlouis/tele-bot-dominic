import asyncio
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
        page = await client.get("/desktop", headers=auth)
        assert page.status_code == 200
        assert "Dom needs you to log in: Sign in &lt;here&gt;" in page.text
        assert "Logins you make here are kept for later tasks." in page.text
        worker.handoff_host = "login.example.org"
        assert "Dom needs you to log in on login.example.org: Sign in &lt;here&gt;" in (await client.get("/desktop", headers=auth)).text
        assert "/desktop/vnc/index.html" in page.text
        done = f"/desktop/tasks/{task_id}/{row['steps']}/done"
        assert (await client.post(done, headers=POST_HEADERS)).status_code == 403
        assert (await client.post(done, headers=auth | {"HX-Request": "true"})).status_code == 403
        assert (await client.post(done, headers=auth | POST_HEADERS | {"Origin": "https://attacker.example.org"})).status_code == 403
        assert (await client.post(done, headers=auth | POST_HEADERS)).status_code == 200
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
            "/desktop/computer/start", headers={"Authorization": f"Bearer {TOKEN}"} | POST_HEADERS,
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


async def test_the_page_lists_every_container_and_shows_the_chosen_one(make_app, monkeypatch):
    app = make_app()
    fleet, computers = two_containers(app)
    computers["shop"].state = "ready"
    task = await app.store.enqueue_desktop_task(OWNER, "read the shop manuals", "shop")
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        main = (await client.get("/desktop", headers=auth)).text
        assert "Computer ready" in main and "No task running" in main and "read the shop manuals" not in main
        assert 'href="/desktop?container=shop"' in main and ">shop<" in main and "pi" in main
        shop = (await client.get("/desktop?container=shop", headers=auth)).text
        assert f"Task #{task}" in shop and "read the shop manuals" in shop
        assert 'hx-get="/desktop/state?container=shop"' in shop
        assert 'data-src="/desktop/vnc/index.html?container=shop"' in shop
        assert "<strong>shop</strong> · Computer ready" in shop
        assert (await client.get("/desktop?container=nope", headers=auth)).status_code == 404
        assert (await client.get("/desktop?container=Bad Name", headers=auth)).status_code == 404
        viewer = await client.get("/desktop/vnc/index.html?container=shop", headers=auth)
        assert "shop" in viewer.text and "<script>" not in viewer.text.replace('<script type="module" src', "")


async def test_start_and_done_act_on_the_chosen_container(make_app, monkeypatch):
    app = make_app()
    fleet, computers = two_containers(app)
    web = dashboard(app, monkeypatch)
    headers = {"Authorization": f"Bearer {TOKEN}"} | POST_HEADERS
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        assert (await client.post("/desktop/computer/start?container=shop", headers=headers)).status_code == 200
        assert (computers["shop"].started, computers["default"].started) == (1, 0)
        assert (await client.post("/desktop/computer/start?container=nope", headers=headers)).status_code == 404


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


# ---- home page desktop tile (W12) ----------------------------------------------------------------------

from unittest.mock import AsyncMock


async def test_home_desktop_tile_when_off_shows_muted_line_without_polling(make_app, monkeypatch):
    app = make_app()
    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        assert (await client.get("/desktop/tile")).status_code == 403
        home = (await client.get("/", headers=auth)).text
        assert 'id="desktop-tile"' in home
        assert "Desktop is off." in home
        assert 'hx-get="/desktop/tile"' not in home
        assert 'hx-trigger="every 4s"' not in home
        assert 'href="/desktop?container=' not in home
        tile = await client.get("/desktop/tile", headers=auth)
        assert tile.status_code == 200
        assert "<!doctype html>" not in tile.text.lower()
        assert "Desktop is off." in tile.text


async def test_home_desktop_tile_shows_containers_states_briefs_links_and_host_limits(make_app, monkeypatch):
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
        home = (await client.get("/", headers=auth)).text
        assert "<style" not in home and " style=" not in home
        assert 'id="desktop-tile"' in home
        assert 'hx-get="/desktop/tile"' in home
        assert 'hx-trigger="every 4s"' in home
        assert 'hx-swap="outerHTML"' in home
        assert 'hx-sync="this:drop"' in home

        tile = (await client.get("/desktop/tile", headers=auth)).text
        assert "<!doctype html>" not in tile.lower()
        assert "\u00b7" not in tile
        for html in (home, tile):
            # Container names and hosts
            assert "default" in html and "booting" in html and "full" in html and "studio" in html
            assert "pi" in html
            # States
            assert "running" in html
            assert "starting" in html
            assert "waiting for you" in html
            assert "refused" in html
            # Truncated and autoescaped brief (full untruncated brief not dumped raw)
            assert "&lt;quarterly&gt;" in html
            assert "<quarterly>" not in html
            assert long_brief not in html
            assert "…" in html
            assert "Sign in to the supplier portal" in html
            # Open links to the viewer
            assert 'href="/desktop"' in html
            assert 'href="/desktop?container=booting"' in html
            assert 'href="/desktop?container=full"' in html
            assert 'href="/desktop?container=studio"' in html
            assert "Open" in html
            # Host limits in small text
            assert "pi:" in html and "2/2" in html and "512 MB" in html
            assert "studio:" in html and "1/1" in html and "16 GB" in html

        # State change reflected on next htmx poll
        computers["booting"].state = "stopped"
        computers["booting"].error = None
        computers["default"].state = "stopped"
        polled = (await client.get("/desktop/tile", headers=auth)).text
        assert "off" in polled
        assert "0/2" in polled


async def test_home_and_desktop_pages_share_one_state_function(make_app, monkeypatch):
    from dom.web import desktop as web_desktop

    app = make_app()
    two_containers(app)
    spy = AsyncMock(wraps=web_desktop.desktop_context)
    monkeypatch.setattr(web_desktop, "desktop_context", spy)

    web = dashboard(app, monkeypatch)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        assert (await client.get("/", headers=auth)).status_code == 200
        assert (await client.get("/desktop", headers=auth)).status_code == 200
        assert (await client.get("/desktop/state", headers=auth)).status_code == 200
        assert (await client.get("/desktop/tile", headers=auth)).status_code == 200
    assert spy.await_count == 4


async def test_home_desktop_tile_handles_zero_floor_multiline_brief_error_and_unclaimed(make_app, monkeypatch):
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
        tile = (await client.get("/desktop/tile", headers=auth)).text
        assert "error" in tile and "remote health check failed" in tile
        assert "studio:" in tile and "0/1" in tile and "free floor" not in tile

        computer.state = "ready"
        computer.error = None
        await app.store.enqueue_desktop_task(OWNER, "Line one\n\n  Line two", "studio")
        tile = (await client.get("/desktop/tile", headers=auth)).text
        assert "Line one Line two" in tile

        monkeypatch.setattr(app.store, "owner_id", None)
        assert (await client.get("/desktop/tile", headers=auth)).status_code == 409
