"""Subscription agents over the Agent Client Protocol (ACP v1): a pilot (v0.8.6 W7, owner decisions E114 and D20).

Codex (`codex-acp`), Claude (`claude-agent-acp`) and Antigravity (`agy acp`) are coding agents the owner may already
pay for. Here each is used as a plain text model for the two roles that suit it (escalation and drafting), in the
owner's DM only, behind its own switch (`[acp.<name>] enabled`). The client is hand-written JSON-RPC over the agent's
stdin and stdout, in the research note's shape (docs/superpowers/notes/v0.8.3-acp-agents.md, section 6):

- One process per call: start it, `initialize`, `session/new`, `session/prompt` (streaming `session/update` text),
  then stop it. Nothing stays running between calls, so a hang, a crash or a settings change leaves nothing behind.
- An agent starts only when Dom has switched its own tools off (`ACP_NO_TOOLS_OFF` names those that cannot be), and
  every `session/request_permission` is refused as well.
- A hard timeout cancels the prompt (`session/cancel`) and kills the whole process group, as does cancelling the task.
- Dom keeps its own tool loop: the tools go in the prompt as text, and the agent answers with either its reply or a
  `{"tool_calls": [...]}` object, which becomes an ordinary OpenAI-style `tool_calls` message.
- Only the owner's DM turn reaches an agent: the chat id and the kind of the request in progress are both checked.
- Calls are billed to the subscription: usage rows of kind `subscription` carry cost 0, so spend caps are not fooled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import tempfile
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from dom import modalities, turn_log
from dom.config import ACP_COMMANDS, ACP_LABELS, ACP_NO_TOOLS_OFF, AcpAgent, AcpName, Config, ProviderEndpoint
from dom.usage import UsageRecorder, check_model_allowed, request_chat_id, request_kind

log = logging.getLogger("dom.acp")

PROTOCOL_VERSION = 1
CANCEL_GRACE = 1.0  # seconds a cancelled prompt gets to answer before the process is killed
STOP_GRACE = 1.0  # seconds the adapter gets after SIGTERM before SIGKILL
STREAM_LIMIT = 16 * 1024 * 1024  # one JSON-RPC line, a long streamed reply included
MAX_REPLY_CHARS = 200_000
AUTH_REQUIRED = -32000  # the ACP error code for "sign in first"
# The only variables the agent's process inherits: the search path, the home it finds its login in, and the locale.
# Dom's own secrets (model keys, the bot token) and the owner's ssh agent never reach it.
ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE")
# The requests an agent serves, by the kind it counts as: its own role's, plus the DM turn's escalation for drafting
# (an escalated turn may draft a skill). Background work under the owner's chat id, a desktop task's skill drafting
# included, carries other kinds and never reaches an agent.
SERVED_KINDS = {"escalation": ("escalation",), "chat": ("chat", "escalation")}

# Each agent's own switches for its built-in tools, which ACP leaves to the agent (v0.8.3 research, section 2).
# Not every one is documented: main's live test checks that a prompt asking each agent to run a command fails.
CODEX_CONFIG = {"features": {"shell_tool": False}, "web_search": "disabled"}
TOOLS_OFF_ENV: dict[str, dict[str, str]] = {
    "codex": {"INITIAL_AGENT_MODE": "read-only", "CODEX_CONFIG": json.dumps(CODEX_CONFIG)},
}
TOOLS_OFF_META: dict[str, dict] = {  # sent as `_meta` on session/new; an adapter that does not know it ignores it
    "claude": {"claudeCode": {"options": {"tools": []}}},
}

PLAIN_TEXT_NOTICE = (
    "You are the language model of another program and answer in plain text only. Do not use any tool of your own: "
    "no shell commands, no reading or editing files, no web search. Everything you need is below. Write the "
    "assistant's next message and nothing else."
)
TOOLS_NOTICE = (
    "You may call the tools listed below. To call one or more, reply with only a JSON object "
    '{"tool_calls": [{"name": "<tool>", "arguments": {...}}]} and nothing else; the results come back as '
    "tool_result messages. When no tool is needed, reply as the instructions above say.\nTools: "
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class AcpError(RuntimeError):
    """The agent could not answer: not installed, signed out, crashed, refused, timed out or not allowed here."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class AcpNotAllowed(AcpError):
    """A call outside the owner's DM, which subscription agents never serve."""


