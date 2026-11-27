"""Subscription agents over ACP (v0.8.6 W7): the client against a fake agent process, the routing limits, the owner-DM
guard, the switches, usage attribution and the migration. No real agent runs here."""

import asyncio
import json
import logging
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from dom import acp
from dom.agent import NoFinalAnswer
from dom.acp import AcpClient, AcpError, AcpNotAllowed, ask, as_message, launch, render_prompt
from dom.config import AcpAgent, AcpConfig, Config, ModelEndpoint, load_config
from dom.llm import ChatClient, FallbackClient, routed_client
from dom.usage import model_request
from tghelpers import FRIEND, OWNER, no_sleep

FAKE = str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")
ENV = {"PATH": os.environ.get("PATH", "")}


@pytest.fixture(autouse=True)
def quick_stops(monkeypatch):
    monkeypatch.setattr(acp, "CANCEL_GRACE", 0.3)
    monkeypatch.setattr(acp, "STOP_GRACE", 0.5)


def run(tmp_path, mode="ok", **kw):
    log = tmp_path / "agent.jsonl"
    argv = [sys.executable, FAKE, mode, str(log)]
    return ask("Fake", argv, kw.pop("env", ENV), kw.pop("prompt", "hi"), timeout=kw.pop("timeout", 10), **kw), log


def events(log, name=None):
    rows = [json.loads(line) for line in Path(log).read_text().splitlines()] if Path(log).exists() else []
    return [row for row in rows if name is None or row["event"] == name]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---- the client against a fake agent -----------------------------------------------------------------------------------


async def test_a_prompt_streams_its_text_and_ignores_thoughts_and_tool_progress(tmp_path):
    call, log = run(tmp_path, prompt="say hello")
    assert await call == "Hello, owner."
    (initialize,) = events(log, "initialize")
    assert initialize["params"]["protocolVersion"] == 1 and initialize["params"]["clientCapabilities"] == {}
    (session,) = events(log, "session_new")
    assert session["params"]["mcpServers"] == [] and "_meta" not in session["params"]
    assert events(log, "prompt")[0]["text"] == "say hello"
    assert not alive(events(log, "start")[0]["pid"])  # one process per call, gone when it ends


async def test_the_session_works_in_an_empty_directory_that_is_removed_afterwards(tmp_path):
    call, log = run(tmp_path)
    await call
    directory = Path(events(log, "session_new")[0]["params"]["cwd"])
    assert directory.name.startswith("dom-acp-") and not directory.exists()


async def test_every_permission_request_is_refused_with_the_agents_reject_option(tmp_path):
    call, log = run(tmp_path, "permission")
    assert await call == "done after refusal"
    (answer,) = events(log, "permission_answer")
    assert answer["result"] == {"outcome": {"outcome": "selected", "optionId": "no"}}


def test_a_permission_request_without_a_reject_option_is_cancelled():
    assert acp.refusal({"options": [{"optionId": "y", "kind": "allow_once"}]}) == {"outcome": "cancelled"}
    assert acp.refusal(None) == {"outcome": "cancelled"}


async def test_session_meta_is_sent_when_given(tmp_path):
    call, log = run(tmp_path, meta={"claudeCode": {"options": {"tools": []}}})
    await call
    assert events(log, "session_new")[0]["params"]["_meta"] == {"claudeCode": {"options": {"tools": []}}}


async def test_a_hard_timeout_cancels_the_prompt_and_kills_the_process(tmp_path):
    call, log = run(tmp_path, "hang", timeout=1)
    with pytest.raises(AcpError, match="did not answer within 1 s"):
        await call
    assert events(log, "cancel") == [{"event": "cancel", "params": {"sessionId": "s1"}}]
    assert not alive(events(log, "start")[0]["pid"])


async def test_cancelling_the_task_cancels_the_prompt_and_kills_the_process(tmp_path):
    call, log = run(tmp_path, "hang")
    task = asyncio.create_task(call)
    while not events(log, "prompt"):
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events(log, "cancel")
    assert not alive(events(log, "start")[0]["pid"])


async def test_a_crash_is_an_error_not_a_hang(tmp_path):
    call, _ = run(tmp_path, "crash")
    with pytest.raises(AcpError, match="exited before answering"):
        await call


async def test_an_agent_that_is_not_installed_is_an_error(tmp_path):
    with pytest.raises(AcpError, match="could not start"):
        await ask("Fake", [str(tmp_path / "no-such-agent")], ENV, "hi", timeout=5)


async def test_a_refusal_or_empty_reply_is_an_error(tmp_path):
    call, _ = run(tmp_path, "empty")
    with pytest.raises(AcpError, match="gave no reply"):
        await call


