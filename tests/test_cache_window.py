"""The chat window trims in chunks so its start (and the cached prompt prefix) holds still between trims."""

import json
import re
from datetime import datetime, timedelta, timezone

from tghelpers import FINAL, FRIEND, GROUP, drain_dispatch, turn_requests

from dom import turn_log
from dom.dispatch import MEMORY_PASS_MARGIN
from dom.models import StoredMessage
from dom.presence import parse_habits
from dom.store.store import window_chunk, window_start

BASE = datetime(2025, 12, 1, tzinfo=timezone.utc)
SIZE = 80  # the default window_size
KEEP = SIZE * 3 // 5


async def add(app, n, chat_id=GROUP, role="user"):
    """Message n (its Telegram id), created one minute after message n-1."""
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), msg_id=n, sender_user_id=FRIEND if role == "user" else None,
        sender_name="Fran" if role == "user" else "", role=role, is_bot=role == "assistant",
        visibility_scope=app.store.visibility_for(chat_id), text=f"message {n}",
        created_at=(BASE + timedelta(minutes=n)).isoformat(),
    ))


def test_the_start_depends_only_on_the_count_and_moves_in_whole_chunks():
    assert window_chunk(SIZE) == 32 and KEEP == 48
    assert [window_start(total, SIZE) for total in (0, 1, 80)] == [0, 0, 0]
    assert window_start(81, SIZE) == window_start(112, SIZE) == 32
    assert window_start(113, SIZE) == window_start(144, SIZE) == 64
    for total in range(1, 400):
        assert window_start(total, SIZE) == window_start(total, SIZE)  # same answer on every turn
        assert window_start(total, SIZE) % 32 == 0
        length = total - window_start(total, SIZE)
        assert length <= SIZE and (total <= SIZE or KEEP < length)  # a trim leaves about 60%, never more than size
    assert window_start(5, 0) == 5 and window_start(5, 1) == 4  # degenerate sizes still make sense


async def test_the_window_trims_in_chunks_and_holds_its_start_between_trims(make_app):
    app = make_app()
    starts = {}
    for n in range(1, 241):
        await add(app, n)
        window = await app.store.get_chat_window(GROUP, SIZE)
        assert [m.msg_id for m in window] == list(range(n - len(window) + 1, n + 1))  # the newest messages, in order
        assert window == await app.store.get_chat_window(GROUP, SIZE)  # asking again changes nothing
        starts[n] = window[0].msg_id
    changes = [n for n in range(2, 241) if starts[n] != starts[n - 1]]
    assert changes == [81, 113, 145, 177, 209]  # one trim every 32 messages, not one per message
    assert starts[81] == 33 and starts[112] == 33 and starts[113] == 65  # a trim leaves 49 messages
    assert starts[80] == 1 and starts[1] == 1


async def test_the_start_is_the_same_after_a_restart(make_app, tmp_path):
    from dom.store import Store

    app = make_app()
    for n in range(1, 100):
        await add(app, n)
    before = [m.msg_id for m in await app.store.get_chat_window(GROUP, SIZE)]
    reopened = Store(tmp_path, owner_id=app.store.owner_id)
    try:
        assert [m.msg_id for m in await reopened.get_chat_window(GROUP, SIZE)] == before
    finally:
        reopened.close()


async def test_a_turns_window_does_not_include_messages_after_the_one_it_answers(make_app):
    app = make_app()
    for n in range(1, 101):
        await add(app, n)
    newest = (await app.store.get_recent_chat_window(GROUP, 100))[89]  # message 90
    window = await app.store.get_chat_window(GROUP, SIZE, through_id=newest.id)
    assert [m.msg_id for m in window][0] == 33 and window[-1].msg_id == 90  # 90 messages count, so one chunk is dropped


async def test_no_message_leaves_the_window_before_a_pass_has_read_it(make_app):
    app = make_app()
    dispatch, store = app.dispatch, app.store
    before, passes, trims = set(), [], []
    for n in range(1, 501):
        await add(app, n)
        window = {m.msg_id for m in await store.get_chat_window(GROUP, SIZE)}  # this turn's window
        if before and min(window) != min(before):
            trims.append(n)
        cursor = await store.get_memory_cursor(GROUP)
        assert all(msg_id <= cursor for msg_id in before - window)  # whatever scrolled out was processed
        before = window
        if await dispatch._memory_due(GROUP):  # checked after the turn, as _respond does
            passes.append(n)
            await store.advance_memory_cursor(GROUP, n)  # the pass reads everything up to now
    assert passes[:3] == [75, 139, 203] and len(passes) <= 8  # a pass every other trim, not one per few turns
    # Each pass comes just before a trim, so the notes and the window start change in the same stretch of turns.
    for n in passes[:-1]:
        assert 0 < min(t for t in trims if t > n) - n <= MEMORY_PASS_MARGIN + 1


async def test_nothing_is_due_while_the_window_is_not_near_a_trim_or_what_leaves_is_already_read(make_app):
    app = make_app()
    for n in range(1, 40):
        await add(app, n)
    assert not await app.dispatch._memory_due(GROUP)  # plenty of room left
    for n in range(40, 80):
        await add(app, n)
    assert await app.dispatch._memory_due(GROUP)  # 79 messages, none read
    await app.store.advance_memory_cursor(GROUP, 79)
    assert not await app.dispatch._memory_due(GROUP)  # the next trim only drops read messages


async def test_a_small_window_that_cannot_trim_in_chunks_still_triggers_by_count(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"window_size": 10})
    for n in range(1, 5):
        await add(app, n)
    assert not await app.dispatch._memory_due(GROUP)
    await add(app, 5)
    assert await app.dispatch._memory_due(GROUP)  # 10 - margin 5


async def test_consecutive_turns_send_the_same_window_start(make_app):
    app = make_app(contents=(FINAL, FINAL))
    await app.store.set_chat_auth_status(GROUP, "approved", title="The Group")
    for n in range(1, 91):
        await add(app, n)
    habits = parse_habits("")
    await app.dispatch.presence.wake(GROUP, habits)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    for n in range(91, 96):
        await add(app, n)
    app.dispatch.now.advance(minutes=1)
    await app.dispatch.tick()
    await drain_dispatch(app.dispatch)
    first_ids = [re.search(r"<chat_window[^>]*>\s*<msg id=\"(\d+)\"", r["messages"][-1]["content"]).group(1) for r in turn_requests(app)]
    assert len(first_ids) == 2 and first_ids[0] == first_ids[1] == "33"
    for request in turn_requests(app):  # the turn note stays after the window, the last thing in the prompt
        assert re.search(r"</chat_window>\n\nIt is [^<]*state: online[^<]*$", request["messages"][-1]["content"])


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