def launch(config: Config, name: AcpName) -> tuple[list[str], dict[str, str], str]:
    """The command line, environment and session working directory for one agent. A remote host runs the same
    command over ssh (key login, no prompts) in a fresh empty directory, which is removed when it ends; the tool
    switches then travel as `env` arguments."""
    agent: AcpAgent = getattr(config.acp, name)
    default_command, default_args = ACP_COMMANDS[name]
    command = [agent.command or default_command, *(default_args if agent.args is None else agent.args)]
    switches = dict(TOOLS_OFF_ENV.get(name, {}))
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    if agent.host:
        box = f"/tmp/dom-acp-{uuid4().hex[:16]}"
        run = shlex.join(["env", *(f"{key}={value}" for key, value in switches.items()), *command])
        remote = f"d={box}; mkdir -m 700 \"$d\" && trap 'cd /; rmdir \"$d\"' EXIT && cd \"$d\" && {run}"
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", agent.host, remote]
        return argv, env, box
    env.update(switches)
    for variable in agent.auth_env:
        if value := config.secret(variable):
            env[variable] = value
    return command, env, ""


class _Process:
    """One agent process, spoken to in newline-delimited JSON-RPC, for one call."""

    def __init__(self, label: str, argv: list[str], env: dict[str, str], cwd: str):
        self.label, self.argv, self.env, self.cwd = label, argv, env, cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.text: list[str] = []
        self.size = 0
        self.refused = 0  # permission requests answered with a refusal
        self.session: str | None = None
        self._ids = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._stderr = b""

    async def start(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self.env, cwd=self.cwd, limit=STREAM_LIMIT,
                start_new_session=True,  # so the whole group, the adapter's own children included, can be stopped
            )
        except OSError as exc:
            raise AcpError(f"{self.label} could not start ({type(exc).__name__}): is it installed here?") from None
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._drain_stderr())]

    async def call(self, method: str, params: dict) -> dict:
        self._ids += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[self._ids] = future
        await self._send({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params})
        result = await future
        return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(message).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise AcpError(f"{self.label} closed its input") from None

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while line := await self.proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # an adapter printing a log line on stdout
                if isinstance(message, dict):
                    await self._handle(message)
        except Exception as exc:  # an oversized line, a broken pipe
            log.warning("%s: reading the agent's output failed (%s)", self.label, type(exc).__name__)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AcpError(f"{self.label} exited before answering"))

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        with contextlib.suppress(Exception):
            while chunk := await self.proc.stderr.read(4096):
                self._stderr = (self._stderr + chunk)[-2000:]  # the tail only, shown at debug level

    async def _handle(self, message: dict) -> None:
        method, ident = message.get("method"), message.get("id")
        if method is None:  # a response to one of ours
            future = self._pending.pop(ident, None) if isinstance(ident, int) else None
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), int) else None
                text = str(error.get("message") or "")[:200]
                future.set_exception(AcpError(f"{self.label} answered with an error ({code}): {text}", code))
            else:
                future.set_result(message.get("result"))
        elif ident is None:  # a notification
            if method == "session/update":
                self._update(message.get("params"))
        elif method == "session/request_permission":  # the agent wants to run a tool: always no
            self.refused += 1
            await self._send({"jsonrpc": "2.0", "id": ident, "result": {"outcome": refusal(message.get("params"))}})
        else:  # fs/* and terminal/* are not offered (no client capabilities), so anything else is unsupported
            await self._send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})

    def _update(self, params: object) -> None:
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
            return  # thoughts, plans and tool progress are not part of the reply
        content = update.get("content")
        chunk = content.get("text") if isinstance(content, dict) and content.get("type") == "text" else None
        if isinstance(chunk, str) and self.size < MAX_REPLY_CHARS:
            self.text.append(chunk)
            self.size += len(chunk)

    async def ask(self, prompt: str, cwd: str, meta: dict | None) -> str:
        init = await self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}, "clientInfo": {"name": "dom", "version": "1"},
        })
        params = {"cwd": cwd, "mcpServers": [], **({"_meta": meta} if meta else {})}
        try:
            session = await self.call("session/new", params)
        except AcpError as exc:
            methods = init.get("authMethods")
            if exc.code != AUTH_REQUIRED or not (isinstance(methods, list) and methods and isinstance(methods[0], dict)):
                raise
            await self.call("authenticate", {"methodId": methods[0].get("id")})  # a login the agent already holds
            session = await self.call("session/new", params)
        if not isinstance(session.get("sessionId"), str):
            raise AcpError(f"{self.label} started no session")
        self.session = session["sessionId"]
        done = await self.call("session/prompt", {"sessionId": self.session, "prompt": [{"type": "text", "text": prompt}]})
        self.session = None  # answered: nothing left to cancel
        stop = done.get("stopReason")
        reply = "".join(self.text).strip()
        if stop == "refusal" or not reply:
            raise AcpError(f"{self.label} gave no reply (stop reason {stop or 'unknown'}, {self.refused} tool requests refused)")
        return reply

    async def stop(self) -> None:
        """Cancel an open prompt, then end the process group. Safe to call whatever state the call ended in."""
        proc = self.proc
        if proc is not None and proc.returncode is None:
            if self.session is not None:
                with contextlib.suppress(Exception):
                    await self.notify("session/cancel", {"sessionId": self.session})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), CANCEL_GRACE)  # let the agent act on it
            self._signal(signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
        if proc is not None:
            self._signal(signal.SIGKILL)  # children can outlive the leader, so the group is always cleared
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
        for task in self._tasks:
            task.cancel()
        if self._stderr:
            log.debug("%s stderr tail: %s", self.label, self._stderr.decode(errors="replace"))

    def _signal(self, sig: signal.Signals) -> None:
        if self.proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, sig)