async def test_log_noise_on_stdout_is_skipped(tmp_path):
    call, _ = run(tmp_path, "noise")
    assert await call == "Hello, owner."


async def test_a_signed_in_agent_that_asks_to_authenticate_is_authenticated_once(tmp_path):
    call, log = run(tmp_path, "auth")
    assert await call == "Hello, owner."
    assert events(log, "authenticate") == [{"event": "authenticate", "params": {"methodId": "login"}}]
    assert len(events(log, "session_new")) == 2


# ---- launching: environment, tools off, a remote host -----------------------------------------------------------------


def cfg(tmp_path, **agents):
    return Config(home=tmp_path, owner_id=OWNER, acp=AcpConfig(**{name: AcpAgent(**value) for name, value in agents.items()}))


def test_the_agents_process_gets_no_secret_it_was_not_named(tmp_path, monkeypatch):
    monkeypatch.setenv("DOM_TEST_SECRET", "hunter2")
    monkeypatch.setenv("FAKE_API_KEY", "key-value")
    config = cfg(tmp_path, codex={"enabled": True, "auth_env": ["FAKE_API_KEY"]})
    argv, env, session_cwd = launch(config, "codex")
    assert argv == ["codex-acp"] and session_cwd == ""
    assert "DOM_TEST_SECRET" not in env and env["FAKE_API_KEY"] == "key-value" and "PATH" in env


def test_the_environment_is_path_home_and_locale_and_nothing_else(tmp_path, monkeypatch):
    polluted = {
        "PATH": "/bin", "HOME": "/home/x", "LANG": "en_SG.UTF-8", "LC_ALL": "C", "LC_CTYPE": "UTF-8", "LANGUAGE": "en",
        "SSH_AUTH_SOCK": "/tmp/agent.sock", "USER": "x", "LOGNAME": "x", "SHELL": "/bin/zsh", "TMPDIR": "/tmp/x",
        "XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d", "TELEGRAM_BOT_TOKEN": "t", "OPENROUTER_API_KEY": "k",
        "GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "a", "DISPLAY": ":0", "LC_MESSAGES": "x",
    }
    monkeypatch.setattr(os, "environ", polluted)
    for name in ("codex", "claude", "antigravity"):
        for host in (None, "studio-mac"):
            _, env, _ = launch(cfg(tmp_path, **{name: {"enabled": True, "host": host}}), name)
            switches = set(acp.TOOLS_OFF_ENV.get(name, {})) if host is None else set()  # remote ones travel as `env` args
            assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE"} | switches


async def test_the_running_agent_sees_no_ssh_agent_and_no_dom_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "SSH_AUTH_SOCK": "/tmp/agent.sock", "OPENROUTER_API_KEY": "k"})
    _, env, _ = launch(cfg(tmp_path, codex={"enabled": True}), "codex")
    call, log = run(tmp_path, env=env)
    await call
    seen = set(events(log, "start")[0]["env_keys"])
    assert not seen & {"SSH_AUTH_SOCK", "OPENROUTER_API_KEY", "USER", "SHELL", "TMPDIR"}


def test_codex_starts_read_only_with_its_shell_tool_and_web_search_off(tmp_path):
    _, env, _ = launch(cfg(tmp_path, codex={"enabled": True}), "codex")
    assert env["INITIAL_AGENT_MODE"] == "read-only"
    assert json.loads(env["CODEX_CONFIG"]) == {"features": {"shell_tool": False}, "web_search": "disabled"}


def test_each_agent_has_its_usual_command_unless_configured(tmp_path):
    config = cfg(tmp_path, claude={"enabled": True}, antigravity={"enabled": True}, codex={"command": "/opt/codex-acp", "args": ["--x"]})
    assert launch(config, "claude")[0] == ["claude-agent-acp"]
    assert launch(config, "antigravity")[0] == ["agy", "acp"]
    assert launch(config, "codex")[0] == ["/opt/codex-acp", "--x"]
    assert acp.TOOLS_OFF_META["claude"] == {"claudeCode": {"options": {"tools": []}}}


