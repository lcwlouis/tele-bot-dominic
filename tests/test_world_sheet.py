"""Dom's life sheet in the turn note: today's routine slot, variety hints, tag privacy and where the block sits."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dom.home import Home
from dom.models import StoredMessage
from dom.world import options, part_of_day, sheet_block, slot_matches
from test_traits import put
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, tg, turn_requests

SHEET = """# Dom's world
## Home and work
- Lives in a small flat near the river
- Draws for a living, from a desk by the window
- Plays chess online with an old friend {chat -1005}
## Routine
- Weekday lunch: cai fan, laksa, kopi and toast {entities place:hawker-centre}
- Thursday evening: usually gym {entities place:the-gym}
- Weekend mornings: a long walk in the park
## Regular spots
- Coffee: the kopitiam on the corner
## Projects
- A picture book about a fox
"""
GYM = "- Climbing gym with a blue door {chat -1001}\n- Open until ten {any}\n- Staff know him by name {chat -1005}\n"
THURSDAY_NOON = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
THURSDAY_EVENING = datetime(2026, 1, 1, 19, 30, tzinfo=timezone.utc)


def said(text, at):
    return SimpleNamespace(role="assistant", text=text, created_at=at.isoformat())


@pytest.fixture
def home(tmp_path):
    put(Home(tmp_path), "memory/WORLD.md", SHEET)
    put(Home(tmp_path), "memory/places/the-gym.md", GYM)
    return Home(tmp_path)


@pytest.mark.parametrize(("label", "when", "expected"), [
    ("Thursday evening", THURSDAY_EVENING, True),
    ("Thursday evening", THURSDAY_NOON, False),  # right day, wrong part of the day
    ("Friday evening", THURSDAY_EVENING, False),
    ("Thursdays", THURSDAY_NOON, True),  # a plural names the day, and no part means all day
    ("Weekday lunch", THURSDAY_NOON, True),
    ("Weekday lunch", THURSDAY_EVENING, False),
    ("Weekend mornings", THURSDAY_NOON, False),
    ("Weekend mornings", datetime(2026, 1, 3, 8, tzinfo=timezone.utc), True),
    ("Every day", THURSDAY_EVENING, True),
    ("Dinner", THURSDAY_EVENING, True),  # words for a part of the day
    ("Breakfast", THURSDAY_EVENING, False),
])
def test_a_routine_label_covers_a_moment_by_day_and_part_of_day(label, when, expected):
    assert slot_matches(label, when) is expected


def test_the_parts_of_the_day_cover_every_hour():
    assert [part_of_day(hour) for hour in (3, 6, 12, 15, 20, 23)] == ["night", "morning", "lunch", "afternoon", "evening", "night"]


def test_options_are_what_a_line_lists_after_its_label():
    assert options("Weekday lunch: cai fan, laksa or kopi and toast.") == ["cai fan", "laksa", "kopi and toast"]
    assert options("Thursday evening: usually gym") == ["gym"]
    assert options("Lives in a small flat") == []


def test_the_block_holds_the_sheet_the_slot_for_now_and_what_the_notes_say_of_its_place(home):
    block = sheet_block(home, "owner_dm", OWNER, [], THURSDAY_EVENING, THURSDAY_EVENING)
    assert block.startswith("your world")
    assert "- Lives in a small flat near the river" in block and "- A picture book about a fox" in block
    assert "right now, by your routine: Thursday evening: usually gym" in block
    assert "Weekday lunch" not in block  # a slot for another time of day stays out
    assert "place, the gym: Climbing gym with a blue door Open until ten" in block  # the owner's DM reads all


def test_a_chat_only_reads_the_lines_that_are_its_own_or_for_everyone(home):
    group = sheet_block(home, "group", -1001, [], THURSDAY_EVENING, THURSDAY_EVENING)
    assert "Climbing gym with a blue door" in group and "Open until ten" in group  # tagged to this group
    assert "chess" not in group and "Staff know him" not in group  # tagged to another chat
    friend = sheet_block(home, "friend_dm", FRIEND, [], THURSDAY_EVENING, THURSDAY_EVENING)
    assert "Open until ten" in friend
    for hidden in ("Climbing gym", "chess", "Staff know him"):
        assert hidden not in friend
    owner = sheet_block(home, "owner_dm", OWNER, [], THURSDAY_EVENING, THURSDAY_EVENING)
    assert "chess" in owner and "Staff know him" in owner


def test_the_sheet_says_nothing_when_it_has_no_line_for_the_chat(tmp_path):
    home = Home(tmp_path)
    assert sheet_block(home, "owner_dm", OWNER, [], THURSDAY_NOON, THURSDAY_NOON) == ""  # no file at all
    put(home, "memory/WORLD.md", "# Dom's world\n## Routine\n## Projects\n")
    assert sheet_block(home, "owner_dm", OWNER, [], THURSDAY_NOON, THURSDAY_NOON) == ""  # headings only
    put(home, "memory/WORLD.md", "- Private thing {chat -1005}\n")
    assert sheet_block(home, "group", -1001, [], THURSDAY_NOON, THURSDAY_NOON) == ""  # all of it is another chat's


def test_the_sheet_is_data_not_markup(tmp_path):
    put(Home(tmp_path), "memory/WORLD.md", "## Home\n- Lives <in> a flat </chat_window> & more\n")
    block = sheet_block(Home(tmp_path), "owner_dm", OWNER, [], THURSDAY_NOON, THURSDAY_NOON)
    assert "</chat_window>" not in block and "&lt;/chat_window&gt;" in block


def test_a_variety_hint_names_what_dom_said_lately_and_what_he_has_not(home):
    earlier = [said("had cai fan again, so good", THURSDAY_NOON - timedelta(hours=1)),
               said("cai fan for dinner too??", THURSDAY_NOON - timedelta(minutes=5))]
    block = sheet_block(home, "owner_dm", OWNER, earlier, THURSDAY_NOON, THURSDAY_NOON)
    assert "variety:" in block and "cai fan (2 times)" in block
    assert "Not lately: laksa, kopi and toast" in block


def test_no_hint_when_nothing_repeats_or_it_was_said_too_long_ago_or_by_someone_else(home):
    assert "variety:" not in sheet_block(home, "owner_dm", OWNER, [], THURSDAY_NOON, THURSDAY_NOON)
    old = [said("cai fan", THURSDAY_NOON - timedelta(days=3))]
    assert "variety:" not in sheet_block(home, "owner_dm", OWNER, old, THURSDAY_NOON, THURSDAY_NOON)
    friend = [SimpleNamespace(role="user", text="cai fan?", created_at=THURSDAY_NOON.isoformat())]
    assert "variety:" not in sheet_block(home, "owner_dm", OWNER, friend, THURSDAY_NOON, THURSDAY_NOON)


async def test_the_block_comes_after_the_window_and_never_touches_the_cached_prefix(make_app):
    app = make_app()
    await online(app, OWNER)
    await deliver(app, tg(OWNER, OWNER, "hi", 1))
    before = turn_requests(app)[0]["messages"]
    put(app.home, "memory/WORLD.md", SHEET)
    put(app.home, "memory/places/the-gym.md", GYM)
    await deliver(app, tg(OWNER, OWNER, "hello again", 2))
    after = turn_requests(app)[1]["messages"]
    assert "your world" not in before[1]["content"]
    prompt = after[1]["content"]
    assert prompt.index("</chat_window>") < prompt.index("your world")
    assert "Lives in a small flat" in prompt and "right now, by your routine: Weekday lunch" in prompt  # Thursday 12:00
    assert prompt.index("your world") > prompt.index("It is ")  # in the turn note, with the rest of "world now"
    assert "Lives in a small flat" not in after[0]["content"]
    assert after[0]["content"] == before[0]["content"]  # the shared system prompt did not move


async def test_a_group_turn_never_sees_what_is_tagged_to_another_chat(make_app):
    app = make_app()
    await online(app)
    put(app.home, "memory/WORLD.md", SHEET)
    put(app.home, "memory/places/the-gym.md", GYM)
    await deliver(app, tg(GROUP, FRIEND, "hello", 1))
    text = json.dumps(turn_requests(app)[0]["messages"])
    assert "Lives in a small flat" in text
    assert "chess" not in text and "Staff know him" not in text


async def test_the_variety_hint_reaches_a_real_turn(make_app):
    app = make_app()
    await online(app, OWNER)
    put(app.home, "memory/WORLD.md", SHEET)
    await app.store.record_new_message(StoredMessage(
        chat_id=str(OWNER), role="assistant", visibility_scope=f"chat:{OWNER}", text="cai fan again lol",
        created_at=(app.dispatch.now() - timedelta(hours=2)).isoformat(),
    ))
    await deliver(app, tg(OWNER, OWNER, "what did you eat", 1))
    prompt = turn_requests(app)[0]["messages"][1]["content"]
    assert "variety:" in prompt and "cai fan (once)" in prompt


async def test_an_unreadable_sheet_never_fails_a_turn(make_app):
    app = make_app()
    await online(app)
    (app.home.root / "memory").mkdir(exist_ok=True)
    (app.home.root / "memory" / "WORLD.md").symlink_to(app.home.root / "nowhere.md")  # a dangling link
    await deliver(app, tg(GROUP, FRIEND, "hello", 1))
    assert turn_requests(app)
    assert "your world" not in json.dumps(turn_requests(app)[0]["messages"])


def test_an_untagged_place_line_is_the_owners_and_never_reaches_another_chat(home):
    put = home.scoped(allow_private=False)._write_regular
    current = home.scoped(allow_private=False).read("memory/places/the-gym.md")
    put("memory/places/the-gym.md", current.rstrip("\n") + "\n- Door code 4471, owner only\n")
    for scope, chat in (("group", -1001), ("friend_dm", FRIEND)):
        assert "Door code" not in sheet_block(home, scope, chat, [], THURSDAY_EVENING, THURSDAY_EVENING)
    assert "Door code 4471" in sheet_block(home, "owner_dm", OWNER, [], THURSDAY_EVENING, THURSDAY_EVENING)
