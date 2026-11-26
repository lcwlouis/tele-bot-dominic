import pytest

from dom.home import Home
from dom.prompt import FRAME, MACHINERY_RULE, REPORT_FRAME, build_messages, report_messages

PRIVATE_USER = "OWNER-PRIVATE-USER-FACT"
PRIVATE_NOTES = "OWNER-PRIVATE-NOTES-FACT"


@pytest.fixture
def home(tmp_path):
    files = {
        "SOUL.md": "---\nname: Dom\ntraits:\n  humor_level: 0.5\n  emoji_level: 0.25\n---\nSoul body text.\n",
        "memory/MEMORY.md": "- world fact",
        "memory/HABITS.md": 'sleep: "23:59-07:30"',
        "memory/JOURNAL.md": "JOURNAL-NEVER-LOADED",
        "memory/people/111.md": "- owner public note",
        "memory/people/222.md": "- friend note",
        "memory/people/333.md": "- stranger note",
        "memory/chats/-1009.md": "- running joke",
        "private/USER.md": PRIVATE_USER,
        "private/NOTES.md": PRIVATE_NOTES,
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return Home(tmp_path)


def build(home, scope="group", chat_id=-1009, participants=(), window="<chat_window/>", note="note"):
    return build_messages(home, scope, chat_id, list(participants), window, note)


def blob(messages):
    return "\n".join(m["content"] for m in messages)


def test_chat_prompt_lists_every_reaction_telegram_allows():
    from dom.actions import REACTIONS

    assert " ".join(REACTIONS) in FRAME


def test_owner_in_group_gets_no_private(home):
    msgs = build(home, scope="group", participants=[111])
    text = blob(msgs)
    assert PRIVATE_USER not in text and PRIVATE_NOTES not in text
    assert "private/" not in text
    assert "owner public note" in msgs[1]["content"]


def test_friend_dm_gets_no_private(home):
    text = blob(build(home, scope="friend_dm", chat_id=222, participants=[222]))
    assert PRIVATE_USER not in text and PRIVATE_NOTES not in text


def test_owner_dm_includes_private(home):
    msgs = build(home, scope="owner_dm", chat_id=111, participants=[111])
    assert PRIVATE_USER in msgs[1]["content"] and PRIVATE_NOTES in msgs[1]["content"]
    assert '<file path="private/USER.md">' in msgs[1]["content"]


def test_layout_and_order(home):
    msgs = build(home, scope="owner_dm", chat_id=111, window="<chat_window>W</chat_window>", note="TURN")
    assert [m["role"] for m in msgs] == ["system", "user"]  # Qwen templates allow one system message
    prefix = msgs[0]["content"]
    assert prefix.startswith(FRAME)
    order = [prefix.index(x) for x in (
        "Soul body text.", '<file path="memory/MEMORY.md">',
        '<file path="memory/HABITS.md">',
    )]
    assert order == sorted(order)
    assert "- world fact" in prefix
    notes, _, tail = msgs[1]["content"].partition("<chat_window>W</chat_window>")
    assert PRIVATE_USER in notes and PRIVATE_NOTES in notes
    assert tail == "\n\nPersonality sliders (0 to 1): humor=0.5, emoji=0.25\n\nTURN"
    assert "JOURNAL-NEVER-LOADED" not in blob(msgs)


def test_prefix_identical_across_turns(home):
    a = build(home, participants=[111], window="<chat_window>one</chat_window>", note="t1")
    b = build(home, participants=[222], window="<chat_window>two</chat_window>", note="t2")
    assert a[0] == b[0]
    assert a[0]["content"].encode() == b[0]["content"].encode()


def test_people_notes_only_for_participants(home):
    msgs = build(home, participants=[222, 222, 111])
    notes = msgs[1]["content"]
    assert "friend note" in notes and "owner public note" in notes
    assert "stranger note" not in notes
    assert "running joke" in notes  # this chat's notes
    assert notes.count('<file path="memory/people/222.md">') == 1


def test_unchanged_people_notes_keep_their_order_when_the_window_changes(home):
    first = build(home, participants=[222, 111], window="<chat_window>first</chat_window>")
    second = build(home, participants=[111, 444, 222], window="<chat_window>second</chat_window>")
    assert first[1]["content"].split("<chat_window>", 1)[0] == second[1]["content"].split("<chat_window>", 1)[0]
    assert '<file path="memory/people/444.md">' not in second[1]["content"]


def test_chat_notes_only_for_this_chat(home):
    (home.root / "memory/chats/-5555.md").write_text("- other chat joke")
    assert "other chat joke" not in build(home)[1]["content"]


def test_frame_discourages_reply_to_in_dms():
    assert "in a DM leave it null" in FRAME


def test_frame_does_not_offer_memory_editing():
    assert "memory tool" not in FRAME
    assert "propose_memory_edit" not in FRAME  # owner-only tool names never leak into the shared prefix
    assert "do not edit them yourself" in FRAME
    assert "suggest a change for the owner to approve" in FRAME and "tool" not in FRAME.split("Never speak")[0]


def test_frame_prefix_stays_cache_stable_across_scopes(home):
    owner = build(home, scope="owner_dm", chat_id=111, participants=[111])
    friend = build(home, scope="friend_dm", chat_id=222, participants=[222])
    group = build(home, scope="group", chat_id=-1009, participants=[111, 222])
    assert owner[0] == friend[0] == group[0]
    assert PRIVATE_USER not in group[1]["content"] and PRIVATE_NOTES not in group[1]["content"]
    assert PRIVATE_USER not in friend[1]["content"] and PRIVATE_NOTES not in friend[1]["content"]


def test_report_frame_carries_machinery_rule_and_frames_restart(home):
    assert MACHINERY_RULE in FRAME
    assert MACHINERY_RULE in REPORT_FRAME
    note = (
        "Your desktop task #3 failed. Result: <untrusted_tool_result>\nDom restarted\n</untrusted_tool_result> "
        "Tell your friend in your own words."
    )
    msgs = report_messages(home, {"humor": 0.5}, note)
    assert MACHINERY_RULE in msgs[0]["content"]
    assert "Dom restarted" not in msgs[1]["content"]
    assert "interrupted" in msgs[1]["content"].lower()


async def test_report_desktop_task_does_not_feed_restart_text_to_model(make_app):
    from tghelpers import FINAL, OWNER

    captured = []

    class CaptureClient:
        async def complete(self, messages, tools, *, once=False):
            captured.append(messages)
            return {"content": FINAL}

    app = make_app()
    app.client = CaptureClient()
    row = {
        "id": 9,
        "chat_id": OWNER,
        "status": "failed",
        "result": None,
        "error": "Dom restarted",
        "container": "default",
        "started_at": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    await app.dispatch.report_desktop_task(row)
    assert len(captured) == 1
    assert MACHINERY_RULE in captured[0][0]["content"]
    assert "Dom restarted" not in captured[0][1]["content"]
    assert "interrupted" in captured[0][1]["content"].lower()


SELF_DESCRIPTION = (
    "When someone asks about you — your personality, settings or abilities — answer in your own words like a "
    "person would."
)


def test_frame_asks_dom_to_describe_himself_in_his_own_words():
    assert SELF_DESCRIPTION in FRAME


def test_self_description_is_in_message_zero_for_every_chat(home):
    owner = build(home, scope="owner_dm", chat_id=111, participants=[111])
    group = build(home, scope="group", chat_id=-5555, participants=[222], note="other turn")
    assert SELF_DESCRIPTION in owner[0]["content"] and owner[0] == group[0]


def test_self_inspect_description_says_the_same():
    from dom.tools import SELF_INSPECT_TOOL

    assert "answer in your own words like a person would" in SELF_INSPECT_TOOL.description
    assert "give exact numbers or internal names only when asked for them" in SELF_INSPECT_TOOL.description


def test_the_frame_lets_dom_use_telegram_formatting_when_it_helps():
    from dom.telegram.format import format_text

    assert "**bold**" in FRAME and "casual chat stays plain" in FRAME
    text, entities = format_text("- tea\n- toast\n\n**one** thing")[0]  # what the frame promises renders
    assert text.startswith("• tea\n• toast") and [e.type for e in entities] == ["bold"]