def test_a_remote_host_runs_the_same_command_over_ssh_with_the_tool_switches_as_env(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "uuid4", lambda: SimpleNamespace(hex="0123456789abcdef0123456789abcdef"))
    argv, env, session_cwd = launch(cfg(tmp_path, codex={"enabled": True, "host": "studio-mac"}), "codex")
    box = "/tmp/dom-acp-0123456789abcdef"
    assert session_cwd == box and "INITIAL_AGENT_MODE" not in env
    # `--` comes before the destination, so ssh reads nothing after it as an option and the remote shell gets one string
    assert argv[:-1] == ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "studio-mac"]
    config = json.dumps(acp.CODEX_CONFIG)
    assert argv[-1] == (
        f"d={box}; mkdir -m 700 \"$d\" && trap 'cd /; rm -rf \"$d\"' EXIT && cd \"$d\" && "
        f"env INITIAL_AGENT_MODE=read-only {shlex.quote('CODEX_CONFIG=' + config)} codex-acp"
    )


def test_the_remote_command_is_quoted_for_the_remote_shell_and_runs_in_a_fresh_empty_directory(tmp_path):
    """The remote string is run here by `sh -c`, as sshd's login shell would run it."""
    out = tmp_path / "seen.json"
    script = tmp_path / "an agent.py"  # a path with a space, and arguments with spaces and shell characters
    script.write_text("import json, os, sys\njson.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'ls': os.listdir('.')}, open(sys.argv[1], 'w'))\n")
    args = [str(out), "two words", "$HOME", "it's", "a;b"]
    argv, _, session_cwd = launch(cfg(tmp_path, claude={"enabled": True, "host": "mac", "command": sys.executable, "args": [str(script), *args]}), "claude")
    assert subprocess.run(["sh", "-c", argv[-1]], check=True).returncode == 0
    seen = json.loads(out.read_text())
    assert seen["argv"] == args and seen["ls"] == []
    assert seen["cwd"] == str(Path(session_cwd).resolve()) and session_cwd != "/tmp"
    assert not Path(session_cwd).exists()  # removed again when the command ends


def test_secrets_are_never_sent_to_a_remote_host_and_a_host_cannot_look_like_an_option():
    with pytest.raises(ValidationError, match="never sent to another machine"):
        AcpAgent(host="mac", auth_env=["CODEX_API_KEY"])
    with pytest.raises(ValidationError):
        AcpAgent(host="-oProxyCommand=x")
    with pytest.raises(ValidationError):
        AcpAgent(command="-rf")
    with pytest.raises(ValidationError):
        AcpAgent(auth_env=["not a name"])


# ---- the prompt and its tool protocol ---------------------------------------------------------------------------------


TOOLS = [{"type": "function", "function": {"name": "remind", "description": "Set a reminder", "parameters": {"type": "object"}}}]


def test_the_prompt_carries_the_conversation_and_the_tools():
    messages = [
        {"role": "system", "content": "Be Dom."},
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "remind", "arguments": '{"text": "tea"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "set"},
    ]
    prompt = render_prompt(messages, TOOLS)
    assert "Do not use any tool of your own" in prompt and '"name": "remind"' in prompt
    assert "<system>\nBe Dom.\n</system>" in prompt and "<user>\nhi\n</user>" in prompt
    assert '{"tool_calls": [{"name": "remind", "arguments": {"text": "tea"}}]}' in prompt
    assert '<tool_result id="c1">\nset\n</tool_result>' in prompt
    assert "tool_calls" not in render_prompt(messages[:2], [])


def test_a_tool_call_object_becomes_an_openai_style_message_and_anything_else_stays_text():
    message = as_message('```json\n{"tool_calls": [{"name": "remind", "arguments": {"text": "tea"}}]}\n```', TOOLS)
    (call,) = message["tool_calls"]
    assert message["content"] is None and call["function"] == {"name": "remind", "arguments": '{"text": "tea"}'}
    actions = '{"bubbles": [{"text": "hi"}]}'
    assert as_message(actions, TOOLS) == {"role": "assistant", "content": actions}
    unknown = '{"tool_calls": [{"name": "rm_rf", "arguments": {}}]}'
    assert as_message(unknown, TOOLS)["content"] == unknown  # not an offered tool: left as text
    assert as_message('{"tool_calls": []}', [])["content"] == '{"tool_calls": []}'  # no tools offered


# ---- the provider: owner DM only, usage, tool calls --------------------------------------------------------------------


def agent_config(app, tmp_path, mode="ok", name="codex", **extra):
    log = tmp_path / "agent.jsonl"
    agent = AcpAgent(enabled=True, command=sys.executable, args=(FAKE, mode, str(log)), **extra)
    return app.config.model_copy(update={"acp": AcpConfig(**{name: agent})}), log


def usage_rows(app):
    with sqlite3.connect(app.store.db_path) as conn:
        return conn.execute("SELECT kind, model, endpoint_host, chat_id, input_tokens, cost_usd, fallback FROM usage ORDER BY id").fetchall()


