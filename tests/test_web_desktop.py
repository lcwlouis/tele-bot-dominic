import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from dom.config import WebConfig
from dom.desktop.client import SandboxClient
from dom.web.app import create_app
from dom.web.auth import Auth
from test_desktop_worker import call, wait_status, wait_task, worker_for
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
    app.sandbox = FakeSandbox()
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
        assert "/desktop/vnc/index.html" in page.text
        done = f"/desktop/tasks/{task_id}/{row['steps']}/done"
        assert (await client.post(done, headers=POST_HEADERS)).status_code == 403
        assert (await client.post(done, headers=auth | {"HX-Request": "true"})).status_code == 403
        assert (await client.post(done, headers=auth | POST_HEADERS | {"Origin": "https://attacker.example.org"})).status_code == 403
        assert (await client.post(done, headers=auth | POST_HEADERS)).status_code == 200
    await wait_task(worker)
    assert any("Resumed desktop task" in row.text for row in await app.store.get_recent_chat_window(str(OWNER), 10))
