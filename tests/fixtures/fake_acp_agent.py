"""A fake ACP agent for tests: JSON-RPC over stdio, in the mode named by argv[1], logging what it saw to the JSON
lines file named by argv[2]. Modes: ok, noise, permission, hang, crash, auth, empty, tools."""

import json
import os
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
log_path = sys.argv[2] if len(sys.argv) > 2 else None


def log(**event):
    if log_path:
        with open(log_path, "a") as handle:
            handle.write(json.dumps(event) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def update(session, kind, text=None, **extra):
    content = {"type": "text", "text": text} if text is not None else None
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session, "update": {"sessionUpdate": kind, **({"content": content} if content else {}), **extra},
    }})


log(event="start", pid=os.getpid(), cwd=os.getcwd(), initial_agent_mode=os.environ.get("INITIAL_AGENT_MODE"),
    codex_config=os.environ.get("CODEX_CONFIG"), has_secret=bool(os.environ.get("DOM_TEST_SECRET")),
    has_api_key=bool(os.environ.get("FAKE_API_KEY")), env_keys=sorted(os.environ))
if mode == "noise":
    sys.stdout.write("agent starting up...\n")
    sys.stdout.flush()
signed_in = mode != "auth"
waiting_prompt = None  # the prompt request waiting for the permission answer

for line in sys.stdin:
    message = json.loads(line)
    method, ident = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if method is None:  # a response to a request of ours
        if ident == "perm-1":
            log(event="permission_answer", result=message.get("result"))
            update("s1", "agent_message_chunk", "done after refusal")
            send({"jsonrpc": "2.0", "id": waiting_prompt, "result": {"stopReason": "end_turn"}})
        continue
    if method == "initialize":
        log(event="initialize", params=params)
        result = {"protocolVersion": 1, "agentCapabilities": {}}
        if mode == "auth":
            result["authMethods"] = [{"id": "login", "name": "Login"}]
        send({"jsonrpc": "2.0", "id": ident, "result": result})
    elif method == "authenticate":
        log(event="authenticate", params=params)
        signed_in = True
        send({"jsonrpc": "2.0", "id": ident, "result": {}})
    elif method == "session/new":
        log(event="session_new", params=params)
        if not signed_in:
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": "Authentication required"}})
        else:
            send({"jsonrpc": "2.0", "id": ident, "result": {"sessionId": "s1"}})
    elif method == "session/cancel":
        log(event="cancel", params=params)
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        log(event="prompt", text=text)
        if mode == "hang":
            continue  # never answers
        if mode == "crash":
            sys.exit(3)
        if mode == "permission":
            waiting_prompt = ident
            send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission", "params": {
                "sessionId": "s1", "toolCall": {"toolCallId": "t1", "title": "Run ls"},
                "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "no", "name": "Reject", "kind": "reject_once"}],
            }})
            continue
        if mode == "empty":
            send({"jsonrpc": "2.0", "id": ident, "result": {"stopReason": "refusal"}})
            continue
        update("s1", "agent_thought_chunk", "thinking about it")
        update("s1", "tool_call", toolCallId="t0", title="ignored")
        if mode == "tools":
            reply = ['{"tool_calls": [{"name": "remind", ', '"arguments": {"text": "tea"}}]}']
        else:
            reply = ["Hello, ", "owner."]
        for chunk in reply:
            update("s1", "agent_message_chunk", chunk)
        send({"jsonrpc": "2.0", "id": ident, "result": {"stopReason": "end_turn"}})
    else:
        send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
