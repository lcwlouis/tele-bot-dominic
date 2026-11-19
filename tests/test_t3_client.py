import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from dom.t3.auth import connect
from dom.t3.client import T3Client, T3Unavailable

BASE = "http://127.0.0.1:39731"


def test_connect(tmp_path, capsys, caplog):
    def handle(request):
        if request.url.path.endswith("/register"):
            assert json.loads(request.content)["grant_types"] == ["authorization_code"]
            return httpx.Response(200, json={"client_id": "fixture"})
        if request.url.path.endswith("/decision"):
            body = json.loads(request.content)
            assert body["decision"] == {"_tag": "pairing-code", "access": "approval-required", "code": "fixture-pair"}
            assert body["authorization"]["code_challenge_method"] == "S256"
            auth.update(body["authorization"])
            return httpx.Response(200, json={"redirectTo": auth["redirect_uri"] + "?code=fixture-code&state=" + auth["state"]})
        body = parse_qs(request.content.decode())
        assert body["code"] == ["fixture-code"]
        assert body["resource"] == [BASE + "/mcp"]
        import base64
        import hashlib

        digest = hashlib.sha256(body["code_verifier"][0].encode()).digest()
        assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == auth["code_challenge"]
        return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 2592000})

    auth = {}
    path = tmp_path / "t3-token.json"

    def run(command, **kwargs):
        assert command == ["t3", "auth", "pairing", "create", "--base-dir", "fixture-dir", "--scope",
                                "orchestration:read", "--scope", "orchestration:operate", "--ttl", "5m",
                                "--label", "Dom", "--json"]
        return SimpleNamespace(stdout='{"credential":"fixture-pair"}')

    connect(BASE, path, ["t3"], "fixture-dir", transport=httpx.MockTransport(handle), runner=run)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["access_token"] == "fixture-token"
    assert "fixture-" not in repr(capsys.readouterr()) + caplog.text


