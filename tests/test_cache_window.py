"""The chat window grows until the prompt reaches a token budget, then one trim drops the oldest messages."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from tghelpers import FINAL, FRIEND, GROUP, drain_dispatch, turn_requests

from dom import turn_log
from dom import window as chat_window
from dom.config import Config
from dom.models import StoredMessage
from dom.presence import parse_habits
from dom.store import Store
from dom.window import DEFAULT_FIXED, MIN_KEPT, estimate_tokens

BASE = datetime(2025, 12, 1, tzinfo=timezone.utc)
BUDGET, KEEP = 3000, 1800


def message(n, size=300, chat_id=GROUP):
    return StoredMessage(
        chat_id=str(chat_id), msg_id=n, sender_user_id=FRIEND, sender_name="Fran", role="user",
        visibility_scope=f"chat:{chat_id}", text=f"m{n} " + "x" * size, created_at=(BASE + timedelta(minutes=n)).isoformat(),
    )


async def add(app, n, size=300, chat_id=GROUP):
    await app.store.record_new_message(message(n, size, chat_id))


def small(app, budget=BUDGET, keep=KEEP, **more):
    app.config = app.config.model_copy(update={"window_budget_tokens": budget, "window_keep_tokens": keep, **more})
    return app.config


async def measured(app, fixed=100, chat_id=GROUP):
    await chat_window.measure(app.store, chat_id, [], fixed + 0)  # the prompt outside the window, as the model reported it


async def read(app, through=None):
    return await chat_window.load(app.store, GROUP, app.config, through_id=through)


async def processed(app, n):
    await app.store.advance_memory_cursor(GROUP, n)


def test_a_message_is_estimated_at_a_token_per_three_bytes():
    assert estimate_tokens(message(1, 300)) == -(-(100 + 303 + 4) // 3)
    cjk = StoredMessage(chat_id="1", text="你好" * 100, visibility_scope="x", created_at="")
    assert estimate_tokens(cjk) == -(-(100 + 600) // 3)  # a CJK character is about a token, not a third of one


def test_the_marks_must_be_ordered():
    with pytest.raises(ValueError):
        Config(home=".", window_budget_tokens=5000, window_keep_tokens=5000)


async def test_nothing_is_trimmed_below_the_budget_however_many_messages(make_app):
    app = make_app()
    small(app)
    await measured(app)
    for n in range(1, 8):  # 7 x ~135 tokens + 100 fixed, under the 3000 budget
        await add(app, n)
    window = await read(app)
    assert [m.msg_id for m in window.rows] == list(range(1, 8)) and not window.blocked and window.tokens < BUDGET
    app.config = app.config.model_copy(update={"window_budget_tokens": 100_000, "window_keep_tokens": 90_000})
    for n in range(8, 120):  # far past the old 80 messages: no sliding
        await add(app, n)
    assert [m.msg_id for m in (await read(app)).rows] == list(range(1, 120))


async def test_one_trim_at_the_budget_down_to_the_watermark_then_the_start_holds_still(make_app):
    app = make_app()
    small(app)
    await measured(app)
    starts, trims = {}, []
    for n in range(1, 121):
        await add(app, n)
        await processed(app, n)  # the memory pass is keeping up
        window = await read(app)
        starts[n] = window.rows[0].msg_id
        assert window.rows[-1].msg_id == n and not window.blocked
        if n > 1 and starts[n] != starts[n - 1]:
            trims.append(n)
            kept = 100 + sum(estimate_tokens(m) for m in window.rows)
            assert KEEP - 140 <= kept <= KEEP  # down to the watermark, by whole messages
        assert 100 + sum(estimate_tokens(m) for m in window.rows) < BUDGET + 140
    assert len(trims) >= 3 and all(b - a >= 8 for a, b in zip(trims, trims[1:]))  # a chunk at a time, not per message
    assert starts[trims[0] - 1] == 1  # until the first trim the window only grew


async def test_the_start_survives_a_restart_and_a_second_read_changes_nothing(make_app, tmp_path):
    app = make_app()
    small(app)
    await measured(app)
    for n in range(1, 41):
        await add(app, n)
        await processed(app, n)
    first = [m.msg_id for m in (await read(app)).rows]
    assert first[0] > 1 and first == [m.msg_id for m in (await read(app)).rows]
    reopened = Store(tmp_path, owner_id=app.store.owner_id)
    try:
        assert [m.msg_id for m in (await chat_window.load(reopened, GROUP, app.config)).rows] == first
    finally:
        reopened.close()


async def test_deleting_the_first_message_or_editing_one_does_not_move_the_start(make_app):
    app = make_app()
    small(app)
    await measured(app)
    for n in range(1, 41):
        await add(app, n)
        await processed(app, n)
    rows = (await read(app)).rows
    await app.store.record_message_delete(str(GROUP), [rows[0].msg_id, rows[3].msg_id])
    await app.store.record_message_edit(str(GROUP), rows[5].msg_id, "edited " * 10)
    again = (await read(app)).rows
    assert [m.msg_id for m in again] == [m.msg_id for m in rows if m.msg_id not in (rows[0].msg_id, rows[3].msg_id)]


async def test_a_trim_never_drops_a_message_the_memory_pass_has_not_read(make_app):
    app = make_app()
    small(app)
    await measured(app)
    for n in range(1, 41):
        await add(app, n)
    window = await read(app)
    assert window.blocked and window.rows[0].msg_id == 1 and len(window.rows) == 40  # due, but nothing is read yet
    await processed(app, 40)
    window = await read(app)
    assert not window.blocked and window.rows[0].msg_id > 1  # now it trims, once

    # every message that leaves the window has been read first, over a long stream with a pass that lags
    before, cursor = set(), 0
    for n in range(41, 400):
        await add(app, n)
        if n % 25 == 0:
            cursor = n - 3
            await processed(app, cursor)
        window = await read(app)
        ids = {m.msg_id for m in window.rows}
        assert all(msg_id <= cursor for msg_id in before - ids)
        before = ids


async def test_huge_messages_leave_a_few_and_do_not_trim_again_and_again(make_app):
    app = make_app()
    small(app, budget=32000, keep=20000)
    await measured(app)
    for n in range(1, 11):
        await add(app, n, size=90_000)  # about 30k tokens each
    await processed(app, 10)
    window = await read(app)
    assert [m.msg_id for m in window.rows] == list(range(10 - MIN_KEPT + 1, 11)) and window.tokens > 32000
    state = await app.store.kv_get(f"window:{GROUP}")
    assert [m.msg_id for m in (await read(app)).rows] == [m.msg_id for m in window.rows]
    assert await app.store.kv_get(f"window:{GROUP}") == state  # nothing left to drop, nothing rewritten


async def test_before_any_prompt_is_measured_a_default_stands_in_for_the_fixed_part(make_app):
    app = make_app()
    small(app, budget=DEFAULT_FIXED + 1500, keep=DEFAULT_FIXED + 900)
    for n in range(1, 15):  # ~136 tokens each
        await add(app, n)
        await processed(app, n)
    window = await read(app)
    assert window.tokens < DEFAULT_FIXED + 1500 + 140 and window.rows[0].msg_id > 1  # trimmed on the estimate alone
    assert await app.store.first_chat_input_tokens(GROUP, 0) == 0  # no usage row yet
    await app.dispatch._measure_prompt(GROUP, window.rows, await app.store.last_usage_id())
    assert "fixed" not in (await app.store.kv_get(f"window:{GROUP}") or "") or json.loads(await app.store.kv_get(f"window:{GROUP}"))["fixed"] == DEFAULT_FIXED


async def test_the_real_prompt_size_replaces_the_default_after_the_first_call(make_app):
    app = make_app()
    small(app)
    await add(app, 1)
    await add(app, 2)
    rows = (await read(app)).rows
    mark = await app.store.last_usage_id()
    await app.store.record_usage("2026-01-01T00:00:00+00:00", "chat", "m", "h", GROUP, 8900, 20, 0.0, 0)
    await app.store.record_usage("2026-01-01T00:00:01+00:00", "chat", "m", "h", GROUP, 9100, 20, 0.0, 0)  # a tool round: not the prompt
    await app.store.record_usage("2026-01-01T00:00:02+00:00", "chat", "m", "h", FRIEND, 1234, 20, 0.0, 0)
    await app.dispatch._measure_prompt(GROUP, rows, mark)
    state = json.loads(await app.store.kv_get(f"window:{GROUP}"))
    assert state["fixed"] == 8900 - sum(estimate_tokens(m) for m in rows) and state["id"] == rows[0].id
    assert (await read(app)).tokens == 8900  # the prompt it measured, plus nothing new
    await add(app, 3)
    assert (await read(app)).tokens == 8900 + estimate_tokens(message(3))


async def test_a_due_trim_asks_for_a_pass_and_the_trim_follows_it_in_the_next_turn(make_app):
    ops = json.dumps({"ops": [{"file": "MEMORY.md", "op": "add", "text": "fact"}], "summary": ""})
    app = make_app(contents=(FINAL, ops, FINAL, FINAL))
    small(app)
    await measured(app)
    await app.store.set_chat_auth_status(GROUP, "approved", title="The Group")
    for n in range(1, 31):
        await add(app, n)
    await app.dispatch.presence.wake(GROUP, parse_habits(""))
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 2 and "tools" in app.script.requests[0] and "tools" not in app.script.requests[1]
    assert await app.store.get_memory_cursor(GROUP) >= 30  # the pass read everything before anything was dropped
    first = first_id(turn_requests(app)[0])
    assert first == "1"
    await add(app, 31)
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    trimmed = first_id(turn_requests(app)[1])
    assert int(trimmed) > 1
    await add(app, 32)
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    assert first_id(turn_requests(app)[2]) == trimmed  # the start holds still between trims
    for request in turn_requests(app):  # the turn note stays after the window
        assert re.search(r"</chat_window>\n\nIt is [^<]*state: online[^<]*$", request["messages"][-1]["content"])


def first_id(request):
    return re.search(r"<chat_window[^>]*>\s*<msg id=\"(\d+)\"", request["messages"][-1]["content"]).group(1)


async def test_no_pass_is_asked_for_below_the_budget(make_app):
    app = make_app(contents=(FINAL,))
    small(app)
    await measured(app)
    await app.store.set_chat_auth_status(GROUP, "approved", title="The Group")
    for n in range(1, 6):
        await add(app, n)
    await app.dispatch.presence.wake(GROUP, parse_habits(""))
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1


async def test_timings_are_recorded_for_each_step_of_a_turn(make_app):
    from test_agent import FINAL as ACTIONS, Script, call, make_client, reply

    from dom.agent import run_turn
    from dom.tools import TurnCtx, core_tools

    app = make_app()
    client = make_client(Script(
        reply(tool_calls=[call("search_history", {"query": "x"})]), reply(content=json.dumps(ACTIONS)),
    ))
    ctx = TurnCtx("group", GROUP, FRIEND, app.home, app.store, None)
    async with turn_log.capture(app.store, datetime(2026, 1, 1, tzinfo=timezone.utc), GROUP, "group"):
        with turn_log.timed("typing"):
            pass
        await run_turn(client, [{"role": "user", "content": "hi"}], core_tools(), ctx)
    [row] = await app.store.recent_turns()
    trace = json.loads(row["trace"])
    assert [item for item in trace if "@" not in item] == ["search_history", "final"]
    timings = [item.rpartition("@") for item in trace if "@" in item]
    assert [what for what, _, _ in timings] == ["typing", "model", "search_history", "model"]
    assert all(ms.isdigit() for _, _, ms in timings)


async def test_a_full_turn_records_model_and_typing_time(make_app):
    app = make_app(contents=(FINAL,))
    await app.store.set_chat_auth_status(GROUP, "approved", title="The Group")
    await add(app, 1)
    await app.dispatch.presence.wake(GROUP, parse_habits(""))
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    [row] = await app.store.recent_turns()
    assert [item.rpartition("@")[0] for item in json.loads(row["trace"]) if "@" in item] == ["model", "typing"]


async def test_a_turn_cut_off_by_a_restart_is_logged_as_interrupted_not_quiet(make_app):
    import asyncio

    app = make_app()
    started = asyncio.Event()

    async def turn():
        async with turn_log.capture(app.store, datetime(2026, 1, 1, tzinfo=timezone.utc), GROUP, "group"):
            turn_log.step("triage")
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(turn())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = await app.store.recent_turns()
    assert row["outcome"] == "Interrupted"
