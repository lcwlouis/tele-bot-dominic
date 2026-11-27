"""Rejected reactions are watched: the log line, the Turn log step and flag, and the week's count (v0.8.6 W8)."""

import json
import logging
from datetime import timedelta

from aiogram.exceptions import TelegramBadRequest

from dom import turn_log
from dom.telegram.executor import react
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, tg


class RefusingBot:
    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kw):
        raise TelegramBadRequest(method=None, message="Bad Request: REACTION_INVALID")


async def test_a_rejected_reaction_logs_telegrams_reason_the_emoji_and_the_message(caplog):
    trace = turn_log.TurnTrace()
    with turn_log.track(trace), caplog.at_level(logging.WARNING):
        assert await react(RefusingBot(), GROUP, 42, "🔥") is False
    assert caplog.messages == [f"reaction '🔥' on message 42 rejected in chat {GROUP}: REACTION_INVALID"]
    assert trace.steps == ["reaction rejected: REACTION_INVALID"]
    [event] = [e for e in trace.events if e["type"] == "reaction"]
    assert (event["msg_id"], event["emoji"], event["result"], event["reason"]) == (42, "🔥", "refused", "REACTION_INVALID")


async def test_a_reason_never_hides_the_step_in_the_turn_log():
    class AtBot:
        async def set_message_reaction(self, *args, **kw):
            raise TelegramBadRequest(method=None, message="Bad Request: no @rights")

    trace = turn_log.TurnTrace()
    with turn_log.track(trace):
        await react(AtBot(), GROUP, 1, "🔥")
    assert turn_log.visible_steps(trace.steps) == ["reaction rejected: no rights"]


async def test_the_turn_row_is_flagged_and_the_week_is_counted(make_app, browser):
    app = make_app(contents=(json.dumps({"react": "🔥"}),))
    await online(app, GROUP)
    app.bot.set_message_reaction = RefusingBot().set_message_reaction
    await deliver(app, tg(GROUP, FRIEND, text="nice", msg_id=5))
    now = app.dispatch.now()
    steps = ["reaction rejected: REACTION_INVALID", "reaction rejected: REACTION_INVALID"]  # two, a week old
    await app.store.record_turn(now - timedelta(days=8), GROUP, "group", 0, steps, "", False, 0, "Reaction failed")  # too old
    await app.store.record_turn(now - timedelta(days=1), GROUP, "group", 0, ["typing"], "", False, 0, "Replied in 1 bubble")
    async with browser(app) as client:
        data = (await client.get("/api/system/turns")).json()
    assert [turn["reaction_rejected"] for turn in data["turns"]].count(True) == 2  # the live turn and the old one shown
    assert [turn["outcome"] for turn in data["turns"] if not turn["reaction_rejected"]] == ["Replied in 1 bubble"]
    assert data["reactions"]["days"] == 7 and data["reactions"]["rejected"] == 1  # the old turn's two are out of the week
    [latest] = data["reactions"]["latest"]
    assert latest["reason"] == "REACTION_INVALID" and latest["chat_id"] == str(GROUP)
