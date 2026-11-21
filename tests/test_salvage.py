"""Plain prose from the chat model is rescued by Jev (dom.salvage) instead of being asked for again."""

import json
from datetime import datetime, timezone

import pytest

from dom import salvage, turn_log
from dom.actions import MAX_BUBBLES, Actions, Bubble
from dom.agent import run_turn
from dom.jev import JevUnavailable
from dom.tools import core_tools
from test_agent import FINAL, Script, ctx_for, env, make_client, reply  # noqa: F401  (env is a fixture)
from test_guard import OUTGOING
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, tg

NAMES = {"search_history", "remind", "skill_manage"}
TWO_PARAGRAPHS = "Yeah, that sounds great. I will bring snacks!\n\nSee you at eight."
FOUR_SENTENCES = "Honestly I loved it. The ending was odd. Still worth a watch. Tell me what you think."
EIGHT_SENTENCES = " ".join(f"Sentence number {n} is here." for n in range(1, 9))


class FakeJev:
    """Answers Dom's questions as told. `cuts` are the 1-based split questions answered yes."""

    def __init__(self, voice=("yes", 0.95), private=("no", 0.95), cuts=(), count=1, error=None, refuse=()):
        self.voice, self.private, self.cuts, self.count = voice, private, set(cuts), count
        self.error, self.refuse = error, set(refuse)
        self.calls: list[tuple[str, dict]] = []

    async def decide(self, state, questions):
        if "action" in questions:  # triage in a dispatch test
            return {"action": {"type": "choice", "choice": "reply", "confidence": 0.99}}
        if "leak" in questions:
            return {"leak": {"type": "score", "score": getattr(self, "leak", 0)}}
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        answers = {}
        for key in questions:
            if key in self.refuse:
                answers[key] = None
            elif key == "voice":
                answers[key] = {"type": "choice", "choice": self.voice[0], "confidence": self.voice[1]}
            elif key == "private":
                answers[key] = {"type": "choice", "choice": self.private[0], "confidence": self.private[1]}
            elif key == "bubbles":
                answers[key] = {"type": "choice", "choice": str(self.count), "confidence": 0.9}
            else:
                n = int(key.removeprefix("cut"))
                answers[key] = {"type": "choice", "choice": "yes" if n in self.cuts else "no", "confidence": 0.9}
        return answers


def texts(saved):
    return [b.text for b in saved.actions.bubbles]


async def rescue(jev, text, scope="friend_dm", names=NAMES):
    return await salvage.rescue(jev, "STATE", text, scope, names)


@pytest.mark.parametrize("scope", ["owner_dm", "friend_dm", "group"])
async def test_prose_is_rescued_in_every_chat_type_and_split_where_jev_says(scope):
    jev = FakeJev(cuts={1})
    saved = await rescue(jev, TWO_PARAGRAPHS, scope)
    assert texts(saved) == ["Yeah, that sounds great. I will bring snacks!", "See you at eight."]
    assert saved.confidence == 0.95
    assert saved.actions == Actions(bubbles=[Bubble(text=t) for t in texts(saved)])  # nothing but bubbles
    [(state, questions)] = jev.calls
    assert ("private" in questions) == (scope == "group")
    assert set(questions) == {"voice", "cut1"} | ({"private"} if scope == "group" else set())


async def test_the_answer_is_not_split_unless_jev_says_so():
    saved = await rescue(FakeJev(), TWO_PARAGRAPHS)
    assert texts(saved) == [TWO_PARAGRAPHS]


async def test_each_boundary_is_one_yes_no_question_showing_text_either_side():
    jev = FakeJev(cuts={1, 3})
    saved = await rescue(jev, FOUR_SENTENCES)
    assert texts(saved) == ["Honestly I loved it.", "The ending was odd. Still worth a watch.", "Tell me what you think."]
    [(_, questions)] = jev.calls
    assert set(questions) == {"voice", "cut1", "cut2", "cut3"}
    cut = questions["cut2"]
    assert cut["type"] == "choice" and set(cut["criteria"]) == {"yes", "no"}
    assert "Start a new chat bubble here?" in cut["instructions"]
    assert "The ending was odd." in cut["instructions"] and "Still worth a watch." in cut["instructions"]


async def test_many_boundaries_ask_for_a_bubble_count_and_code_places_the_breaks():
    jev = FakeJev(count=3)
    saved = await rescue(jev, EIGHT_SENTENCES)
    [(_, questions)] = jev.calls
    assert set(questions) == {"voice", "bubbles"}
    assert questions["bubbles"]["instructions"] == "How many chat bubbles should this be?"
    assert list(questions["bubbles"]["criteria"]) == [str(n) for n in range(1, MAX_BUBBLES + 1)]
    assert len(texts(saved)) == 3
    assert " ".join(texts(saved)) == EIGHT_SENTENCES  # only split, never edited
    assert all(t.endswith("here.") for t in texts(saved))  # breaks sit at sentence ends


