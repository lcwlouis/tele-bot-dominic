"""The step budget of the owner's desktop tasks (v0.8.3 W4): a task about to run out pauses and asks the owner to keep
going or wrap up; Keep going adds the starting budget once per tap, up to three times it in all; no answer wraps up."""

import asyncio
import re

from aiogram.exceptions import TelegramNetworkError
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


def tap(app, data, user=OWNER):
    return handle_callback(app, CallbackQuery(
        id="callback", chat_instance="instance", data=data,
        from_user=User(id=user, is_bot=False, first_name="Owner"), message=tg(OWNER, user),
    ))


def press(app, card, index, user=OWNER):
    return tap(app, card[3]["reply_markup"].inline_keyboard[0][index].callback_data, user)


def token_of(card):
    return card[3]["reply_markup"].inline_keyboard[0][0].callback_data.rsplit(":", 1)[1]


def toasts(app):
    return [c[2] for c in app.bot.calls if c[0] == "answer"]


async def test_keep_going_extends_the_budget_and_is_recorded_on_the_task(make_app):
    app = make_app()
    worker = await worker_for(app, STEP, STEP, STEP, call("finish", result="All found"), INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    card = await ask_card(app)
    assert f"#{task_id}" in card[2] and "2 steps" in card[2]
    keep, wrap = card[3]["reply_markup"].inline_keyboard[0]
    token = token_of(card)
    assert re.fullmatch(r"[0-9a-f]{16}", token)  # a random single-use token, not the task's step count
    assert (keep.text, keep.callback_data) == ("Keep going (+2 steps)", f"desktop:more:{task_id}:{token}")
    assert (wrap.text, wrap.callback_data) == ("Wrap up now", f"desktop:wrap:{task_id}:{token}")
    assert (await task_row(app, task_id))["status"] == "running"  # paused before the wrap-up, not failed

    await press(app, card, 0, user=FRIEND)  # only the owner answers
    assert worker._budget_ask is not None
    assert not await worker.extend(OWNER, task_id, "0" * 16, True)  # a token that is not this card's does nothing
    assert not await worker.extend(OWNER, task_id + 1, token, True)  # nor this card's token for another task
    await press(app, card, 0)
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert row["status"] == "done" and row["result"] == "All found"
    assert (row["steps_budget"], row["extra_steps"], row["steps"]) == (2, 2, 4)
    assert not await worker.extend(OWNER, task_id, token, True)  # answered once only
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
    assert not await worker.extend(OWNER, task_id, "0" * 16, True)


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


async def test_a_stale_or_replayed_tap_never_answers_a_later_pause(make_app):
    app = make_app()
    worker = await worker_for(app, *[STEP] * 4, call("finish", result="Done"), INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    first = await ask_card(app, 1)
    await press(app, first, 0)
    second = await ask_card(app, 2)
    assert token_of(second) != token_of(first)
    await press(app, first, 0)  # the first card's Keep going again: replayed, or a delayed duplicate
    await press(app, first, 1)  # and its Wrap up now
    await tap(app, f"desktop:more:{task_id}:{token_of(first)}")
    assert toasts(app)[-3:] == ["already handled"] * 3
    assert worker._budget_ask is not None and not worker._budget_ask[3].done()  # the second pause is still open
    await press(app, second, 1)
    await wait_task(worker)
    row = await task_row(app, task_id)
    assert (row["status"], row["extra_steps"]) == ("failed", 2)  # one extension, then the second card's wrap-up


async def test_a_card_that_cannot_be_sent_wraps_up_with_the_partial_work(make_app):
    app = make_app()
    send = app.bot.send_message

    async def flaky(chat_id, text, **kw):
        if "Keep going" in str(kw.get("reply_markup")):
            raise TelegramNetworkError(method=None, message="Telegram is unreachable")
        return await send(chat_id, text, **kw)

    app.bot.send_message = flaky
    worker = await worker_for(app, STEP, STEP, call("finish", result="Partial findings"), INVALID, max_steps=2,
                              handoff_seconds=60)
    task_id = await worker.enqueue(OWNER, "Research it")
    await wait_task(worker)  # well within the handoff window: no card means nobody to wait for
    row = await task_row(app, task_id)
    assert row["status"] == "failed" and row["result"] == "Partial findings" and row["extra_steps"] == 0
    assert worker._budget_ask is None


async def test_cancelling_a_paused_task_does_not_wait_on_the_card_edit(make_app):
    app = make_app()
    release = asyncio.Event()
    edits = []

    async def slow_edit(**kw):
        edits.append(kw)
        await release.wait()  # a slow request or a flood wait

    app.bot.edit_message_reply_markup = slow_edit
    worker = await worker_for(app, STEP, STEP, call("finish", result="Partial"), INVALID, max_steps=2)
    task_id = await worker.enqueue(OWNER, "Research it")
    await ask_card(app)
    async with asyncio.timeout(1):
        assert await worker.cancel(OWNER, task_id)
        while not edits:
            await asyncio.sleep(0.001)
    assert not worker.control.locked()  # new work and other answers are not held up behind the edit
    assert (await task_row(app, task_id))["status"] == "cancelled"
    release.set()
    await worker.close()
    assert not worker._chores
