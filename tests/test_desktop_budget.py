"""The step budget of the owner's desktop tasks (v0.8.3 W4): a task about to run out pauses and asks the owner to keep
going or wrap up; Keep going adds the starting budget once per tap, up to three times it in all; no answer wraps up."""

import asyncio

from aiogram.types import CallbackQuery, User

from dom.desktop.worker import BUDGET_CAP
from dom.telegram.handlers import handle_callback
from test_desktop_worker import call, task_row, wait_task, worker_for
from tghelpers import FRIEND, OWNER, tg

STEP = call("read_page")
INVALID = {"content": "invalid"}  # what the step-count report's model call gets: the report falls back to plain text


async def ask_card(app, count=1):
    """The `count`th keep-going card sent to the owner, once it is out."""
    async with asyncio.timeout(2):
        while len(cards := [c for c in app.bot.of("send", OWNER) if "Keep going" in str(c[3].get("reply_markup"))]) < count:
            await asyncio.sleep(0.001)
    return cards[count - 1]


def press(app, card, index, user=OWNER):
    button = card[3]["reply_markup"].inline_keyboard[0][index]
    return handle_callback(app, CallbackQuery(
        id="callback", chat_instance="instance", data=button.callback_data,
        from_user=User(id=user, is_bot=False, first_name="Owner"), message=tg(OWNER, user),
    ))


async def test_keep_going_extends_the_budget_and_is_recorded_on_the_task(make_app):
    app = make_app()
    worker = await worker_for(app, STEP, STEP, STEP, call("finish", result="All found"), INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    card = await ask_card(app)
    assert f"#{task_id}" in card[2] and "2 steps" in card[2]
    keep, wrap = card[3]["reply_markup"].inline_keyboard[0]
    assert (keep.text, keep.callback_data) == ("Keep going (+2 steps)", f"desktop:more:{task_id}:2")
    assert (wrap.text, wrap.callback_data) == ("Wrap up now", f"desktop:wrap:{task_id}:2")
    assert (await task_row(app, task_id))["status"] == "running"  # paused before the wrap-up, not failed

    await press(app, card, 0, user=FRIEND)  # only the owner answers
    assert worker._budget_ask is not None
    assert not await worker.extend(OWNER, task_id, 3, True)  # a stale card does nothing
    await press(app, card, 0)
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert row["status"] == "done" and row["result"] == "All found"
    assert (row["steps_budget"], row["extra_steps"], row["steps"]) == (2, 2, 4)
    assert not await worker.extend(OWNER, task_id, 2, True)  # answered once only
    settled = app.bot.of("markup")
    assert any("Kept going (+2 steps)" in str(c) for c in settled)


async def test_wrap_up_now_reports_what_the_task_found(make_app):
    app = make_app()
    finish = call("finish", result="Found two of three")
    worker = await worker_for(app, STEP, STEP, finish, INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    await press(app, await ask_card(app), 1)
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert row["status"] == "failed" and "step limit" in row["error"]
    assert row["result"] == "Found two of three"  # the wrap-up's account
    assert (row["steps_budget"], row["extra_steps"]) == (2, 0)
    request = app.client.requests[2]
    assert [s["function"]["name"] for s in request[1]] == ["finish"]  # the wrap-up call offers only finish


async def test_no_answer_within_the_handoff_window_wraps_up(make_app):
    app = make_app()
    worker = await worker_for(app, STEP, STEP, call("finish", result="Partial"), INVALID, max_steps=2, handoff_seconds=0.05)
    task_id = await worker.enqueue(OWNER, "Research it")
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert row["status"] == "failed" and row["result"] == "Partial" and row["extra_steps"] == 0
    assert any("No answer: wrapped up" in str(c) for c in app.bot.of("markup"))
    assert not await worker.extend(OWNER, task_id, 2, True)


async def test_extensions_stop_at_three_times_the_starting_budget(make_app):
    app = make_app()
    worker = await worker_for(app, *[STEP] * 6, call("finish", result="Out of room"), INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    await press(app, await ask_card(app, 1), 0)
    await press(app, await ask_card(app, 2), 0)
    await wait_task(worker)  # at 6 steps (3 x 2) it wraps up without asking again
    row = await task_row(app, task_id)
    assert BUDGET_CAP == 3
    assert (row["status"], row["steps"], row["steps_budget"], row["extra_steps"]) == ("failed", 6, 2, 4)
    assert row["result"] == "Out of room"
    assert len([c for c in app.bot.of("send", OWNER) if "Keep going" in str(c[3].get("reply_markup"))]) == 2


async def test_the_budget_comes_from_the_dashboard_setting(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"desktop": app.config.desktop.model_copy(update={"max_steps": 12})})
    worker = await worker_for(app, call("finish", result="Done"), INVALID)
    assert worker.max_steps == 12
    task_id = await worker.enqueue(OWNER, "Quick one")
    await wait_task(worker)
    assert (await task_row(app, task_id))["steps_budget"] == 12


async def test_desk_shows_the_budget_and_extension(make_app, browser):
    app = make_app()
    worker = await worker_for(app, STEP, STEP, STEP, call("finish", result="Done"), INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    card = await ask_card(app)
    async with browser(app) as client:
        desk = (await client.get("/api/work/desk")).json()
        assert desk["task"]["id"] == task_id and (desk["task"]["steps_budget"], desk["task"]["extra_steps"]) == (2, 0)
        await press(app, card, 0)
        await wait_task(worker)
        desk = (await client.get("/api/work/desk")).json()
    entry = next(item for item in desk["history"] if item["id"] == task_id)
    assert (entry["steps_budget"], entry["extra_steps"]) == (2, 2)