async def allowed():
    return True


async def test_the_owners_dm_gets_a_reply_recorded_as_subscription_usage_with_no_cost(make_app, tmp_path):
    app = make_app()
    config, log = agent_config(app, tmp_path)
    client = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]
    with model_request("escalation", OWNER, allowed):
        reply = await client.complete([{"role": "user", "content": "hi"}], [])
    assert reply == {"role": "assistant", "content": "Hello, owner."}
    assert usage_rows(app) == [("subscription", "Codex subscription", "acp:codex", OWNER, 0, 0.0, 0)]
    assert events(log, "initialize")


async def test_a_failed_call_still_counts_as_subscription_usage(make_app, tmp_path):
    app = make_app()
    config, _ = agent_config(app, tmp_path, "crash")
    client = acp.acp_clients(config, "drafting", app.dispatch.usage)[0]
    with model_request("chat", OWNER, allowed), pytest.raises(AcpError):
        await client.complete([{"role": "user", "content": "hi"}], [])
    assert [row[0] for row in usage_rows(app)] == ["subscription"]


@pytest.mark.parametrize("chat_id", [FRIEND, -1009, None])
async def test_no_chat_but_the_owners_dm_ever_starts_the_agent(make_app, tmp_path, chat_id):
    app = make_app()
    config, log = agent_config(app, tmp_path)
    client = acp.acp_clients(config, "drafting", app.dispatch.usage)[0]
    with model_request("chat", chat_id, allowed), pytest.raises(AcpNotAllowed):
        await client.complete([{"role": "user", "content": "hi"}], [])
    assert not log.exists() and usage_rows(app) == []  # nothing was started or recorded


async def test_a_call_outside_any_request_or_with_no_owner_is_refused(make_app, tmp_path):
    app = make_app(owner_id=None)
    config, log = agent_config(app, tmp_path)
    client = acp.acp_clients(config, "drafting", app.dispatch.usage)[0]
    with model_request("chat", None, allowed), pytest.raises(AcpNotAllowed):
        await client.complete([], [])
    with pytest.raises(AcpNotAllowed):  # no request at all
        await client.complete([], [])
    assert not log.exists()


async def test_the_chat_loses_approval_before_the_agent_starts(make_app, tmp_path):
    from dom.usage import ModelCallSkipped

    app = make_app()
    config, log = agent_config(app, tmp_path)
    client = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]

    async def denied():
        return False

    with model_request("escalation", OWNER, denied), pytest.raises(ModelCallSkipped):
        await client.complete([], [])
    assert not log.exists()


async def test_a_tool_call_from_the_agent_reaches_dom_as_an_ordinary_tool_call(make_app, tmp_path):
    app = make_app()
    config, _ = agent_config(app, tmp_path, "tools")
    client = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]
    with model_request("escalation", OWNER, allowed):
        reply = await client.complete([{"role": "user", "content": "remind me"}], TOOLS)
    assert reply["tool_calls"][0]["function"] == {"name": "remind", "arguments": '{"text": "tea"}'}


async def test_claude_is_started_with_its_tools_switched_off_in_the_session(make_app, tmp_path):
    app = make_app()
    config, log = agent_config(app, tmp_path, name="claude")
    client = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]
    with model_request("escalation", OWNER, allowed):
        await client.complete([{"role": "user", "content": "hi"}], [])
    assert events(log, "session_new")[0]["params"]["_meta"] == {"claudeCode": {"options": {"tools": []}}}


async def test_codex_runs_read_only_with_the_shell_tool_off(make_app, tmp_path):
    app = make_app()
    config, log = agent_config(app, tmp_path)
    client = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]
    with model_request("escalation", OWNER, allowed):
        await client.complete([{"role": "user", "content": "hi"}], [])
    start = events(log, "start")[0]
    assert start["initial_agent_mode"] == "read-only" and json.loads(start["codex_config"])["features"] == {"shell_tool": False}


# ---- routing: which roles, switched off by default --------------------------------------------------------------------


def role_config(tmp_path, **agents):
    endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env=None)
    return Config(
        home=tmp_path, owner_id=OWNER, models={role: endpoint for role in ("chat", "fallback", "media", "drafting", "escalation")},
        acp=AcpConfig(**{name: AcpAgent(**value) for name, value in agents.items()}),
    )


def kinds(client):
    return [type(provider).__name__ for provider in client.providers]


