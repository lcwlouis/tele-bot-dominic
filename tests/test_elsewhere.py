import json
import random
import re

import pytest

from dom.attention import FACT_INSTRUCTION, Attention, filter_fact
from dom.presence import parse_habits
from dom.telegram.handlers import handle_message
from test_cross_chat import call_tool
from tghelpers import FRIEND, GROUP, OWNER, Clock, drain_dispatch, tg, turn_requests


async def test_elsewhere_rides_on_the_final_reply_and_leaves_the_cache_prefix_stable(make_app):
    fact = "We agreed to bring supplies."
    app = make_app(contents=[
        '{"bubbles":["got it"]}',
        json.dumps({"bubbles": ["will do"], "elsewhere": fact}),
        '{"bubbles":["hey"]}',
    ])
    for chat in (GROUP, OWNER):
        await app.store.set_chat_auth_status(chat, "approved")
        await app.dispatch.presence.go_online(chat, parse_habits(""))

    async def send(chat, sender=FRIEND):
        msg = tg(chat_id=chat, sender=sender, text=f"message in {chat}").model_copy(update={"date": app.dispatch.now()})
        await handle_message(app, msg)
        await drain_dispatch(app.dispatch)

    await send(OWNER, OWNER)
    await send(GROUP)
    app.dispatch.now.advance(minutes=4)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, msg_id=2))
    await drain_dispatch(app.dispatch)
    before, group, after = turn_requests(app)
    assert before["messages"][0] == group["messages"][0] == after["messages"][0]
    assert json.dumps(before["tools"]) == json.dumps(after["tools"])
    assert FACT_INSTRUCTION in group["messages"][-1]["content"]
    assert fact not in before["messages"][-1]["content"]
    assert after["messages"][-1]["content"].endswith(f'- 4 min ago in "The Group" (group): {fact}')


def test_only_owner_sees_other_chats_facts_across_random_visit_sequences():
    rng = random.Random(123)
    loop, clock = Attention(), Clock()
    chats = [(GROUP, "group"), (GROUP - 1, "group"), (FRIEND, "friend_dm"), (OWNER, "owner_dm")]
    recent = []
    previous = None
    for idx in range(250):
        chat, scope = rng.choice(chats)
        visible = loop.elsewhere(chat, scope, clock())
        if scope == "owner_dm":
            candidates = [(at, fact) for at, fact in recent if (clock() - at).total_seconds() < 1800][-3:]
            assert len(visible.splitlines()) == len(candidates)
            for line, (_, fact) in zip(visible.splitlines(), reversed(candidates)):
                assert line.endswith(fact)
            assert "owner secret" not in visible
        else:
            expected = "you were just in another conversation" if (
                previous is not None and previous[0] != chat and (clock() - previous[1]).total_seconds() < 600
            ) else ""
            assert visible == expected
        fact = f"Unique fact {idx}" if scope != "owner_dm" else "owner secret"
        loop.finish_conversation(chat, scope, clock(), title=f"Private title {idx}", fact=fact)
        if scope != "owner_dm":
            recent.append((clock(), fact))
        previous = chat, clock()
        clock.advance(minutes=rng.randrange(0, 16))


@pytest.mark.parametrize(("line", "drops"), [
    ("They said ALPHA, beta; gamma delta, Epsilon zeta.", True),
    ("They said alpha beta gamma delta something else.", False),
    ('They said “one two three four” at lunch.', True),
    ("They said 'one two three four' at lunch.", True),
    ('They said "one two three" at lunch.', False),
])
def test_fact_filter_rejects_word_runs_and_long_quotes(line, drops):
    assert bool(filter_fact(line, ["alpha beta gamma delta epsilon zeta"])) is not drops


def test_fact_cap_length_expiry_and_restart():
    loop, clock = Attention(), Clock()
    for index in range(5):
        fact = filter_fact(f"Fact {index} " + "a" * 200, [])
        assert len(fact) == 120
        loop.finish_conversation(GROUP, "group", clock(), title='Test <room> "A"', fact=fact)
        clock.advance(minutes=1)
    owner_note = loop.elsewhere(OWNER, "owner_dm", clock())
    assert [line.split(": ", 1)[-1][:6] for line in owner_note.splitlines()] == ["Fact 4", "Fact 3", "Fact 2"]
    assert '&lt;room&gt; &quot;A&quot;' in owner_note
    assert not Attention().elsewhere(OWNER, "owner_dm", clock())
    clock.advance(minutes=9)
    assert not loop.elsewhere(FRIEND, "friend_dm", clock())
    clock.advance(minutes=20)
    assert not loop.elsewhere(OWNER, "owner_dm", clock())


@pytest.mark.parametrize(("scope", "chat"), [("group", GROUP), ("friend_dm", FRIEND)])
async def test_non_owner_note_and_tool_results_keep_machinery_out(make_app, scope, chat):
    app = make_app()
    await app.store.set_chat_auth_status(chat, "approved")
    await app.dispatch.presence.go_online(chat, parse_habits(""))
    app.dispatch.attention.finish_conversation(OWNER, "owner_dm", app.dispatch.now(), fact="owner secret")
    await handle_message(app, tg(chat_id=chat))
    await drain_dispatch(app.dispatch)
    [turn] = turn_requests(app)
    note = turn["messages"][-1]["content"].split("</chat_window>", 1)[1]
    results = []
    for _ in range(2):
        results.append(await call_tool(
            app, scope, chat, "tell_owner", text="Could use your help.", reason="Someone asked",
        ))
    assert note.endswith("you were just in another conversation")
    assert not re.search(r"\b(loop|queue|visit|card|timing)\b", "\n".join([note, *results]), re.IGNORECASE)
    assert "owner secret" not in note
    assert results[0] == "Sent to the owner." and results[1].startswith("error:")