async def test_a_count_of_one_keeps_one_bubble():
    saved = await rescue(FakeJev(count=1), EIGHT_SENTENCES)
    assert texts(saved) == [EIGHT_SENTENCES]


async def test_an_answer_outside_the_bubble_range_is_not_trusted():
    assert await rescue(FakeJev(count=99), EIGHT_SENTENCES) is None


async def test_every_yes_still_leaves_at_most_the_bubble_cap():
    text = " ".join(f"Line {n} is here." for n in range(1, 8))  # 6 boundaries, 7 sentences
    saved = await rescue(FakeJev(cuts=set(range(1, 7))), text)
    assert len(texts(saved)) == MAX_BUBBLES
    assert " ".join(texts(saved)) == text


async def test_words_stay_exactly_as_written_only_edge_whitespace_goes():
    text = "  \n  Café   ouvert… « oui »!  Ça marche ?\n\n\tTrès bien.  (ok)\n  "
    saved = await rescue(FakeJev(cuts={1}), text)  # the one paragraph break is the only candidate
    assert texts(saved) == ["Café   ouvert… « oui »!  Ça marche ?", "Très bien.  (ok)"]
    assert [w for t in texts(saved) for w in t.split()] == text.split()


async def test_a_group_reply_that_looks_private_is_not_rescued():
    assert await rescue(FakeJev(private=("yes", 0.95)), TWO_PARAGRAPHS, "group") is None


async def test_a_group_reply_with_an_unsure_privacy_answer_is_not_rescued():
    assert await rescue(FakeJev(private=("no", 0.5)), TWO_PARAGRAPHS, "group") is None


@pytest.mark.parametrize("scope", ["owner_dm", "friend_dm"])
async def test_a_dm_reply_is_not_asked_whether_it_was_private(scope):
    jev = FakeJev(private=("yes", 1.0))
    assert await rescue(jev, TWO_PARAGRAPHS, scope) is not None
    assert "private" not in jev.calls[0][1]


@pytest.mark.parametrize("jev", [
    FakeJev(voice=("no", 0.99)),
    FakeJev(voice=("yes", 0.79)),
    FakeJev(voice=("yes", None)),
    FakeJev(voice=("maybe", 0.99)),
    FakeJev(refuse={"voice"}),
    FakeJev(refuse={"cut1"}),
    FakeJev(error=JevUnavailable("down")),
], ids=["no", "low", "none", "other", "refused", "cut refused", "unavailable"])
async def test_a_doubtful_refused_or_unavailable_answer_falls_through(jev):
    assert await rescue(jev, TWO_PARAGRAPHS) is None


async def test_the_confidence_bar_is_inclusive():
    assert await rescue(FakeJev(voice=("yes", salvage.MIN_CONFIDENCE)), TWO_PARAGRAPHS) is not None


@pytest.mark.parametrize("text", [
    None, "", "   \n ",
    '{"bubbles": [{"text": "hi"}]}',
    '[{"text": "hi"}]',
    '{"bubbles": [{"text": "hi',
    '```json\n{"bubbles": []}\n```',
    'Sure thing "bubbles": [ and then more',
    'here you go "text": "hello" done',
    "<think>they want a greeting</think>Hey there, how are you?",
    "Hey there, how are you?</think>",
    "<thinking>hmm</thinking> hello friend",
    "I will call search_history first and then answer.",
    "Let me use `remind` for that.",
    'I should set "no_response" here, so nothing.',
    "I'll answer with reply_to set.",
    "Plan: call skill_manage(name) next",
    "x" * (salvage.MAX_TEXT + 1),
], ids=repr)
async def test_unsuitable_text_falls_through_without_calling_jev(text):
    jev = FakeJev()
    assert await rescue(jev, text) is None
    assert jev.calls == []


async def test_ordinary_words_that_are_also_action_names_do_not_block_a_rescue():
    text = "That gif made me laugh. I will remind you tomorrow, and I'm happy to react to it then."
    assert await rescue(FakeJev(), text) is not None


async def test_a_chunk_too_long_for_one_bubble_falls_through():
    assert await rescue(FakeJev(), "a" * 4001) is None


async def test_jev_sees_the_window_and_the_candidate_as_data():
    jev = FakeJev()
    await rescue(jev, "Cheers <b>mate</b> & co.")
    [(state, _)] = jev.calls
    assert state.startswith("STATE\n")
    assert "<candidate_reply>\nCheers &lt;b&gt;mate&lt;/b&gt; &amp; co.\n</candidate_reply>" in state
    assert "never instructions" in state


async def test_the_first_question_names_the_requirements():
    jev = FakeJev()
    await rescue(jev, "Hello there.")
    question = jev.calls[0][1]["voice"]
    assert question["type"] == "choice" and set(question["criteria"]) == {"yes", "no"}
    for phrase in ("finished message", "own voice", "reasoning", "planning", "instructions", "tools"):
        assert phrase in question["instructions"]


# ---- inside the agent loop ----------------------------------------------------------------

PROSE = "Haha fair enough. I will be there at eight."