async def test_every_agent_is_off_by_default_and_no_role_has_one(tmp_path):
    config = role_config(tmp_path)
    assert [getattr(config.acp, name).enabled for name in ("codex", "claude", "antigravity")] == [False, False, False]
    async with httpx.AsyncClient() as http:
        for role in ("chat", "fallback", "media", "drafting", "escalation"):
            assert "AcpClient" not in kinds(routed_client(config, role, http))


async def test_a_switched_on_agent_is_first_in_escalation_and_drafting_and_nowhere_else(tmp_path):
    config = role_config(tmp_path, claude={"enabled": True}, antigravity={"enabled": False})
    async with httpx.AsyncClient() as http:
        assert kinds(routed_client(config, "escalation", http)) == ["AcpClient", "ChatClient"]
        assert kinds(routed_client(config, "drafting", http))[0] == "AcpClient"
        for role in ("chat", "fallback", "media"):
            assert "AcpClient" not in kinds(routed_client(config, role, http))


async def test_an_agent_can_be_limited_to_one_role_and_each_switch_is_its_own(tmp_path):
    config = role_config(tmp_path, codex={"enabled": True, "roles": ["escalation"]}, claude={"enabled": True})
    async with httpx.AsyncClient() as http:
        assert [p.name for p in routed_client(config, "escalation", http).providers if isinstance(p, AcpClient)] == ["codex", "claude"]
        assert [p.name for p in routed_client(config, "drafting", http).providers if isinstance(p, AcpClient)] == ["claude"]


def test_chat_triage_and_media_cannot_be_named_as_an_agents_role(tmp_path):
    for role in ("chat", "triage", "media", "fallback"):
        with pytest.raises(ValidationError):
            AcpAgent(roles=[role])


async def test_a_friends_drafting_request_falls_through_to_the_next_provider(make_app, tmp_path):
    app = make_app()
    config, log = agent_config(app, tmp_path)
    first = acp.acp_clients(config, "drafting", app.dispatch.usage)[0]
    seen = []

    class Next:
        endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="next")
        address, kind, usage, available = "llm.test", "chat", None, True

        async def complete(self, messages, tools, **kw):
            seen.append(kw["fallback"])
            return {"role": "assistant", "content": "from the next provider"}

    client = FallbackClient(first, Next())
    with model_request("chat", FRIEND, allowed):
        assert (await client.complete([], []))["content"] == "from the next provider"
    assert seen == [True] and not log.exists()


async def test_a_failing_agent_falls_through_to_the_next_provider(make_app, tmp_path):
    app = make_app()
    config, _ = agent_config(app, tmp_path, "crash")
    first = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]

    class Next:
        endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="next")
        address, kind, usage, available = "llm.test", "chat", None, True

        async def complete(self, messages, tools, **kw):
            return {"role": "assistant", "content": "ok"}

    with model_request("escalation", OWNER, allowed):
        assert (await FallbackClient(first, Next()).complete([{"role": "user", "content": "x"}], []))["content"] == "ok"


# ---- the switches: config, secrets, the escalation card, the migration -------------------------------------------------


def test_the_config_reads_each_agent_table_and_names_its_secrets(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "m"\n'
        '[acp.codex]\nenabled = true\nauth_env = ["CODEX_API_KEY"]\ntimeout_seconds = 60\n'
        '[acp.claude]\nhost = "studio-mac"\n'
    )
    config = load_config(tmp_path)
    assert config.acp.codex.enabled and config.acp.codex.timeout_seconds == 60
    assert config.acp.claude.enabled is False and config.acp.antigravity.enabled is False
    assert "CODEX_API_KEY" in config.secret_names()
    assert [name for name, _ in config.acp.agents("escalation")] == ["codex"]


def test_an_unknown_agent_table_is_refused(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "m"\n[acp.gemini]\nenabled = true\n'
    )
    with pytest.raises((ValidationError, ValueError)):
        load_config(tmp_path)


async def test_the_escalation_card_says_the_agent_is_billed_to_the_subscription(make_app, tmp_path):
    from dom.escalation import MODEL_FAILURES, Prices

    app = make_app()
    config, _ = agent_config(app, tmp_path)
    client = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]
    assert AcpError in MODEL_FAILURES
    prices = Prices(lambda coro: asyncio.ensure_future(coro), lambda: 0.0)
    assert (await prices.estimate(client, [], [])).startswith("Billed to your subscription")