def refusal(params: object) -> dict:
    """The answer to a permission request: pick the agent's own reject option, else cancel."""
    options = params.get("options") if isinstance(params, dict) else None
    for option in options if isinstance(options, list) else ():
        if isinstance(option, dict) and str(option.get("kind", "")).startswith("reject") and option.get("optionId"):
            return {"outcome": "selected", "optionId": option["optionId"]}
    return {"outcome": "cancelled"}


async def ask(label: str, argv: list[str], env: dict[str, str], prompt: str, *, timeout: float,
              session_cwd: str = "", meta: dict | None = None) -> str:
    """Run one prompt on a fresh agent process and return its streamed text. Raises AcpError on any failure; the
    process is gone however this ends, including when the calling task is cancelled."""
    with tempfile.TemporaryDirectory(prefix="dom-acp-") as empty:
        process = _Process(label, argv, env, empty)
        try:
            async with asyncio.timeout(timeout):
                await process.start()
                return await process.ask(prompt, session_cwd or empty, meta)
        except TimeoutError:
            raise AcpError(f"{label} did not answer within {timeout:g} s") from None
        finally:
            await process.stop()


def render_prompt(messages: list[dict], tools: list[dict]) -> str:
    """The conversation as one text prompt, with Dom's tools described in it when there are any."""
    parts = [PLAIN_TEXT_NOTICE]
    if tools:
        parts.append(TOOLS_NOTICE + json.dumps([tool.get("function", tool) for tool in tools], ensure_ascii=False))
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = content if isinstance(content, str) else ""
        if role == "assistant" and message.get("tool_calls"):
            calls = [{"name": call["function"]["name"], "arguments": _arguments(call["function"].get("arguments"))}
                     for call in message["tool_calls"] if isinstance(call.get("function"), dict)]
            text = (text + "\n" if text else "") + json.dumps({"tool_calls": calls}, ensure_ascii=False)
        if role == "tool":
            parts.append(f'<tool_result id="{message.get("tool_call_id", "")}">\n{text}\n</tool_result>')
        else:
            tag = "system" if role in ("system", "developer") else str(role)
            parts.append(f"<{tag}>\n{text}\n</{tag}>")
    return "\n\n".join(parts)


def _arguments(raw: object) -> object:
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def _tool_call(call: object, names: set[str]) -> dict | None:
    """One well-formed call for a tool Dom offered, with its arguments as an object, else None."""
    if not isinstance(call, dict) or not isinstance(call.get("name"), str) or call["name"] not in names:
        return None
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    return {"name": call["name"], "arguments": arguments} if isinstance(arguments, dict) else None


