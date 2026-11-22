"""v0.8.1 W4: bookkeeping model calls (skill_view, refused switch_vibe, extend_online) are replaced by Jev
choices made on calls that already happen, or after the reply has gone out."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from dom import skills
from dom.jev import JevClient, JevUnavailable
from dom.presence import parse_habits
from dom.telegram.handlers import handle_message
from dom.tools import SKILL_VIEW_TOOL, TurnCtx, enabled_tools
from dom.traits import SwitchRefused, start_task_mode, switch_blocker, switch_vibe
from dom.triage import NO_SKILL, Triage, triage_online, triage_stay
from dom.web.pages import _turn_timings
from test_chat_skills import BODY, approved
from test_dispatch import FakeJev, deliver, gated_sleep, setup_dispatch  # noqa: F401  (fixtures)
from test_vibe import auto, install, pick
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg, turn_requests


class W4Jev(FakeJev):
    """FakeJev that also answers the skill question (inside the triage call) and the after-reply stay question."""

    def __init__(self, *args, skill=None, stay="a_while", stay_confidence=1, stay_failure=None, on_skill=None, **kw):
        super().__init__(*args, **kw)
        self.skill, self.stay, self.stay_confidence = skill, stay, stay_confidence
        self.stay_failure, self.on_skill = stay_failure, on_skill

    @property
    def stay_calls(self):
        return [call for call in self.calls if "stay" in call[1]]

    async def decide(self, state, questions):
        if "stay" in questions:
            self.calls.append((state, questions))
            if self.stay_failure:
                raise self.stay_failure
            return {"stay": {"type": "choice", "choice": self.stay, "confidence": self.stay_confidence}}
        answers = await super().decide(state, questions)
        if "skill" in questions:
            if self.on_skill:
                self.on_skill()
            answers["skill"] = {"type": "choice", "choice": self.skill, "confidence": 1}
        return answers


def note(app, index=0):
    return turn_requests(app)[index]["messages"][-1]["content"]


def tool_names(app, index=0):
    return [tool["function"]["name"] for tool in turn_requests(app)[index]["tools"]]


# ---- 1. skill pre-pick -------------------------------------------------------------------------

@pytest.fixture
def jev_http():
    """A real JevClient over a mock transport, so model calls are recorded the way they are in production."""
    def make(http, dispatch):
        return JevClient(http, "k", "test-jev", "http://localhost", usage=dispatch.usage)
    return make


class Stub:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def decide(self, state, questions):
        self.calls.append(questions)
        return self.answers


async def test_triage_asks_for_the_skill_inside_the_existing_call():
    stub = Stub({
        "action": {"type": "choice", "choice": "reply", "confidence": 1},
        "skill": {"type": "choice", "choice": "report", "confidence": 0.9},
    })
    verdict = await triage_online(stub, "card", "<chat_window/>", skills=[("report", "Summarise a table"), ("tea", "Brew")])
    assert len(stub.calls) == 1
    question = stub.calls[0]["skill"]
    assert question["type"] == "choice"
    assert list(question["criteria"]) == ["report", "tea", NO_SKILL]
    assert question["criteria"]["report"] == "Summarise a table"
    assert verdict.skill == "report" and isinstance(verdict, Triage)


@pytest.mark.parametrize("choice,confidence", [
    ("ghost", 1),       # not one of the approved skills
    (NO_SKILL, 1),      # Jev says none fits
    ("report", 0.69),   # not sure enough
    ("report", None),
    (["report"], 1),    # malformed
])
async def test_triage_only_accepts_a_confident_approved_skill_name(choice, confidence):
    answer = {"type": "choice", "choice": choice}
    if confidence is not None:
        answer["confidence"] = confidence
    stub = Stub({"action": {"type": "choice", "choice": "reply", "confidence": 1}, "skill": answer})
    verdict = await triage_online(stub, "card", "<chat_window/>", skills=[("report", "Summarise a table")])
    assert verdict.skill is None


async def test_triage_without_skills_asks_nothing_new():
    stub = Stub({"action": {"type": "choice", "choice": "reply", "confidence": 1}})
    await triage_online(stub, "card", "<chat_window/>")
    assert "skill" not in stub.calls[0]


@pytest.mark.parametrize("scope,chat,sender", [("owner_dm", OWNER, OWNER), ("friend_dm", FRIEND, FRIEND), ("group", GROUP, FRIEND)])
async def test_picked_skill_body_reaches_the_turn_note_and_saves_a_call(setup_dispatch, scope, chat, sender):
    jev = W4Jev(skill="report")
    app, dispatch, _ = await setup_dispatch(jev, chat_id=chat)
    await approved(app)
    await dispatch.presence.go_online(chat, parse_habits(""))
    await deliver(app, chat_id=chat, sender=sender)

    arrival_call = jev.calls[0]
    assert list(arrival_call[1]["skill"]["criteria"]) == ["report", NO_SKILL]
    assert len(jev.calls) == 1  # the pick rode the triage call: no second Jev call on the reply path
    assert len(turn_requests(app)) == 1  # and the model never needed skill_view
    text = note(app)
    assert '<skill name="report">' in text and BODY in text
    assert "never present it as a procedure" in text


@pytest.mark.parametrize("scope,chat,sender,present", [
    ("owner_dm", OWNER, OWNER, True),
    ("friend_dm", FRIEND, FRIEND, False),
    ("group", GROUP, FRIEND, False),
])
async def test_skill_body_follows_the_scopes_skill_view_is_offered_in(setup_dispatch, monkeypatch, scope, chat, sender, present):
    monkeypatch.setattr("dom.tools.SKILL_VIEW_TOOL", replace(SKILL_VIEW_TOOL, scopes={"owner_dm"}))
    app, dispatch, _ = await setup_dispatch(W4Jev(skill="report"), chat_id=chat)
    await approved(app)
    await dispatch.presence.go_online(chat, parse_habits(""))
    await deliver(app, chat_id=chat, sender=sender)

    assert ("skill_view" in tool_names(app)) is present
    assert (BODY in note(app)) is present  # the body goes in only where skill_view would have returned it


async def test_retired_skills_are_not_offered_and_not_injected(setup_dispatch):
    jev = W4Jev(skill="report")
    app, dispatch, _ = await setup_dispatch(jev)
    await approved(app, "report")
    await approved(app, "tea", "Brew tea", "Warm the pot.")
    skills.retire_skill(app.home, "report")
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)

    assert list(jev.calls[0][1]["skill"]["criteria"]) == ["tea", NO_SKILL]  # retired: never a choice
    assert "<skill" not in note(app) and BODY not in note(app)  # and a stale answer for it is dropped


async def test_a_skill_retired_after_triage_is_not_injected(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(W4Jev(skill="report", on_skill=lambda: None))
    await approved(app)
    dispatch.jev.on_skill = lambda: skills.retire_skill(app.home, "report")  # retired while Jev is answering
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)
    assert BODY not in note(app)


async def test_no_skills_means_no_skill_question(setup_dispatch):
    jev = W4Jev(skill="report")
    app, dispatch, _ = await setup_dispatch(jev)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)
    assert "skill" not in jev.calls[0][1] and "<skill" not in note(app)


async def test_skill_view_stays_as_the_fallback(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(W4Jev(skill=NO_SKILL))
    await approved(app)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)
    assert "skill_view" in tool_names(app) and "<skill" not in note(app)
    context = TurnCtx("group", GROUP, FRIEND, app.home, app.store, OWNER, app=app)
    assert await SKILL_VIEW_TOOL.handler({"name": "report"}, context) == BODY


async def test_follow_up_turn_still_gets_the_skill_of_its_message(setup_dispatch, gated_sleep):
    """A turn that is not judged again (follow-up, restart, /wake) still carries the choice made on arrival."""
    sleep, _ = gated_sleep  # the debounce never ends: Jev saying the sender is done starts each turn
    app, dispatch, _ = await setup_dispatch(W4Jev(skill="report", done=1), sleep=sleep)
    await approved(app)
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)
    dispatch._followups[GROUP] = [0]  # an earlier bubble went out before this message came in
    await deliver(app, 2, text="and one more")
    assert "came in while you were answering" in note(app, 1)
    assert BODY in note(app, 0) and BODY in note(app, 1)


# ---- 2. vibe switch ----------------------------------------------------------------------------

SETTLED = "your vibe is settled for now"


async def test_note_says_when_the_vibe_cannot_switch_and_the_tool_list_stays_put(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    install(app)
    await auto(app)
    await pick(app, "classic")
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)  # can switch: no note
    await pick(app, "fboy", source="switch")  # already switched today
    await deliver(app, 2)

    assert SETTLED not in note(app, 0) and SETTLED in note(app, 1)
    assert tool_names(app, 0) == tool_names(app, 1) and "switch_vibe" in tool_names(app, 1)  # fixed shape (W5)
    settled_line = next(line for line in note(app, 1).splitlines() if SETTLED in line)
    assert "switch" not in settled_line and "tool" not in settled_line  # plain words, no machinery (E46)


async def test_note_for_a_chat_with_its_own_preset_and_for_task_mode(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(chat_id=OWNER)
    install(app)
    await auto(app)
    await pick(app, "classic")
    await dispatch.presence.go_online(OWNER, parse_habits(""))
    await deliver(app, chat_id=OWNER, sender=OWNER)
    await start_task_mode(app.home, app.store, OWNER, "plan the trip", dispatch.now())
    await deliver(app, 2, chat_id=OWNER, sender=OWNER)
    await app.store.set_chat_preset(OWNER, "fboy")
    await deliver(app, 3, chat_id=OWNER, sender=OWNER)

    assert SETTLED not in note(app, 0)
    assert SETTLED in note(app, 1)  # task mode
    assert SETTLED in note(app, 2)  # its own preset


async def test_note_is_silent_when_the_owner_fixed_the_preset_because_the_tool_is_not_offered(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    install(app)
    await app.store.kv_set("preset:global", "classic")
    await dispatch.presence.go_online(GROUP, parse_habits(""))
    await deliver(app)
    assert "switch_vibe" not in tool_names(app) and SETTLED not in note(app)


async def test_blocker_and_tool_refuse_for_the_same_reasons(make_app):
    app = make_app()
    install(app)
    await app.store.set_chat_auth_status(GROUP, "approved")
    now = app.dispatch.now()
    assert await switch_blocker(app.home, app.store, GROUP, now) == "the owner has fixed your preset, so you cannot switch"
    await auto(app)
    await pick(app, "classic")
    assert await switch_blocker(app.home, app.store, GROUP, now) is None
    await app.store.set_chat_preset(GROUP, "classic")
    assert "own preset" in await switch_blocker(app.home, app.store, GROUP, now)
    await app.store.set_chat_preset(GROUP, "")
    await pick(app, "fboy", source="switch")
    blocked = await switch_blocker(app.home, app.store, GROUP, now)
    assert "already switched" in blocked
    with pytest.raises(SwitchRefused, match="already switched"):
        await switch_vibe(app.home, app.store, GROUP, "classic", "bored", now)


async def test_blocker_when_there_is_no_other_vibe(make_app):
    app = make_app()
    install(app)
    for name in ("fboy", "assistant"):
        (app.home.root / f"presets/{name}.md").unlink()
    await auto(app)
    await pick(app, "classic")
    assert await switch_blocker(app.home, app.store, GROUP, app.dispatch.now()) == "there is no other vibe to switch to"


# ---- 3. staying online -------------------------------------------------------------------------

HABITS = parse_habits("")


async def online_for(dispatch, minutes, chat=GROUP):
    return await dispatch.presence.go_online(chat, HABITS, minutes=minutes)


async def test_extend_online_is_no_longer_a_tool(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    await dispatch.presence.go_online(GROUP, HABITS)
    await deliver(app)
    assert "extend_online" not in tool_names(app) and "extend_online" not in note(app)
    for scope in ("owner_dm", "friend_dm", "group"):
        assert "extend_online" not in [t.name for t in await enabled_tools(app, scope)]


async def test_jev_extends_the_session_after_the_reply(setup_dispatch):
    jev = W4Jev(stay="a_while")
    app, dispatch, _ = await setup_dispatch(jev)
    until = await online_for(dispatch, 5)
    await deliver(app)

    assert app.bot.of("send", GROUP)  # the reply went out
    [(state, questions)] = jev.stay_calls
    assert "hello" in state and list(questions) == ["stay"]
    assert (await dispatch.presence.state(GROUP, HABITS))[1] == until + timedelta(minutes=30)
    assert len(turn_requests(app)) == 1  # one model call: no bookkeeping round


async def test_a_little_is_ten_minutes(setup_dispatch):
    app, dispatch, _ = await setup_dispatch(W4Jev(stay="a_little"))
    until = await online_for(dispatch, 5)
    await deliver(app)
    assert (await dispatch.presence.state(GROUP, HABITS))[1] == until + timedelta(minutes=10)


@pytest.mark.parametrize("kw", [
    {"stay": "no"},
    {"stay": "a_while", "stay_confidence": 0.69},
    {"stay": "a_while", "stay_failure": JevUnavailable("down")},
    {"stay": "maybe"},
])
async def test_without_a_confident_yes_the_session_is_left_as_it_was(setup_dispatch, kw):
    jev = W4Jev(**kw)
    app, dispatch, _ = await setup_dispatch(jev)
    until = await online_for(dispatch, 5)
    await deliver(app)
    assert len(jev.stay_calls) == 1 and app.bot.of("send", GROUP)
    assert (await dispatch.presence.state(GROUP, HABITS))[1] == until  # today's default, never an error


async def test_no_jev_means_default_presence(setup_dispatch):
    app, dispatch, _ = await setup_dispatch()
    until = await online_for(dispatch, 5)
    await deliver(app)
    assert app.bot.of("send", GROUP) and (await dispatch.presence.state(GROUP, HABITS))[1] == until


async def test_jev_is_not_asked_while_plenty_of_the_session_is_left(setup_dispatch):
    jev = W4Jev()
    app, dispatch, _ = await setup_dispatch(jev)
    await online_for(dispatch, 20)
    await deliver(app)
    assert jev.stay_calls == []


async def test_jev_is_not_asked_when_the_caps_leave_no_room(setup_dispatch):
    jev = W4Jev()
    app, dispatch, clock = await setup_dispatch(jev)
    clock.at = clock.at.replace(hour=23, minute=55)  # the sleep window starts at 23:59
    await online_for(dispatch, 4)
    await dispatch.presence.extend_online(GROUP, HABITS, 30)
    assert await dispatch.presence.stay_room(GROUP, HABITS) == (pytest.approx(4), 0)
    await deliver(app)
    assert jev.stay_calls == []


async def test_extensions_keep_the_tools_caps(setup_dispatch):
    """Whatever Jev says, it goes through Presence.extend_online: 30 minutes at a time and two hours a session."""
    jev = W4Jev(stay="a_while")
    app, dispatch, clock = await setup_dispatch(jev)
    await online_for(dispatch, 5)
    ends = []
    for msg_id in range(1, 7):
        await deliver(app, msg_id, text=f"line {msg_id}")
        until = (await dispatch.presence.state(GROUP, HABITS))[1]
        ends.append(until.strftime("%H:%M"))
        clock.at = until - timedelta(minutes=5)  # near the end each time, so Jev is asked while there is room
    assert len(jev.stay_calls) == 4  # not asked once the two hours are used up
    assert ends == ["12:35", "13:05", "13:35", "14:00", "14:00", "14:00"]


async def test_stay_check_pauses_with_the_spend_cap_and_changes_nothing(setup_dispatch, monkeypatch):
    jev = W4Jev()
    app, dispatch, _ = await setup_dispatch(jev)
    until = await online_for(dispatch, 5)

    async def paused(chat_id):
        return False

    monkeypatch.setattr(dispatch, "model_allowed", paused)
    await dispatch._stay_check(GROUP, None)
    assert jev.stay_calls == [] and (await dispatch.presence.state(GROUP, HABITS))[1] == until


async def test_stay_check_when_the_session_already_ended_is_harmless(setup_dispatch):
    jev = W4Jev()
    app, dispatch, clock = await setup_dispatch(jev)
    await online_for(dispatch, 5)
    clock.advance(minutes=6)
    await dispatch._stay_check(GROUP, None)
    assert jev.stay_calls == []


async def test_stay_decision_does_not_hold_up_the_next_turn(setup_dispatch):
    release = asyncio.Event()

    class SlowStay(W4Jev):
        async def decide(self, state, questions):
            if "stay" in questions:
                await release.wait()
            return await super().decide(state, questions)

    app, dispatch, _ = await setup_dispatch(SlowStay())
    await online_for(dispatch, 5)
    await handle_message(app, tg())
    while len(app.bot.of("send", GROUP)) < 1:
        await asyncio.sleep(0)
    while not dispatch.jev.stay_calls and not release.is_set() and not dispatch._tasks:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert dispatch._slots == {}  # the chat's turn slot is free while Jev is still deciding
    release.set()
    await drain_dispatch(dispatch)


async def test_cancelled_attempt_starts_no_decision_and_the_restart_starts_exactly_one(setup_dispatch, monkeypatch):
    jev = W4Jev(done=1, stay="a_while")
    app, dispatch, _ = await setup_dispatch(jev)
    until = await online_for(dispatch, 5)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    complete = app.client.complete

    async def generate(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(app.client, "complete", generate)
    await handle_message(app, tg(text="first thought"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert jev.stay_calls == []
    await handle_message(app, tg(msg_id=2, text="and a detail"))
    await asyncio.wait_for(drain_dispatch(dispatch), timeout=2)

    assert cancelled.is_set() and len(app.bot.of("send", GROUP)) == 1
    assert len(jev.stay_calls) == 1  # not lost, not twice
    assert (await dispatch.presence.state(GROUP, HABITS))[1] == until + timedelta(minutes=30)  # one extension, not two
    assert dispatch._stay_marks == {GROUP: 2}


async def test_a_turn_that_stays_quiet_asks_nothing(setup_dispatch):
    jev = W4Jev()
    app, dispatch, _ = await setup_dispatch(jev)
    app.script.contents[:] = [json.dumps({"no_response": True})]
    until = await online_for(dispatch, 5)
    await deliver(app)
    assert jev.stay_calls == [] and (await dispatch.presence.state(GROUP, HABITS))[1] == until


# ---- 4. timing and accounting ------------------------------------------------------------------

async def test_the_stay_call_is_recorded_as_jev_time_on_the_turn_and_in_usage(setup_dispatch, jev_http):
    app, dispatch, clock = await setup_dispatch()
    await online_for(dispatch, 5)

    def jev_reply(request):
        questions = json.loads(request.content)["questions"]
        if "stay" in questions:
            answers = {"stay": {"type": "choice", "choice": "a_little", "confidence": 1}}
        else:
            answers = {"action": {"type": "choice", "choice": "reply", "confidence": 1}}
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev_reply)) as http:
        dispatch.jev = jev_http(http, dispatch)
        await deliver(app)

    [row] = await app.store.recent_turns()
    calls = json.loads(row["model_calls"])
    assert sorted(call["kind"] for call in calls) == ["chat", "triage", "triage"]  # the stay call is Jev, not chat
    assert row["calls"] == 3
    trace = json.loads(row["trace"])
    assert "Jev stay=10 minutes" in trace and any(item.startswith("after_reply@") for item in trace)
    usage = app.store._conn.execute("SELECT kind, chat_id FROM usage ORDER BY id").fetchall()
    assert [(r["kind"], r["chat_id"]) for r in usage] == [("triage", GROUP), ("triage", GROUP)]  # Jev, attributed to this chat


async def test_dashboard_counts_the_after_reply_stage_apart_from_other_time():
    row = {"last_bubble_ms": 5000, "model_calls": "[]"}
    _turn_timings(row, ["triage@300", "model@2000", "typing@1000", "after_reply@700"])
    stages = {name: ms for name, _, _, ms in row["segments"]}
    assert stages["after_reply"] == 700 and stages["other"] == 1700  # 5000 - 300 - 2000 - 1000; not 1000
    assert stages["triage"] == 300


async def test_extend_turn_adds_to_the_saved_row(make_app):
    from datetime import datetime, timezone

    app = make_app()
    row_id = await app.store.record_turn(datetime(2026, 1, 1, tzinfo=timezone.utc), GROUP, "group", 1, ["final"], "m", False, 5, "Replied")
    assert isinstance(row_id, int)
    await app.store.extend_turn(row_id, ["after_reply@9"], [{"kind": "triage"}])
    await app.store.extend_turn(row_id + 99, ["x"], [])  # a pruned row is ignored
    [row] = await app.store.recent_turns()
    assert (row["calls"], json.loads(row["trace"]), json.loads(row["model_calls"])) == (
        2, ["final", "after_reply@9"], [{"kind": "triage"}],
    )


async def test_a_session_that_ran_into_sleep_is_not_extended(setup_dispatch):
    """go_online does not stop at the start of sleep, so a session can still have minutes left once sleep has begun:
    no extension then (and no Jev call), unless the owner woke Dom."""
    jev = W4Jev(stay="a_while")
    app, dispatch, clock = await setup_dispatch(jev)
    clock.at = clock.at.replace(hour=23, minute=50)  # the sleep window starts at 23:59
    await online_for(dispatch, 15)  # online until 00:05
    clock.at = clock.at.replace(hour=23, minute=59, second=5)
    assert await dispatch.presence.stay_room(GROUP, HABITS) == (0, 0)
    with pytest.raises(ValueError):
        await dispatch.presence.extend_online(GROUP, HABITS, 30)
    await dispatch._stay_check(GROUP, None)
    assert jev.stay_calls == []


@pytest.mark.parametrize("first, calls, added", [("a_while", 1, 30), ("no", 2, 30)])
async def test_a_reply_during_a_stay_decision_waits_for_it(setup_dispatch, first, calls, added):
    """Two quick replies never run two decisions at once (that could add two extensions in one go). The later reply is
    judged after the first decision, which it sees: after an extension there is plenty left, so Jev is not asked again."""
    release, asked = asyncio.Event(), asyncio.Event()

    class Gated(W4Jev):
        async def decide(self, state, questions):
            if "stay" in questions and not asked.is_set():
                asked.set()
                await release.wait()
                self.stay = first
                answer = await super().decide(state, questions)
                self.stay = "a_while"
                return answer
            return await super().decide(state, questions)

    jev = Gated()
    app, dispatch, _ = await setup_dispatch(jev)
    until = await online_for(dispatch, 5)
    task = dispatch._stay_running[GROUP] = asyncio.create_task(dispatch._stay_checks(GROUP, None))
    await asyncio.wait_for(asked.wait(), timeout=2)
    dispatch._stay_pending[GROUP] = None  # what a second reply finishing now records
    release.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(jev.stay_calls) == calls
    assert (await dispatch.presence.state(GROUP, HABITS))[1] == until + timedelta(minutes=added)
    assert GROUP not in dispatch._stay_running and GROUP not in dispatch._stay_pending