def test_the_usage_migration_keeps_rows_and_accepts_the_subscription_kind(tmp_path):
    from dom.store.store import MIGRATIONS, MIGRATIONS_DIR, TARGET_VERSION, migrate_database

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        for version, name in MIGRATIONS:
            if version <= 36:
                conn.executescript((MIGRATIONS_DIR / name).read_text())
        conn.execute(
            "INSERT INTO usage (at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback, settled) "
            "VALUES ('t', 'assistant', 'm', 'h', 111, 1, 2, 0.5, 0, 1)"
        )
    assert migrate_database(db) == TARGET_VERSION >= 37
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT kind, cost_usd, settled FROM usage").fetchall() == [("assistant", 0.5, 1)]
        conn.execute(
            "INSERT INTO usage (at, kind, model, endpoint_host, chat_id, input_tokens, output_tokens, cost_usd, fallback) "
            "VALUES ('t', 'subscription', 'm', 'acp:codex', 111, 0, 0, 0, 0)"
        )
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 2
        assert ("idx_usage_at",) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()


# ---- review rework (v0.8.6 W7): tools off or no agent, group cleanup, hostile output, the request context -------------


def test_every_agent_has_its_tools_switched_off_or_is_refused_outright():
    from dom.config import ACP_NAMES, ACP_NO_TOOLS_OFF

    for name in ACP_NAMES:
        switches = (name in acp.TOOLS_OFF_ENV) + (name in acp.TOOLS_OFF_META) + (name in ACP_NO_TOOLS_OFF)
        assert switches == 1, f"{name}: needs exactly one of tool-off settings or a reason it cannot be enabled"
    assert set(ACP_NO_TOOLS_OFF) == {"antigravity"}


async def test_antigravity_is_refused_before_it_starts_with_one_log_line(make_app, tmp_path, caplog):
    app = make_app()
    config, log = agent_config(app, tmp_path, name="antigravity")
    for role, kind in (("escalation", "escalation"), ("drafting", "chat")):
        client = acp.acp_clients(config, role, app.dispatch.usage)[0]
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="dom.acp"), model_request(kind, OWNER, allowed):
            with pytest.raises(AcpError, match="no way to turn its tools off yet"):
                await client.complete([{"role": "user", "content": "hi"}], [])
        assert [record.message for record in caplog.records if record.name == "dom.acp"] == [
            "antigravity agent not started: no way to turn its tools off yet"]
    assert not log.exists() and usage_rows(app) == []


async def test_antigravity_falls_through_to_the_next_provider(make_app, tmp_path):
    app = make_app()
    config, log = agent_config(app, tmp_path, name="antigravity")
    first = acp.acp_clients(config, "escalation", app.dispatch.usage)[0]

    class Next:
        endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="next")
        address, kind, usage, available = "llm.test", "chat", None, True

        async def complete(self, messages, tools, **kw):
            return {"role": "assistant", "content": "from the next provider"}

    with model_request("escalation", OWNER, allowed):
        assert (await FallbackClient(first, Next()).complete([], []))["content"] == "from the next provider"
    assert not log.exists()


async def test_stopping_a_call_ends_children_that_outlived_the_agent(tmp_path):
    """The leader exits at once but leaves a sleeping grandchild in its process group."""
    pidfile = tmp_path / "grandchild.pid"
    argv = ["sh", "-c", f"sleep 300 >/dev/null 2>&1 & echo $! > {pidfile}; exit 0"]
    process = acp._Process("Fake", argv, ENV, str(tmp_path))
    await process.start()
    await process.proc.wait()
    assert process.proc.returncode == 0
    while not pidfile.exists() or not pidfile.read_text().strip():
        await asyncio.sleep(0.02)
    grandchild = int(pidfile.read_text())
    assert alive(grandchild)
    await process.stop()
    for _ in range(100):  # the dead child is reaped by init a moment later
        if not alive(grandchild):
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the grandchild outlived the call")


HOSTILE_CALLS = [
    {"tool_calls": [{"name": []}]},
    {"tool_calls": [{"name": {}}]},
    {"tool_calls": [{"name": None}]},
    {"tool_calls": [{"name": 5}]},
    {"tool_calls": [{}]},
    {"tool_calls": ["remind"]},
    {"tool_calls": [None]},
    {"tool_calls": [["remind"]]},
    {"tool_calls": [{"name": "remind", "arguments": []}]},
    {"tool_calls": [{"name": "remind", "arguments": 5}]},
    {"tool_calls": [{"name": "remind", "arguments": None}]},
    {"tool_calls": [{"name": "remind", "arguments": "not json"}]},
    {"tool_calls": [{"name": "remind", "arguments": "[1, 2]"}]},
    {"tool_calls": [{"name": "remind", "arguments": '"text"'}]},
    {"tool_calls": [{"name": "remind", "arguments": {"text": "tea"}}, {"name": ["remind"]}]},  # one bad call spoils the batch
    {"tool_calls": [{"name": "remind", "arguments": {"text": "tea"}}, {"name": "not_offered"}]},
    {"tool_calls": "remind"},
    {"tool_calls": {"name": "remind"}},
    {"tool_calls": 7},
    {"tool_calls": None},
    {"tool_calls": []},
]