def test_mcp_reuses_and_reopens_session(tmp_path):
    path = tmp_path / "token"
    path.write_text(json.dumps({"access_token": "fixture", "expires_at": time.time() + 3600}))
    methods = []

    def handle(request):
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        assert request.headers["authorization"] == "Bearer " + "fixture"
        if method == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "fixture"}, json={
                "result": {"protocolVersion": "2025-06-18"}})
        assert request.headers["mcp-session-id"] == "fixture"
        if method == "notifications/initialized":
            return httpx.Response(202)
        if len(methods) == 4:
            return httpx.Response(404)
        assert body["params"]["name"] == "t3_project_list"
        result = json.dumps({"result": {"structuredContent": {"projects": []}}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=f"data: {result}\n\n")

    client = T3Client(BASE, path, ["t3"], transport=httpx.MockTransport(handle))
    for name in ("t3_thread_send", "t3_other"):
        with pytest.raises(T3Unavailable):
            client.call(name, {})
    assert methods == []
    assert client.call("t3_project_list", {}) == {"projects": []}
    assert client.call("t3_project_list", {}) == {"projects": []}
    assert methods.count("initialize") == 2


def test_confirmed_send_uses_queue_and_audit_id(tmp_path):
    path = tmp_path / "token"
    path.write_text(json.dumps({"access_token": "fixture", "expires_at": time.time() + 3600}))
    thread = "9bd62f03-ebb5-4ec3-9cf3-ed7e3712012a"
    machine = "bcbfab34-8339-4399-85fa-763d1b882bcf"

    def handle(request):
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "fixture"}, json={
                "result": {"protocolVersion": "2025-06-18"}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert body["params"] == {"name": "t3_thread_send", "arguments": {
            "mode": "queue", "threadId": thread, "message": "Fixture text", "environmentId": machine,
            "clientRequestId": "dom-7",
        }}
        return httpx.Response(200, json={"result": {"structuredContent": {
            "_tag": "OrchestratorMcpFailure", "code": "capability_denied", "message": "Private details",
        }}})

    client = T3Client(BASE, path, ["t3"], transport=httpx.MockTransport(handle))
    with pytest.raises(T3Unavailable, match="tool failed"):
        client.confirmed_send(thread, "Fixture text", machine, 7)


def test_mcp_deadline_includes_handshake_and_reinitialization(tmp_path, monkeypatch):
    from dom.t3 import client as module

    path = tmp_path / "token"
    path.write_text(json.dumps({"access_token": "fixture", "expires_at": time.time() + 3600}))
    now = 0
    methods = []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=time.time, monotonic=lambda: now))

    def handle(request):
        nonlocal now
        timeout = request.extensions["timeout"]
        assert timeout["read"] == 20 - now
        assert timeout["connect"] == min(5, 20 - now)
        now += 6
        method = json.loads(request.content)["method"]
        methods.append(method)
        if method == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "fixture"}, json={
                "result": {"protocolVersion": "2025-06-18"}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(404)

    client = T3Client(BASE, path, ["t3"], transport=httpx.MockTransport(handle))
    with pytest.raises(T3Unavailable, match="time"):
        client.call("t3_project_list", {})
    assert methods == ["initialize", "notifications/initialized", "tools/call", "initialize"]
    assert client.session is None


@pytest.mark.parametrize("invalid", ["state", "redirect", "code"])
def test_connect_rejects_invalid_decisions_without_leaking(tmp_path, capsys, caplog, invalid):
    path = tmp_path / "token"

    def handle(request):
        if request.url.path.endswith("/register"):
            return httpx.Response(200, json={"client_id": "fixture-client"})
        assert request.url.path.endswith("/decision")
        auth = json.loads(request.content)["authorization"]
        redirect = auth["redirect_uri"] if invalid != "redirect" else BASE + "/wrong"
        state = auth["state"] if invalid != "state" else "fixture-state"
        code = "&code=fixture-code" if invalid != "code" else ""
        return httpx.Response(200, json={"redirectTo": f"{redirect}?state={state}{code}"})

    with pytest.raises(T3Unavailable, match=r"^T3 sign-in failed\.$") as caught:
        connect(BASE, path, ["t3"], "fixture-dir", transport=httpx.MockTransport(handle),
                runner=lambda *a, **kw: SimpleNamespace(stdout='{"credential":"fixture-pair"}'))
    assert not path.exists()
    assert "fixture-" not in repr(capsys.readouterr()) + caplog.text + str(caught.value)


@pytest.mark.parametrize("response", ["chunked", "oversize", "unauthorized", "timeout"])
def test_confirmed_send_responses(tmp_path, response):
    from dom.t3.client import LIMIT, T3Uncertain

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'event: message\r\nda'
            yield b'ta: {"result":{"structuredContent":{"accepted":true}}}\r'
            yield b'\n\r'
            yield b'\n'

    path = tmp_path / "token"
    path.write_text(json.dumps({"access_token": "fixture", "expires_at": time.time() + 3600}))

    def handle(request):
        assert json.loads(request.content)["params"]["arguments"]["clientRequestId"] == "dom-7"
        if response == "chunked":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        if response == "timeout":
            raise httpx.ReadTimeout("Private fixture detail")
        if response == "unauthorized":
            return httpx.Response(401)
        return httpx.Response(200, content=b"x" * (LIMIT + 1))

    client = T3Client(BASE, path, ["t3"], transport=httpx.MockTransport(handle))
    client.session, client.protocol, client.token = "fixture-session", "2025-06-18", "fixture"
    if response == "chunked":
        assert client.confirmed_send("fixture-thread", "Fixture text", None, 7) == {"accepted": True}
    elif response == "unauthorized":
        with pytest.raises(T3Unavailable, match="Reconnect") as caught:
            client.confirmed_send("fixture-thread", "Fixture text", None, 7)
        assert not isinstance(caught.value, T3Uncertain)
    else:
        with pytest.raises(T3Uncertain, match="size limit|gateway request failed"):
            client.confirmed_send("fixture-thread", "Fixture text", None, 7)
