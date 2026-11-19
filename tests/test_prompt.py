import pytest

from dom.home import Home
from dom.prompt import FRAME, build_messages

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
    assert msgs[1]["content"].startswith("Personality sliders (0 to 1): humor=0.5, emoji=0.25")
    assert "JOURNAL-NEVER-LOADED" not in blob(msgs)
    assert msgs[1]["content"].endswith("<chat_window>W</chat_window>\n\nTURN")


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


def test_chat_notes_only_for_this_chat(home):
    (home.root / "memory/chats/-5555.md").write_text("- other chat joke")
    assert "other chat joke" not in build(home)[1]["content"]


def test_frame_discourages_reply_to_in_dms():
    assert "in a DM leave it null" in FRAME


def test_frame_does_not_offer_memory_editing():
    assert "memory tool" not in FRAME
    assert "cannot edit them" in FRAME
