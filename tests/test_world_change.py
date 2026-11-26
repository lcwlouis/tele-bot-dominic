"""Changes to Dom's life sheet: a reason, at most weekly, never over the owner's pinned or chat-tagged lines."""

import json
from datetime import date, timedelta

from dom import reflection, world_change
from dom.home import WORLD, content_version, limit_for
from test_reflection import PUBLIC, record, title
from test_traits import put
from tghelpers import GROUP, OWNER

DAY = date(2026, 1, 1)
START = "- Lives in a small flat\n- Works from home\n"


def edit(*ops, reason="he moved closer to the park"):
    return {"reason": reason, "ops": list(ops)}


def add(text, **extra):
    return {"op": "add", "text": text, **extra}


async def test_a_change_with_a_reason_is_written_and_remembered(make_app):
    app = make_app()
    put(app.home, WORLD, START)
    assert await world_change.apply(app, edit(
        {"op": "replace", "old": "Lives in a small flat", "text": "Lives in a flat by the park"},
        add("Thursday evening: usually gym", entities=["place:the-gym", "nonsense"]),
    ), DAY)
    text = app.home.read(WORLD)
    assert "- Lives in a flat by the park" in text and "- Thursday evening: usually gym {entities place:the-gym}" in text
    assert "small flat" in app.home.read("memory/WORLD.former.md")  # the replaced line is kept, with its day
    found = await world_change.last_change(app)
    assert found == (DAY, "he moved closer to the park")
    events = [row[0] for row in app.store._conn.execute("SELECT text FROM messages WHERE role = 'event' AND chat_id = ?", (str(OWNER),))]
    assert any("he moved closer to the park" in text for text in events)  # the owner's DM remembers it


async def test_a_change_applies_at_most_once_a_week(make_app):
    app = make_app()
    put(app.home, WORLD, START)
    assert await world_change.apply(app, edit(add("Plays chess online")), DAY)
    for days in (1, 3, 6):
        assert not await world_change.due(app, DAY + timedelta(days=days))
        assert not await world_change.apply(app, edit(add(f"A new habit {days}")), DAY + timedelta(days=days))
    assert "new habit" not in app.home.read(WORLD)
    assert await world_change.due(app, DAY + timedelta(days=7))
    assert await world_change.apply(app, edit(add("A new habit"), reason="a week on"), DAY + timedelta(days=7))
    assert "A new habit" in app.home.read(WORLD)


async def test_a_change_needs_a_reason_and_ops(make_app):
    app = make_app()
    put(app.home, WORLD, START)
    for bad in (
        {"ops": [add("No reason")]}, edit(add("Blank reason"), reason="   "), edit(add("Not text"), reason=42),
        {"reason": "no ops"}, {"reason": "ops of the wrong kind", "ops": "add"}, "not an object", None, [],
    ):
        assert not await world_change.apply(app, bad, DAY)
    assert app.home.read(WORLD) == START
    assert await world_change.last_change(app) is None  # a refused change does not start the week


async def test_a_refused_op_does_not_start_the_week_but_a_good_one_next_to_it_does(make_app):
    app = make_app()
    put(app.home, WORLD, START)
    assert not await world_change.apply(app, edit({"op": "replace", "old": "not in the file", "text": "x"}), DAY)
    assert await world_change.last_change(app) is None
    assert await world_change.apply(app, edit(
        {"op": "remove", "old": "nowhere"}, {"op": "bogus"}, "junk", add("Walks to work"),
    ), DAY)
    assert app.home.read(WORLD).endswith("- Walks to work")


async def test_the_owners_pinned_lines_and_chat_tagged_lines_are_not_the_reflections_to_change(make_app):
    app = make_app()
    put(app.home, WORLD, "- Lives by the sea {core owner 2026-01-01}\n- Chess with a friend {chat -1005}\n- Works from home\n")
    assert not await world_change.apply(app, edit(
        {"op": "replace", "old": "Lives by the sea", "text": "Lives in the city"},
        {"op": "remove", "old": "Lives by the sea"},
        {"op": "remove", "old": "Chess with a friend"},
    ), DAY)
    assert app.home.read(WORLD) == "- Lives by the sea {core owner 2026-01-01}\n- Chess with a friend {chat -1005}\n- Works from home\n"
    assert await world_change.apply(app, edit(add("Lunch: cai fan, laksa {core owner 2026-01-01} {chat -1005}")), DAY)
    added = app.home.read(WORLD).splitlines()[-1]
    assert added == "- Lunch: cai fan, laksa"  # a model can neither pin nor tag a line