@pytest.mark.parametrize("data", HOSTILE_CALLS, ids=lambda data: json.dumps(data)[:70])
def test_malformed_tool_calls_fall_through_as_text(data, caplog):
    text = json.dumps(data)
    with caplog.at_level(logging.INFO, logger="dom.acp"):
        assert as_message(text, TOOLS) == {"role": "assistant", "content": text}
    assert sum(record.name == "dom.acp" for record in caplog.records) == 1  # one line says why


def test_tool_call_arguments_may_be_an_object_or_a_json_string():
    for arguments in ({"text": "tea"}, '{"text": "tea"}'):
        message = as_message(json.dumps({"tool_calls": [{"name": "remind", "arguments": arguments}]}), TOOLS)
        assert message["tool_calls"][0]["function"] == {"name": "remind", "arguments": '{"text": "tea"}'}
    message = as_message('{"tool_calls": [{"name": "remind"}]}', TOOLS)  # no arguments is an empty object
    assert message["tool_calls"][0]["function"]["arguments"] == "{}"


async def refused_in(make_app, tmp_path, role, kind, chat_id, *, unprompted=False):
    app = make_app()
    config, log = agent_config(app, tmp_path)
    client = acp.acp_clients(config, role, app.dispatch.usage)[0]
    with model_request(kind, chat_id, allowed, unprompted=unprompted):
        try:
            await client.complete([{"role": "user", "content": "hi"}], [])
        except AcpNotAllowed:
            assert not log.exists() and usage_rows(app) == []  # refused before anything started or was recorded
            return True
    return False


@pytest.mark.parametrize("kind", [
    "desktop", "reflection", "memory", "assistant", "triage", "media", "search", "guard", "embedding", "subscription",
    "studio_image", "studio_voice", "studio_video",
])
@pytest.mark.parametrize("role", ["escalation", "drafting"])
async def test_background_work_under_the_owners_chat_id_never_reaches_an_agent(make_app, tmp_path, role, kind):
    assert await refused_in(make_app, tmp_path, role, kind, OWNER)


async def test_the_desktop_skill_drafting_call_is_refused_and_falls_through(make_app, tmp_path):
    """Desktop skill publishing drafts under a `desktop` request for the owner's chat (dom/desktop/worker.py)."""
    assert await refused_in(make_app, tmp_path, "drafting", "desktop", OWNER)


async def test_the_chat_kinds_do_not_cross_roles(make_app, tmp_path):
    assert await refused_in(make_app, tmp_path, "escalation", "chat", OWNER)  # an escalation is only an escalation request


@pytest.mark.parametrize("role,kind", [("escalation", "escalation"), ("drafting", "chat"), ("drafting", "escalation")])
async def test_an_owner_dm_turns_escalation_and_drafting_are_served(make_app, tmp_path, role, kind):
    assert not await refused_in(make_app, tmp_path, role, kind, OWNER)


@pytest.mark.parametrize("role,kind", [("escalation", "escalation"), ("drafting", "chat"), ("drafting", "escalation")])
async def test_a_turn_dom_started_himself_in_the_owners_dm_never_reaches_an_agent(make_app, tmp_path, role, kind):
    """An opener or a noticing runs a tool-capable owner-DM turn that no message asked for (dom/openers.py)."""
    assert await refused_in(make_app, tmp_path, role, kind, OWNER, unprompted=True)


def test_the_remote_directory_is_removed_even_when_the_agent_leaves_files_in_it(tmp_path):
    script = tmp_path / "litter.py"
    script.write_text("open('left-behind.txt', 'w').write('x')\n")
    argv, _, session_cwd = launch(cfg(tmp_path, claude={"enabled": True, "host": "mac", "command": sys.executable, "args": [str(script)]}), "claude")
    assert subprocess.run(["sh", "-c", argv[-1]], check=True).returncode == 0
    assert not Path(session_cwd).exists()


# ---- an agent pinned for escalation that fails goes on to the cloud provider -------------------------------------------


