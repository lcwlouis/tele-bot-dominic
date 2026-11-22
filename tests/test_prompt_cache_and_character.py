"""Tests for W5 (prompt cache: fixed-shape tool list, deferred note/memory swaps, read_notes)
and W14 (in character about Dom's machinery, coarse availability note, owner-DM timings in self_inspect)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from tghelpers import FINAL, FRIEND, GROUP, OWNER, drain_dispatch, tg, turn_requests

from dom import window as chat_window
from dom.llm import ChatClient
from dom.models import StoredMessage
from dom.plugins.manifest import Manifest, Plugin, ToolManifest
from dom.presence import parse_habits
from dom.prompt import FRAME, online_window_note
from dom.tools import READ_NOTES_TOOL, SELF_INSPECT_TOOL, TurnCtx, enabled_tools
from dom.traits import AUTO, GLOBAL_PRESET_KEY
from tghelpers import no_sleep

BASE = datetime(2025, 12, 1, 10, 0, tzinfo=timezone.utc)
OWNER_ONLY_NAMES = {
    "remind", "draft_plugin", "write_brief", "self_inspect", "own_code",
    "find_devices", "task_mode", "start_desktop_task",
    "coding_overview", "coding_thread", "coding_search", "coding_send",
}


def _ctx(app, scope="owner_dm", chat_id=OWNER, sender_id=OWNER, participant_ids=()):
    return TurnCtx(
        scope, chat_id, sender_id, app.home, app.store, OWNER, app=app, participant_ids=tuple(participant_ids),
    )


def _msg(n: int, *, chat_id: int = OWNER, sender: int = OWNER, size: int = 20, text: str | None = None):
    return StoredMessage(
        chat_id=str(chat_id),
        msg_id=n,
        sender_user_id=sender,
        sender_name="Owner" if sender == OWNER else "Fran",
        role="user",
        visibility_scope=f"chat:{chat_id}",
        text=text if text is not None else f"msg-{n} " + "a" * size,
        created_at=(BASE + timedelta(minutes=n)).isoformat(),
    )


def _write(app, rel: str, text: str) -> None:
    path = app.home.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


async def _noop() -> None:
    return None


def _fake_plugin(name: str, tool_name: str, scopes: tuple[str, ...]) -> tuple[Plugin, ToolManifest]:
    tool = ToolManifest(
        name=tool_name,
        description=f"Plugin {name} {tool_name}",
        parameters={"type": "object", "properties": {}},
        scopes=scopes,
    )
    manifest = Manifest(
        name=name, description=f"Plugin {name}", version="1.0.0",
        network=False, timeout_seconds=10, memory_mb=64, tools=(tool,),
    )
    return Plugin(manifest=manifest, files={}, hash="0" * 64), tool


# ---- W5: fixed-shape tool list -------------------------------------------------------------


async def test_tool_list_puts_shared_first_owner_last_and_keeps_scopes_separate(make_app, monkeypatch):
    app = make_app()
    app.desktop_worker = SimpleNamespace(available=lambda: True, close=_noop)
    app.t3 = SimpleNamespace(available=lambda: True)
    await app.store.kv_set(GLOBAL_PRESET_KEY, AUTO)

    shared_plugin = _fake_plugin("weather", "forecast", ("owner_dm", "friend_dm", "group"))
    owner_plugin = _fake_plugin("alpha", "secret", ("owner_dm",))

    async def fake_entries(scope: str):
        # Intentionally return in unsorted order so sorting is exercised.
        items = [owner_plugin, shared_plugin]
        return [(p, t) for p, t in items if scope in t.scopes]

    monkeypatch.setattr(app.plugins, "entries", fake_entries)
    monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)

    group_tools = await enabled_tools(app, "group")
    friend_tools = await enabled_tools(app, "friend_dm")
    owner_tools = await enabled_tools(app, "owner_dm")

    group_names = [t.name for t in group_tools]
    friend_names = [t.name for t in friend_tools]
    owner_names = [t.name for t in owner_tools]

    # Groups and friend DMs never see any owner-only tool names (core or plugin).
    assert not (set(group_names) & OWNER_ONLY_NAMES)
    assert not (set(friend_names) & OWNER_ONLY_NAMES)
    assert "plugin_alpha_secret" not in group_names
    assert "plugin_alpha_secret" not in friend_names

    # Owner DM starts with the exact shared prefix in the exact same order and schemas.
    assert group_names == friend_names
    assert owner_names[: len(group_names)] == group_names
    assert [t.openai_schema() for t in owner_tools[: len(group_tools)]] == [
        t.openai_schema() for t in group_tools
    ]

    # Owner-only tools come strictly after shared tools, with dynamic tools after core owner tools.
    owner_tail = owner_names[len(group_names):]
    assert "remind" in owner_tail and "self_inspect" in owner_tail
    assert owner_tail.index("remind") < owner_tail.index("start_desktop_task")
    assert owner_tail.index("start_desktop_task") < owner_tail.index("coding_overview")
    assert owner_tail.index("coding_send") < owner_tail.index("plugin_alpha_secret")


async def test_dynamic_tools_coming_and_going_do_not_reshape_shared_part(make_app, monkeypatch):
    app = make_app()
    desktop_on = False
    t3_on = False
    plugin_entries: list[tuple[Plugin, ToolManifest]] = []

    app.desktop_worker = SimpleNamespace(available=lambda: desktop_on, close=_noop)
    app.t3 = SimpleNamespace(available=lambda: t3_on)

    async def fake_entries(scope: str):
        return [(p, t) for p, t in plugin_entries if scope in t.scopes]

    monkeypatch.setattr(app.plugins, "entries", fake_entries)
    monkeypatch.setattr(app.plugin_runner, "self_test", lambda: True)

    base_shared = [t.openai_schema() for t in await enabled_tools(app, "group")]
    base_owner_all = await enabled_tools(app, "owner_dm")
    assert [t.openai_schema() for t in base_owner_all[: len(base_shared)]] == base_shared
    core_owner_schemas = [t.openai_schema() for t in base_owner_all[len(base_shared):]]

    # 1) Toggle desktop on -> shared prefix and core owner-only tools stay identical.
    desktop_on = True
    after_desktop = await enabled_tools(app, "owner_dm")
    assert [t.openai_schema() for t in after_desktop[: len(base_shared)]] == base_shared
    assert [t.openai_schema() for t in after_desktop[len(base_shared): len(base_shared) + len(core_owner_schemas)]] == core_owner_schemas

    # 2) Toggle T3 on -> shared prefix and core owner-only tools still identical.
    t3_on = True
    after_t3 = await enabled_tools(app, "owner_dm")
    assert [t.openai_schema() for t in after_t3[: len(base_shared)]] == base_shared
    assert [t.openai_schema() for t in after_t3[len(base_shared): len(base_shared) + len(core_owner_schemas)]] == core_owner_schemas

    # 3) Add an owner-only plugin -> shared prefix and core owner-only tools still identical.
    plugin_entries.append(_fake_plugin("ztool", "run", ("owner_dm",)))
    after_owner_plugin = await enabled_tools(app, "owner_dm")
    assert [t.openai_schema() for t in after_owner_plugin[: len(base_shared)]] == base_shared
    assert [t.openai_schema() for t in after_owner_plugin[len(base_shared): len(base_shared) + len(core_owner_schemas)]] == core_owner_schemas
    assert [t.openai_schema() for t in await enabled_tools(app, "group")] == base_shared

    # 4) Toggle vibe switch on and add a shared plugin -> fixed shared core prefix never reorders or reshapes.
    await app.store.kv_set(GLOBAL_PRESET_KEY, AUTO)
    plugin_entries.append(_fake_plugin("calc", "add", ("owner_dm", "friend_dm", "group")))
    group_now = await enabled_tools(app, "group")
    owner_now = await enabled_tools(app, "owner_dm")
    assert [t.openai_schema() for t in group_now[: len(base_shared)]] == base_shared
    assert [t.openai_schema() for t in owner_now[: len(group_now)]] == [t.openai_schema() for t in group_now]
    assert group_now[len(base_shared)].name == "switch_vibe"
    assert group_now[len(base_shared) + 1].name == "plugin_calc_add"


# ---- W5: prompt hashes stay stable until trim / hourly refresh ------------------------------


async def test_hashes_hold_when_notes_and_memory_change_until_trim_and_hour(make_app):
    app = make_app(contents=(FINAL, FINAL, FINAL, FINAL))
    app.config = app.config.model_copy(update={"window_budget_tokens": 2200, "window_keep_tokens": 1300})
    await app.store.set_chat_auth_status(OWNER, "approved", title="Owner")
    await chat_window.measure(app.store, OWNER, [], 100)

    _write(app, "memory/MEMORY.md", "- world v1\n")
    _write(app, "private/USER.md", "- user v1\n")
    _write(app, "private/NOTES.md", "- notes v1\n")
    _write(app, f"memory/chats/{OWNER}.md", "- chat v1\n")
    _write(app, f"memory/people/{OWNER}.md", "- person v1\n")

    await app.dispatch.presence.wake(OWNER, parse_habits(""))
    await app.store.record_new_message(_msg(1, size=200))
    await app.store.advance_memory_cursor(OWNER, 1)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    # Mutate every note file + shared MEMORY.md between Turn 1 and Turn 2 (within 1 hour, no trim).
    _write(app, "memory/MEMORY.md", "- world v2\n")
    _write(app, "private/USER.md", "- user v2\n")
    _write(app, "private/NOTES.md", "- notes v2\n")
    _write(app, f"memory/chats/{OWNER}.md", "- chat v2\n")
    _write(app, f"memory/people/{OWNER}.md", "- person v2\n")

    app.dispatch.now.advance(minutes=5)
    await app.store.record_new_message(_msg(2, size=200))
    await app.store.advance_memory_cursor(OWNER, 2)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    # Push past budget to trigger a window trim on Turn 3 (still within 1 hour of Turn 1).
    for n in range(3, 25):
        await app.store.record_new_message(_msg(n, size=350))
    # The window is in arrival order, so Dom's own earlier bubbles (the fake bot numbers them from 5001) sit among
    # the rows a trim would drop, and the memory pass must have read past them too.
    await app.store.advance_memory_cursor(OWNER, 5002)
    app.dispatch.now.advance(minutes=5)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    # Advance past 1 hour from Turn 1 -> MEMORY.md refreshes on Turn 4.
    app.dispatch.now.advance(minutes=55)
    await app.dispatch.presence.wake(OWNER, parse_habits(""))
    await app.store.record_new_message(_msg(25, size=50))
    await app.store.advance_memory_cursor(OWNER, 25)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    rows = list(reversed(await app.store.recent_turns(str(OWNER))))
    assert len(rows) == 4
    calls = [json.loads(row["model_calls"])[0] for row in rows]
    reqs = turn_requests(app)

    # Turn 1 -> Turn 2 (notes & MEMORY.md changed, no trim): all three prefix hashes stay identical!
    assert calls[0]["system_hash"] == calls[1]["system_hash"]
    assert calls[0]["tools_hash"] == calls[1]["tools_hash"]
    assert calls[0]["user_prefix_hash"] == calls[1]["user_prefix_hash"]
    assert calls[0]["user_prefix_hash"] is not None
    assert "- notes v1" in reqs[1]["messages"][1]["content"]
    assert "- notes v2" not in reqs[1]["messages"][1]["content"]
    assert "notes updated since " in reqs[1]["messages"][1]["content"].rsplit("</chat_window>", 1)[1]
    assert "read_notes" in reqs[1]["messages"][1]["content"].rsplit("</chat_window>", 1)[1]

    # Turn 3 (after window trim, still < 1 hour): per-chat notes swapped in, MEMORY.md still cached!
    assert calls[2]["system_hash"] == calls[0]["system_hash"]
    assert calls[2]["tools_hash"] == calls[0]["tools_hash"]
    assert calls[2]["user_prefix_hash"] != calls[1]["user_prefix_hash"]
    assert "- notes v2" in reqs[2]["messages"][1]["content"]
    assert "- world v1" in reqs[2]["messages"][0]["content"]
    # MEMORY.md is still behind disk, so the turn note still mentions updated notes.
    assert "notes updated since " in reqs[2]["messages"][1]["content"].rsplit("</chat_window>", 1)[1]

    # Turn 4 (> 1 hour later): MEMORY.md refreshes, nothing is stale anymore.
    assert calls[3]["system_hash"] != calls[0]["system_hash"]
    assert calls[3]["tools_hash"] == calls[0]["tools_hash"]
    assert "- world v2" in reqs[3]["messages"][0]["content"]
    assert "notes updated since " not in reqs[3]["messages"][1]["content"]


async def test_group_user_prefix_hash_holds_even_when_new_participant_joins_before_trim(make_app):
    app = make_app(contents=(FINAL, FINAL, FINAL))
    await app.store.set_chat_auth_status(GROUP, "approved", title="Study Group")
    _write(app, f"memory/chats/{GROUP}.md", "- group note v1\n")
    _write(app, f"memory/people/{FRIEND}.md", "- fran v1\n")
    _write(app, "memory/people/333.md", "- sam fresh note\n")

    await app.dispatch.presence.wake(GROUP, parse_habits(""))
    await app.store.record_new_message(_msg(1, chat_id=GROUP, sender=FRIEND, text="first"))
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    # New participant (333) joins with non-empty notes and chat notes change (within 1 hour, no trim).
    _write(app, f"memory/chats/{GROUP}.md", "- group note v2\n")
    app.dispatch.now.advance(minutes=3)
    await app.store.record_new_message(_msg(2, chat_id=GROUP, sender=333, text="joining in"))
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    # After 1 hour (slow cadence, no trim), notes swap in including 333's notes.
    app.dispatch.now.advance(minutes=65)
    await app.dispatch.presence.wake(GROUP, parse_habits(""))
    await app.store.record_new_message(_msg(3, chat_id=GROUP, sender=333, text="an hour later"))
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)

    rows = list(reversed(await app.store.recent_turns(str(GROUP))))
    calls = [json.loads(row["model_calls"])[0] for row in rows]
    reqs = turn_requests(app)

    assert calls[0]["system_hash"] == calls[1]["system_hash"]
    assert calls[0]["tools_hash"] == calls[1]["tools_hash"]
    assert calls[0]["user_prefix_hash"] == calls[1]["user_prefix_hash"]
    assert "notes updated since " in reqs[1]["messages"][1]["content"].rsplit("</chat_window>", 1)[1]

    # Hourly cadence swapped the fresh notes in.
    assert calls[2]["user_prefix_hash"] != calls[1]["user_prefix_hash"]
    assert "- group note v2" in reqs[2]["messages"][1]["content"]
    assert "- sam fresh note" in reqs[2]["messages"][1]["content"]
    assert "notes updated since " not in reqs[2]["messages"][1]["content"]


# ---- W5: read_notes scope-filtered mid-turn read -------------------------------------------


async def test_read_notes_returns_fresh_notes_and_enforces_private_boundary(make_app):
    app = make_app()
    _write(app, "memory/MEMORY.md", "- shared fact fresh\n")
    _write(app, "private/USER.md", "- OWNER-PRIVATE-USER\n")
    _write(app, "private/NOTES.md", "- OWNER-PRIVATE-NOTES\n")
    _write(app, f"memory/chats/{OWNER}.md", "- owner chat note\n")
    _write(app, f"memory/chats/{GROUP}.md", "- group chat note\n")
    _write(app, f"memory/chats/{FRIEND}.md", "- friend chat note\n")
    _write(app, f"memory/people/{FRIEND}.md", "- friend person note\n")
    _write(app, "memory/people/333.md", "- stranger person note\n")

    # Owner DM sees private notes + shared memory + owner chat notes.
    owner_out = await READ_NOTES_TOOL.handler({}, _ctx(app, "owner_dm", OWNER, OWNER, (OWNER,)))
    assert "OWNER-PRIVATE-USER" in owner_out
    assert "OWNER-PRIVATE-NOTES" in owner_out
    assert "shared fact fresh" in owner_out
    assert "owner chat note" in owner_out
    assert "group chat note" not in owner_out

    # Group NEVER sees private notes — even when the sender is the owner!
    group_out = await READ_NOTES_TOOL.handler({}, _ctx(app, "group", GROUP, OWNER, (FRIEND, OWNER)))
    assert "OWNER-PRIVATE-USER" not in group_out
    assert "OWNER-PRIVATE-NOTES" not in group_out
    assert "private/" not in group_out
    assert "shared fact fresh" in group_out
    assert "group chat note" in group_out
    assert "friend person note" in group_out
    assert "stranger person note" not in group_out
    assert "friend chat note" not in group_out

    # Friend DM NEVER sees private notes or other chats.
    friend_out = await READ_NOTES_TOOL.handler({}, _ctx(app, "friend_dm", FRIEND, FRIEND, (FRIEND,)))
    assert "OWNER-PRIVATE-USER" not in friend_out
    assert "OWNER-PRIVATE-NOTES" not in friend_out
    assert "private/" not in friend_out
    assert "friend chat note" in friend_out
    assert "friend person note" in friend_out
    assert "group chat note" not in friend_out

    # Spoofed scope="owner_dm" with wrong chat_id never leaks private notes.
    spoofed = await READ_NOTES_TOOL.handler({}, _ctx(app, "owner_dm", GROUP, FRIEND, (FRIEND,)))
    assert "OWNER-PRIVATE-USER" not in spoofed
    assert "OWNER-PRIVATE-NOTES" not in spoofed


async def test_mid_turn_read_notes_tool_call_gets_fresh_notes_while_prefix_stays_cached(make_app):
    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_notes", "arguments": "{}"},
        }],
    }
    responses = [
        {"choices": [{"message": {"role": "assistant", "content": FINAL}}]},
        {"choices": [{"message": tool_call}]},
        {"choices": [{"message": {"role": "assistant", "content": FINAL}}]},
    ]
    recorded: list[dict] = []

    def handler(request):
        recorded.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    app = make_app()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        app.client = ChatClient(app.config.models["chat"], None, http, sleep=no_sleep)
        await app.store.set_chat_auth_status(OWNER, "approved", title="Owner")
        _write(app, "private/NOTES.md", "- old note\n")
        await app.dispatch.presence.wake(OWNER, parse_habits(""))
        await app.store.record_new_message(_msg(1, text="hi"))
        await app.dispatch.tick()
        await drain_dispatch(app.dispatch)

        _write(app, "private/NOTES.md", "- fresh note written mid-conversation\n")
        app.dispatch.now.advance(minutes=2)
        await app.store.record_new_message(_msg(2, text="did you remember that new thing?"))
        await app.dispatch.tick()
        await drain_dispatch(app.dispatch)

    assert len(recorded) == 3
    # Turn 2's first call keeps the old prompt prefix, then tool result delivers the fresh note.
    assert "- old note" in recorded[1]["messages"][1]["content"]
    assert "- fresh note written mid-conversation" not in recorded[1]["messages"][1]["content"]
    tool_msg = next(m for m in recorded[2]["messages"] if m.get("role") == "tool")
    assert "- fresh note written mid-conversation" in tool_msg["content"]


# ---- W14: in character about machinery (E46) & owner-DM self_inspect timings ---------------


@pytest.mark.parametrize(("minutes", "expected"), [
    (0.0, "availability: you're about to head off"),
    (1.5, "availability: you're about to head off"),
    (5.0, "availability: you'll be heading off soon"),
    (9.1, "availability: you'll be heading off soon"),
    (15.0, "availability: you're around for a bit longer"),
    (45.0, "availability: you're around for a while"),
])
def test_online_window_note_is_coarse_and_never_quotable_minutes(minutes, expected):
    line = online_window_note(minutes)
    assert line == expected
    assert "minute" not in line.lower()
    assert not re.search(r"\d", line)


@pytest.mark.parametrize("scope,chat_id,sender,away,remaining_min,late", [
    ("group", GROUP, FRIEND, 0, 1.5, False),
    ("group", GROUP, FRIEND, 12, 9.1, False),
    ("friend_dm", FRIEND, FRIEND, 0, 15.0, True),
    ("owner_dm", OWNER, OWNER, 4, 40.0, False),
])
async def test_replay_over_sample_turn_notes_carries_machinery_rule_and_no_countdown(
    make_app, scope, chat_id, sender, away, remaining_min, late,
):
    app = make_app(contents=(FINAL,))
    await app.store.set_chat_auth_status(chat_id, "approved", title="Sample Chat")
    now = app.dispatch.now()
    msg_time = now - timedelta(minutes=5 if late else 0)
    stored = StoredMessage(
        chat_id=str(chat_id), msg_id=1, sender_user_id=sender, sender_name="Alex", role="user",
        visibility_scope=f"chat:{chat_id}", text="hey are you still around?", created_at=msg_time.isoformat(),
    )
    await app.store.record_new_message(stored)
    until = now + timedelta(minutes=remaining_min)

    await app.dispatch._compose_turn(
        chat_id, scope, stored, None, [stored], until, away, False, parse_habits(""),
    )
    req = turn_requests(app)[-1]
    system_text = req["messages"][0]["content"]
    user_text = req["messages"][1]["content"]
    turn_note = user_text.rsplit("</chat_window>", 1)[1]

    # FRAME carries the E46 machinery rule in every scope's system prompt.
    assert FRAME in system_text
    lower_frame = FRAME.lower()
    for term in ("machinery", "online minutes left", "restarts", "versions", "timings", "tool names", "in-life reason"):
        assert term in lower_frame
    assert "owner's dm" in lower_frame and "answer plainly" in lower_frame

    # Turn note keeps what Dom needs (availability + arrivals while away) without quotable countdowns.
    assert "state: online" in turn_note
    assert f"messages that arrived while away: {away}" in turn_note
    assert online_window_note(remaining_min) in turn_note
    assert "online minutes left" not in turn_note
    assert f"{remaining_min:.1f}" not in turn_note


async def test_self_inspect_returns_recent_timings_only_in_owner_dm(make_app):
    app = make_app()
    now = app.dispatch.now()
    await app.store.record_turn(
        now - timedelta(minutes=2), GROUP, "group", 1,
        ["triage", "final", "debounce@1200", "triage@300", "model@4100", "typing@600", "send@150"],
        "chat-model", False, 6350, "Replied in 1 bubble",
        model_calls=[{
            "kind": "chat", "model": "chat-model", "fallback": False,
            "system_hash": "aaa", "tools_hash": "bbb", "user_hash": "ccc", "user_prefix_hash": "ddd",
            "duration_ms": 4100, "status": 200, "input_tokens": 9000, "output_tokens": 45, "cached_tokens": 8200,
        }],
        first_bubble_ms=6200, last_bubble_ms=6350,
    )
    await app.store.record_turn(
        now, OWNER, "owner_dm", 2,
        ["final", "debounce@2500", "model@9000", "fallback_model@1800", "typing@400"],
        "fallback-model", True, 13700, "Replied in 2 bubbles",
        model_calls=[
            {
                "kind": "chat", "model": "chat-model", "fallback": False,
                "system_hash": "aaa", "tools_hash": "bbb", "user_hash": "eee", "user_prefix_hash": "ddd",
                "duration_ms": 9000, "status": 503, "input_tokens": None, "output_tokens": None, "cached_tokens": None,
            },
            {
                "kind": "chat", "model": "fallback-model", "fallback": True,
                "system_hash": "aaa", "tools_hash": "bbb", "user_hash": "eee", "user_prefix_hash": "ddd",
                "duration_ms": 1800, "status": 200, "input_tokens": 9200, "output_tokens": 60, "cached_tokens": 0,
            },
        ],
        first_bubble_ms=13300, last_bubble_ms=13700,
    )

    # Without include_timings, recent_timings is omitted (keeps normal self_inspect compact).
    plain = json.loads(await SELF_INSPECT_TOOL.handler({}, _ctx(app)))
    assert "recent_timings" not in plain

    # With include_timings=True in owner DM, timings come back newest first without chat_id.
    detailed = json.loads(await SELF_INSPECT_TOOL.handler({"include_timings": True, "limit": 5}, _ctx(app)))
    timings = detailed["recent_timings"]
    assert len(timings) == 2
    assert timings[0]["scope"] == "owner_dm"
    assert timings[0]["fallback"] is True
    assert timings[0]["first_bubble_ms"] == 13300
    assert timings[0]["stages_ms"] == {"debounce": 2500, "model": 9000, "fallback_model": 1800, "typing": 400}
    assert timings[0]["model_calls"][0]["status"] == 503
    assert timings[1]["model_calls"][0]["cached_tokens"] == 8200
    assert all("chat_id" not in row for row in timings)

    # Limit parameter caps the returned rows.
    one = json.loads(await SELF_INSPECT_TOOL.handler({"include_timings": True, "limit": 1}, _ctx(app)))
    assert len(one["recent_timings"]) == 1

    # Non-owner scopes never get timings (nor anything else from self_inspect).
    for scope, cid, sid in (("group", GROUP, FRIEND), ("group", GROUP, OWNER), ("friend_dm", FRIEND, FRIEND)):
        denied = await SELF_INSPECT_TOOL.handler({"include_timings": True}, _ctx(app, scope, cid, sid))
        assert denied.startswith("error:")
        assert "recent_timings" not in denied
        assert "13300" not in denied

    assert "include_timings" in SELF_INSPECT_TOOL.description
    assert "why a reply was slow" in SELF_INSPECT_TOOL.description
