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


async def test_http_proxy_forwards_key_and_strips_client_auth(make_app, monkeypatch):
    app = make_app()
    await worker_for(app)
    sent = []

    def upstream(request):
        sent.append(request)
        assert request.url == "https://example.org/vnc/index.html"
        assert request.headers["authorization"] == "Bearer sandbox-key"
        assert not {"cookie", "cf-access-jwt-assertion", "x-api-key"} & request.headers.keys()
        return httpx.Response(200, text="<html>noVNC</html>", headers={
            "content-type": "text/html", "set-cookie": "upstream=value", "x-upstream": "private",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        app.sandbox.client = SandboxClient("https://example.org", "sandbox-key", http)
        web = dashboard(app, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
            path = "/desktop/vnc/index.html"
            assert (await client.get(path)).status_code == 403
            headers = {"Authorization": f"Bearer {TOKEN}", "Cf-Access-Jwt-Assertion": "fake",
                       "Cookie": "secret=value", "X-API-Key": "client-key"}
            result = await client.get(path, headers=headers)
            assert result.status_code == 200
            assert result.text == "<html>noVNC</html>"
            assert result.headers["x-frame-options"] == "SAMEORIGIN"
            assert result.headers["cache-control"] == "no-store"
            assert "sandbox-key" not in str(result.headers)
            assert "set-cookie" not in result.headers
            assert "x-upstream" not in result.headers
            for blocked in ("/desktop/vnc/app/%2e%2e/index.html", "/desktop/vnc/health", "/desktop/vnc/app/%252e%252e/a.js"):
                assert (await client.get(blocked, headers=headers)).status_code == 404
    assert len(sent) == 1


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