def escalation_app(make_app, http, tmp_path, **agents):
    """An app whose escalation route is the given agents first, then one cloud provider named `cloud`."""
    app = make_app()
    local = ModelEndpoint(base_url="http://localhost/v1", model="local")
    cloud = ModelEndpoint(base_url="https://llm.test/v1", model="cloud")
    app.config = app.config.model_copy(update={
        "models": {"chat": local, "escalation": cloud},
        "acp": AcpConfig(**{
            name: AcpAgent(**{key: item for key, item in value.items() if key != "quick"}).model_copy(
                update={"timeout_seconds": 0.3} if value.get("quick") else {})  # the setting's own minimum is 5 s
            for name, value in agents.items()
        }),
    })
    app.client = FallbackClient(ChatClient(local, None, http, sleep=no_sleep, usage=app.dispatch.usage), None)
    app.escalation_client = routed_client(app.config, "escalation", http, app.dispatch.usage)
    return app


def fake_agent(tmp_path, mode, **extra):
    return {"enabled": True, "command": sys.executable, "args": (FAKE, mode, str(tmp_path / "agent.jsonl")), **extra}


@pytest.mark.parametrize("failure", ["refused", "missing", "signed_out", "timeout", "crash"])
async def test_a_failed_pinned_agent_goes_on_to_the_cloud_provider_and_tells_the_owner(make_app, tmp_path, failure):
    asked = []

    async def respond(request):
        asked.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "from cloud"}}]})

    agent = {
        "refused": fake_agent(tmp_path, "empty"),
        "missing": {"enabled": True, "command": str(tmp_path / "no-such-agent")},
        "signed_out": fake_agent(tmp_path, "signed_out"),
        "timeout": fake_agent(tmp_path, "hang", quick=True),
        "crash": fake_agent(tmp_path, "crash"),
    }[failure]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app = escalation_app(make_app, http, tmp_path, codex=agent)
        with app.dispatch.escalation.turn(OWNER, "owner_dm", app.client) as turn:
            assert isinstance(turn.provider, AcpClient)  # the card shows the agent
            turn.choice = "switch"
            with model_request("escalation", OWNER, allowed):
                reply = await turn.complete(app.client, [{"role": "user", "content": "hi"}], [])
            assert turn.pinned.then.endpoint.model == "cloud"
        await drain(app)
    assert reply["content"] == "from cloud" and asked == ["cloud"]
    [note] = [call for call in app.bot.of("send", OWNER) if "did not answer" in call[2]]
    assert "Codex subscription" in note[2] and "cloud" in note[2]
    assert [row[0] for row in usage_rows(app)] == ["subscription", "escalation"]


async def test_a_pinned_agent_that_answers_never_reaches_the_cloud_provider(make_app, tmp_path):
    async def respond(request):
        raise AssertionError("the cloud provider was called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app = escalation_app(make_app, http, tmp_path, codex=fake_agent(tmp_path, "ok"))
        with app.dispatch.escalation.turn(OWNER, "owner_dm", app.client) as turn:
            turn.choice = "switch"
            with model_request("escalation", OWNER, allowed):
                reply = await turn.complete(app.client, [{"role": "user", "content": "hi"}], [])
        await drain(app)
    assert reply["content"] == "Hello, owner." and not app.bot.of("send", OWNER)


async def test_a_cloud_provider_pinned_for_escalation_still_fails_alone(make_app, tmp_path):
    asked = []

    async def respond(request):
        asked.append(json.loads(request.content)["model"])
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app = escalation_app(make_app, http, tmp_path)
        second = ChatClient(ModelEndpoint(base_url="https://other.test/v1", model="second"), None, http, sleep=no_sleep)
        app.escalation_client = FallbackClient(app.escalation_client.primary, second)
        with app.dispatch.escalation.turn(OWNER, "owner_dm", app.client) as turn:
            turn.choice = "switch"
            assert turn.pinned.then is None
            with model_request("escalation", OWNER, allowed), pytest.raises(NoFinalAnswer):
                await turn.complete(app.client, [{"role": "user", "content": "hi"}], [])
    assert set(asked) == {"cloud"}


async def test_antigravity_enabled_by_hand_is_never_pinned_or_counted_available(make_app, tmp_path):
    async with httpx.AsyncClient() as http:
        app = escalation_app(make_app, http, tmp_path, antigravity={"enabled": True})
        agent = app.escalation_client.providers[0]
        assert isinstance(agent, AcpClient) and agent.name == "antigravity" and not agent.available
        with app.dispatch.escalation.turn(OWNER, "owner_dm", app.client) as turn:
            assert turn.provider.endpoint.model == "cloud" and turn.pinned.then is None
    assert not AcpClient(app.config.model_copy(update={"acp": AcpConfig()}), "codex", "escalation").available  # off


async def drain(app):
    from tghelpers import drain_dispatch
    await drain_dispatch(app.dispatch)
