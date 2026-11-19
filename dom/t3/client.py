"""The local T3 gateway's small, allowlisted MCP client."""

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

READS = frozenset({"t3_project_list", "t3_thread_list", "t3_thread_search", "t3_thread_read"})
TIMEOUT = httpx.Timeout(20, connect=5)
LIMIT = 512 * 1024


class T3Unavailable(RuntimeError):
    pass


class SessionGone(T3Unavailable):
    pass


def local_base(url):
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parts.username is not None or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise T3Unavailable("T3 base URL must be a loopback HTTP origin.")
    return url.rstrip("/")


def read_token(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        raise T3Unavailable("Reconnect to T3 Code.") from None


def cli_json(cli, args, runner):
    try:
        result = runner([*cli, *args], capture_output=True, text=True, check=True, timeout=20)
        if len(result.stdout.encode()) <= LIMIT:
            return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    raise T3Unavailable("T3 CLI did not return a usable response.")


def request(http, url, **kwargs):
    try:
        with http.stream("POST", url, **kwargs) as response:
            if response.status_code == 401:
                raise T3Unavailable("T3 sign-in was rejected. Reconnect to T3 Code.")
            if response.status_code == 404 and "mcp-session-id" in kwargs.get("headers", {}):
                raise SessionGone("T3 session expired.")
            response.raise_for_status()
            data = bytearray()
            deadline = time.monotonic() + 20
            for chunk in response.iter_bytes(chunk_size=8192):
                data.extend(chunk)
                if time.monotonic() > deadline or len(data) > LIMIT:
                    raise T3Unavailable("T3 response exceeded its time or size limit.")
            if not data:
                return {}, response.headers
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                for event in data.decode().replace("\r\n", "\n").split("\n\n"):
                    lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
                    if lines:
                        message = json.loads("\n".join(lines))
                        if isinstance(message, dict) and ("result" in message or "error" in message):
                            return message, response.headers
                raise T3Unavailable("T3 returned no MCP result.")
            return json.loads(data), response.headers
    except (httpx.HTTPError, ValueError):
        raise T3Unavailable("T3 gateway request failed.") from None


class T3Client:
    def __init__(self, base_url, token_path, cli, *, transport=None, runner=subprocess.run):
        self.url = local_base(base_url) + "/mcp"
        self.token_path = token_path
        self.cli = cli
        self.transport = transport
        self.runner = runner
        self.session = None
        self.protocol = None
        self.token = None
        self.request_id = 0

    def environments(self):
        data = cli_json(self.cli, ["environment", "list", "--json"], self.runner)
        try:
            return [{key: row[key] for key in ("label", "environmentId", "status")} for row in data]
        except (TypeError, KeyError):
            raise T3Unavailable("T3 returned invalid machine data.") from None

    def _rpc(self, http, method, params):
        headers = {"authorization": f"Bearer {self.token}", "accept": "application/json, text/event-stream"}
        if self.session:
            headers.update({"mcp-session-id": self.session, "mcp-protocol-version": self.protocol})
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if method != "notifications/initialized":
            self.request_id += 1
            body["id"] = self.request_id
        data, response_headers = request(http, self.url, headers=headers, json=body)
        if not isinstance(data, dict) or "error" in data:
            raise T3Unavailable("T3 rejected the MCP request.")
        return data.get("result"), response_headers

    def _initialize(self, http):
        result, headers = self._rpc(http, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "Dom", "version": "1"},
        })
        self.session = headers.get("mcp-session-id")
        self.protocol = result.get("protocolVersion") if isinstance(result, dict) else None
        if not self.session or not isinstance(self.protocol, str):
            self.session = None
            raise T3Unavailable("T3 did not establish an MCP session.")
        try:
            self._rpc(http, "notifications/initialized", {})
        except T3Unavailable:
            self.session = None
            raise

    def call(self, tool, args):
        if tool not in READS:
            raise T3Unavailable("T3 tool is outside the read allowlist.")
        return self._call(tool, args)

    def confirmed_send(self, thread_id, text, environment_id, audit_id):
        args = {"threadId": thread_id, "message": text, "mode": "queue", "clientRequestId": f"dom-{audit_id}"}
        if environment_id is not None:
            args["environmentId"] = environment_id
        return self._call("t3_thread_send", args)

    def _call(self, tool, args):
        token = read_token(self.token_path)
        try:
            if not isinstance(token["access_token"], str) or not token["expires_at"] > time.time():
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise T3Unavailable("T3 sign-in expired or is invalid. Reconnect to T3 Code.") from None
        if token["access_token"] != self.token:
            self.token = token["access_token"]
            self.session = None
        with httpx.Client(transport=self.transport, timeout=TIMEOUT, trust_env=False) as http:
            for attempt in range(2):
                try:
                    if not self.session:
                        self._initialize(http)
                    result, _ = self._rpc(http, "tools/call", {"name": tool, "arguments": args})
                    if not isinstance(result, dict) or result.get("isError") or "structuredContent" not in result:
                        raise T3Unavailable("T3 tool failed or the machine is unreachable.")
                    return result["structuredContent"]
                except SessionGone:
                    self.session = None
                    if attempt:
                        raise