def as_message(text: str, tools: list[dict]) -> dict:
    """An OpenAI-style assistant message from the agent's text: `tool_calls` when it answered with a well-formed
    tool call object for tools Dom offered, else the text itself (the actions JSON, or whatever it wrote). A tool
    call object that is not well-formed is logged and passed on as text, as any other output of the agent is."""
    names = {name for tool in tools if isinstance(name := tool.get("function", {}).get("name"), str)}
    if names:
        fenced = _FENCE.match(text.strip())
        try:
            data = json.loads(fenced.group(1) if fenced else text)
        except ValueError:
            data = None
        if isinstance(data, dict) and "tool_calls" in data:
            raw = data["tool_calls"]
            calls = [_tool_call(call, names) for call in raw] if isinstance(raw, list) and raw else []
            if calls and all(calls):
                batch = uuid4().hex[:8]
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"acp_{batch}_{index}", "type": "function",
                     "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                    for index, call in enumerate(calls)
                ]}
            log.info("a tool call from the agent was malformed or named a tool Dom did not offer; kept as text")
    return {"role": "assistant", "content": text}


@dataclass
class AcpClient:
    """A subscription agent with the same surface as ChatClient, so a role's chain can hold it. It answers only in the
    owner's DM turns, and only with its tools switched off: the checks are in `complete`, in code, where the call
    is made."""

    config: Config
    name: AcpName
    kind: str  # the request kind it counts as in traces: "escalation" or "chat" (drafting)
    usage: UsageRecorder | None = None
    available: bool = field(default=True, init=False)
    inputs: frozenset[str] = field(default_factory=frozenset, init=False)  # text only; media is described instead

    def __post_init__(self) -> None:
        self.endpoint = ProviderEndpoint(base_url=f"acp://{self.name}", model=f"{ACP_LABELS[self.name]} subscription")
        self.address = self.host = f"acp:{self.name}"
        self.http = None

    async def complete(
        self, messages: list[dict], tools: list[dict], *, fallback: bool = False, once: bool = False,
        max_tokens: int | None = None, timeout: float = 0,
    ) -> dict:
        """The agent's reply as an assistant message. The caller's HTTP-sized `timeout` is ignored: the agent's own
        `timeout_seconds` is the hard limit."""
        await check_model_allowed()
        chat_id = request_chat_id()
        owner = self.usage.store.owner_id if self.usage is not None else None
        if owner is None or chat_id != owner:
            raise AcpNotAllowed(f"{ACP_LABELS[self.name]} serves the owner's DM only")
        if request_kind(self.kind) not in SERVED_KINDS[self.kind]:  # background work under the owner's chat id
            raise AcpNotAllowed(f"{ACP_LABELS[self.name]} serves the owner's DM turns only")
        if reason := ACP_NO_TOOLS_OFF.get(self.name):
            log.warning("%s agent not started: %s", self.name, reason)
            raise AcpError(f"{ACP_LABELS[self.name]} is not started: {reason}")
        if modalities.has_attachments(messages):
            messages, _ = await modalities.prepare(messages, frozenset())
        agent: AcpAgent = getattr(self.config.acp, self.name)
        argv, env, session_cwd = launch(self.config, self.name)
        prompt = render_prompt(messages, tools)
        started = monotonic()
        try:
            with turn_log.model_call(
                self.endpoint.model, request_kind(self.kind), messages, tools, fallback=fallback, host=self.address,
                body={"agent": self.name, "billing": "subscription", "host": agent.host or "local"},
                url=f"acp://{self.name}",
            ) as call:
                turn_log.step("subscription agent")
                text = await ask(
                    ACP_LABELS[self.name], argv, env, prompt, timeout=agent.timeout_seconds, session_cwd=session_cwd,
                    meta=TOOLS_OFF_META.get(self.name),
                )
                if call is not None:
                    call.status = "ok"
        except AcpError as exc:
            log.warning("%s agent failed after %.1f s: %s", self.name, monotonic() - started, exc)
            raise
        else:
            log.info("%s agent answered in %.1f s", self.name, monotonic() - started)
            return as_message(text, tools)
        finally:  # a call that failed or was cancelled still used some of the plan's allowance
            if self.usage is not None:
                await self.usage.record("subscription", self.endpoint.model, self.address, None, chat_id=chat_id, fallback=fallback)


def acp_clients(config: Config, role: str, usage: UsageRecorder | None) -> list[AcpClient]:
    """The switched-on agents for a role, for the front of its chain."""
    kind = "escalation" if role == "escalation" else "chat"
    return [AcpClient(config, name, kind, usage) for name, _ in config.acp.agents(role)]