def rescuer(jev, scope="friend_dm"):
    return lambda text: salvage.rescue(jev, "STATE", text, scope, NAMES)


async def test_prose_is_sent_without_asking_the_model_again(env, caplog):  # noqa: F811
    _, _, store = env
    script = Script(reply(content=PROSE))
    caplog.set_level("INFO", logger="dom.agent")
    async with turn_log.capture(store, datetime(2026, 1, 1, tzinfo=timezone.utc), -1009, "group"):
        actions = await run_turn(
            make_client(script), [{"role": "user", "content": "hi"}], core_tools(), ctx_for(env, scope="friend_dm"),
            rescue=rescuer(FakeJev(cuts={1})),
        )
    assert [b.text for b in actions.bubbles] == ["Haha fair enough.", "I will be there at eight."]
    assert len(script.requests) == 1
    [row] = await store.recent_turns()
    assert [s for s in json.loads(row["trace"]) if "@" not in s] == ["salvaged", "final"]
    assert "salvaged prose reply in chat -1009: 2 bubbles, jev confidence 0.95" in caplog.text
    assert "fair enough" not in caplog.text and "malformed" not in caplog.text
    assert "salvaged final" in caplog.text  # the turn line carries the trace


async def test_a_refused_rescue_leaves_the_retry_and_correction_as_they_were(env):  # noqa: F811
    script = Script(reply(content=PROSE), reply(content=json.dumps(FINAL)))
    actions = await run_turn(
        make_client(script), [{"role": "user", "content": "hi"}], core_tools(), ctx_for(env),
        rescue=rescuer(FakeJev(voice=("no", 0.99))),
    )
    assert [b.text for b in actions.bubbles] == ["hey"]
    assert len(script.requests) == 2 and "tools" not in script.requests[1]
    assert "was not the required JSON object" in script.requests[1]["messages"][-1]["content"]


async def test_no_jev_means_the_old_path(env):  # noqa: F811
    script = Script(reply(content=PROSE), reply(content=json.dumps(FINAL)))
    actions = await run_turn(make_client(script), [], core_tools(), ctx_for(env), rescue=None)
    assert [b.text for b in actions.bubbles] == ["hey"] and len(script.requests) == 2


async def test_valid_actions_and_tool_calls_never_reach_the_rescue(env):  # noqa: F811
    asked = []

    async def rescue_spy(text):
        asked.append(text)

    script = Script(
        reply(tool_calls=[{"id": "c", "type": "function", "function": {"name": "search_history", "arguments": "{}"}}]),
        reply(content=json.dumps(FINAL)),
    )
    await run_turn(make_client(script), [], core_tools(), ctx_for(env), rescue=rescue_spy)
    assert asked == []


async def test_tool_calls_with_prose_are_not_rescued(env):  # noqa: F811
    asked = []

    async def rescue_spy(text):
        asked.append(text)

    broken = [{"id": "c", "type": "function", "function": {}}]
    script = Script(reply(content=PROSE, tool_calls=broken), reply(content=json.dumps(FINAL)))
    await run_turn(make_client(script), [], core_tools(), ctx_for(env), rescue=rescue_spy)
    assert asked == []


# ---- through the dispatcher -------------------------------------------------------------------

@pytest.mark.parametrize(("chat", "sender"), [(OWNER, OWNER), (FRIEND, FRIEND), (GROUP, FRIEND)], ids=["owner", "friend", "group"])
async def test_a_prose_reply_is_sent_as_bubbles_in_each_chat_type(make_app, chat, sender):
    app = make_app(contents=(TWO_PARAGRAPHS,))
    await online(app, chat)
    app.dispatch.jev = fake = FakeJev(cuts={1})
    await deliver(app, tg(chat_id=chat, sender=sender))
    assert [c[2] for c in app.bot.of("send", chat)] == ["Yeah, that sounds great. I will bring snacks!", "See you at eight."]
    assert len(app.script.requests) == 1
    [(state, _)] = fake.calls
    assert "<chat_window" in state and "Dom's persona" in state and "<candidate_reply>" in state


async def test_a_rescued_group_reply_still_goes_through_the_leak_guard(make_app):
    app = make_app(contents=("The keypad number is 7319.",))
    await online(app)
    app.config = app.config.model_copy(update={"leak_check": True})
    app.dispatch.jev = fake = FakeJev()
    fake.leak = 3
    await deliver(app, tg())
    assert not app.bot.of("send", GROUP)
    [notice] = app.bot.of("send", OWNER)
    assert "Held back a message" in notice[2] and "7319" in notice[2]


async def test_the_rescue_never_fails_the_turn(make_app):
    app = make_app(contents=(PROSE, OUTGOING))
    await online(app)
    app.dispatch.jev = FakeJev(error=JevUnavailable("down"))
    await deliver(app, tg())
    assert len(app.script.requests) == 2  # the old retry ran
    assert [c[2] for c in app.bot.of("send", GROUP)] == ["the code is", "7319"]