async def test_the_sheet_keeps_its_size_limit(make_app):
    app = make_app()
    put(app.home, WORLD, START)
    assert not await world_change.apply(app, edit(add("x" * (limit_for(WORLD) + 1))), DAY)
    assert app.home.read(WORLD) == START


async def test_the_owners_hand_edit_is_pinned_and_does_not_move_the_week(make_app):
    app = make_app()
    put(app.home, WORLD, START)
    result = app.home.save_memory(WORLD, START + "- Works at a bakery\n", content_version(START), today=DAY)
    assert result.ok and "- Works at a bakery {core owner 2026-01-01}" in result.content
    assert await world_change.last_change(app) is None  # only Dom's own changes count toward the week
    assert any(item["file"] == WORLD and "Works at a bakery" in item["text"] for item in app.home.scoped(allow_private=True).core_memories())


async def test_the_reflection_offers_the_change_once_a_week_in_the_same_request_and_applies_it(make_app):
    world = {"reason": "he took up climbing", "ops": [add("Thursday evening: usually gym {chat -5}")]}
    app = make_app(contents=(json.dumps({**PUBLIC, "world_edit": world}), json.dumps(PUBLIC), json.dumps(PUBLIC)))
    put(app.home, WORLD, "- Lives in a small flat {chat -1005}\n- Works from home\n- Lives by the sea {core owner 2026-01-01}\n")
    await title(app, GROUP, "Book Club")
    await record(app, "hello")
    await reflection.run(app)
    request = app.script.requests[0]["messages"]
    assert "world_edit" in request[0]["content"] and "<world limit=" in request[1]["content"]
    assert "Works from home" in request[1]["content"] and "small flat" not in request[1]["content"]  # a chat's own line stays out
    assert "Lives by the sea {core owner 2026-01-01}" in request[1]["content"]  # a pin is shown as one
    assert len(app.script.requests) == 1  # no call of its own
    assert app.home.read(WORLD).splitlines()[-1] == "- Thursday evening: usually gym"
    assert (await world_change.last_change(app))[1] == "he took up climbing"

    app.dispatch.now.advance(days=1)
    await record(app, "again")
    await reflection.run(app)
    second = app.script.requests[1]["messages"]
    assert "world_edit" not in second[0]["content"] and "<world" not in second[1]["content"]  # the week is not up

    app.dispatch.now.advance(days=6)
    await record(app, "a week later")
    await reflection.run(app)
    assert "world_edit" in app.script.requests[2]["messages"][0]["content"]


async def test_a_bad_world_edit_never_costs_the_journal_entry(make_app):
    app = make_app(contents=(json.dumps({**PUBLIC, "world_edit": {"ops": "garbage"}}),))
    await title(app, GROUP, "Book Club")
    await record(app, "hello")
    await reflection.run(app)
    assert "the book chat got lively." in app.home.read("memory/JOURNAL.md")
    assert await world_change.last_change(app) is None


async def test_the_week_is_kept_with_the_sheet_so_a_crash_or_a_failed_kv_write_cannot_reopen_it(make_app):
    app = make_app()
    put(app.home, WORLD, START)

    async def broken(*_):
        raise OSError("kv is down")

    app.store.kv_set = broken
    assert await world_change.apply(app, edit(add("Plays chess online")), DAY)  # the marker never rides on kv
    assert (await world_change.last_change(app))[0] == DAY
    assert not await world_change.apply(app, edit(add("Again")), DAY + timedelta(days=1))


async def test_a_crash_between_the_steps_leaves_the_week_spent_not_open(make_app, monkeypatch):
    from dom.home import Home

    app = make_app()
    put(app.home, WORLD, START)
    real = Home.memory_write
    calls = []

    def crash_on_second(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("the process died")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Home, "memory_write", crash_on_second)
    try:
        await world_change.apply(app, edit(add("First line"), add("Second line")), DAY)
    except RuntimeError:
        pass
    assert "First line" in app.home.read(WORLD)  # the sheet did change
    assert not await world_change.due(app, DAY + timedelta(days=1))  # so the week counts, whatever else was lost
    assert not await world_change.apply(app, edit(add("Third line")), DAY + timedelta(days=1))
    assert "Third line" not in app.home.read(WORLD)


async def test_two_calls_at_once_change_the_sheet_once(make_app):
    import asyncio

    app = make_app()
    put(app.home, WORLD, START)
    results = await asyncio.gather(
        world_change.apply(app, edit(add("From the first")), DAY), world_change.apply(app, edit(add("From the second")), DAY),
    )
    assert sorted(results) == [False, True]
    text = app.home.read(WORLD)
    assert ("From the first" in text) != ("From the second" in text)
