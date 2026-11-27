"""E117: the dashboard's one "talk to Dom" conversation proposes edits to SOUL.md or WORLD.md, through the soul_edit
proposal and its approval, and never over the owner's pinned lines or a line one chat owns."""

import json

import pytest

from dom import notes
from dom.home import limit_for
from dom.proposals import Refused, apply_world_edit, soul_edit_lines, world_edit_problem
from test_web_auth import ORIGIN

JSON = {"Origin": ORIGIN, "Content-Type": "application/json"}
WORLD = "memory/WORLD.md"
SHEET = (
    "- Works at a small design studio\n"
    "- Thursday evening: usually gym\n"
    "- Lives near the old market {core owner 2026-11-01}\n"
    "- Plays chess with an old friend {chat -1005}\n"
)


def said(*texts):
    return {"messages": [{"role": "owner" if at % 2 == 0 else "dom", "text": text} for at, text in enumerate(texts)]}


def answer(reply, edit=None):
    return json.dumps({"reply": reply, "edit": edit})


def make(make_app, *contents):
    app = make_app(contents=list(contents))
    app.home._write_regular("SOUL.md", "An old line")
    app.home._write_regular(WORLD, SHEET)
    return app


async def test_the_conversation_reads_both_files_and_files_a_world_edit(make_app, browser):
    app = make(make_app, answer("Swimming now, actually.", {
        "file": "WORLD.md", "old": "Thursday evening: usually gym", "new": "Thursday evening: usually swimming",
    }))
    async with browser(app) as client:
        response = await client.post("/api/memory/soul", headers=JSON, json=said("You swim on Thursdays now"))
        assert response.status_code == 200
        data = response.json()
    system = app.script.requests[0]["messages"][0]["content"]
    assert "An old line" in system and "Thursday evening: usually gym" in system
    assert "core owner 2026-11-01" in system  # the model sees which lines are pinned
    assert "chess" not in system  # a line one chat owns stays out
    assert data["proposal"]["file"] == "WORLD.md"
    assert data["proposal"]["diff"] == ["- Thursday evening: usually gym", "+ Thursday evening: usually swimming"]
    (edit,) = data["edits"]
    assert edit["label"] == "World edit, asked for in the dashboard" and edit["diff"] == data["proposal"]["diff"]
    assert app.home.read(WORLD) == SHEET  # nothing written before the owner approves
    events = [e.text for e in await app.store.get_recent_chat_window(app.owner_id) if e.role == "event"]
    assert events == [f"Dom proposed a world edit (#{data['proposal']['id']}) from the dashboard"]

    async with browser(app) as client:
        decided = await client.post("/api/memory/proposal", headers=JSON, json={"id": data["proposal"]["id"], "decision": "approve"})
        assert decided.status_code == 200 and decided.json()["message"] == "Approved"
    sheet = app.home.read(WORLD)
    assert "- Thursday evening: usually swimming" in sheet and "usually gym" not in sheet
    assert "- Lives near the old market {core owner 2026-11-01}" in sheet and "{chat -1005}" in sheet
    assert app.home.read("SOUL.md") == "An old line"


async def test_a_soul_edit_still_needs_no_file_and_lands_in_soul(make_app, browser):
    app = make(make_app, answer("Sure.", {"old": "An old line", "new": "A new line"}))
    async with browser(app) as client:
        data = (await client.post("/api/memory/soul", headers=JSON, json=said("Rewrite it"))).json()
        assert data["proposal"]["file"] == "SOUL.md" and data["edits"][0]["label"] == "Soul edit, asked for in the dashboard"
        await client.post("/api/memory/proposal", headers=JSON, json={"id": data["proposal"]["id"], "decision": "approve"})
    assert app.home.read("SOUL.md") == "A new line" and app.home.read(WORLD) == SHEET


@pytest.mark.parametrize("pinned", [
    {"file": "WORLD.md", "old": "Lives near the old market", "new": "Lives by the sea"},
    {"file": "WORLD.md", "old": "- Lives near the old market {core owner 2026-11-01}\n", "new": ""},
    {"file": "WORLD.md", "old": "Plays chess with an old friend", "new": "Plays go"},
])
async def test_a_pinned_or_chat_owned_line_is_never_proposed(make_app, browser, pinned):
    """Dom gets one retry with the reason; when that is refused too, the reply stands and nothing is filed."""
    app = make(make_app, answer("Moving!", pinned), answer("Moving!", pinned))
    async with browser(app) as client:
        response = await client.post("/api/memory/soul", headers=JSON, json=said("You moved"))
        assert response.status_code == 200 and response.json()["proposal"] is None
        assert "exact place in WORLD.md" in response.json()["reply"]
    retry = app.script.requests[1]["messages"][-1]["content"]
    assert "WORLD.md" in retry and ("pinned" in retry or "one chat" in retry)
    assert await app.proposals.pending() == [] and app.home.read(WORLD) == SHEET


async def test_a_world_line_is_added_and_removed(make_app, browser):
    app = make(make_app,
               answer("Noted.", {"file": "WORLD.md", "old": "", "new": "Sunday morning: kopi at the market"}),
               answer("Gone.", {"file": "WORLD.md", "old": "- Works at a small design studio\n", "new": ""}))
    async with browser(app) as client:
        added = (await client.post("/api/memory/soul", headers=JSON, json=said("Kopi on Sundays"))).json()["proposal"]
        removed = (await client.post("/api/memory/soul", headers=JSON, json=said("You quit"))).json()["proposal"]
        assert added["diff"] == ["+ Sunday morning: kopi at the market"]
        assert removed["diff"] == ["- - Works at a small design studio\n"]
        for proposal in (added, removed):
            await client.post("/api/memory/proposal", headers=JSON, json={"id": proposal["id"], "decision": "approve"})
    sheet = app.home.read(WORLD)
    assert "- Sunday morning: kopi at the market" in sheet and "design studio" not in sheet
    assert "{core owner 2026-11-01}" in sheet


@pytest.mark.parametrize("edit", [
    {"file": "NOTES.md", "old": "x", "new": "y"},  # another file
    {"file": "SOUL.md", "old": "", "new": "y"},  # SOUL.md has no add
    {"file": "WORLD.md", "old": "", "new": ""},
    {"file": "WORLD.md", "old": "x", "new": "y", "extra": 1},
])
async def test_a_malformed_edit_is_no_edit(make_app, browser, edit):
    app = make(make_app, answer("Hm.", edit))
    async with browser(app) as client:
        response = await client.post("/api/memory/soul", headers=JSON, json=said("Hi"))
        assert response.status_code == 200 and response.json() == {**response.json(), "reply": "Hm.", "proposal": None}
    assert len(app.script.requests) == 1 and await app.proposals.pending() == []


async def test_approval_refuses_a_line_pinned_since_the_proposal(make_app):
    """The owner pins the line by hand after Dom proposed changing it: the approval is refused and stays pending."""
    app = make(make_app)
    proposal = await app.proposals.create(
        "soul_edit", "Gym", {"file": WORLD, "old": "Thursday evening: usually gym", "new": "Thursday evening: yoga"},
        "dashboard", notify=False,
    )
    app.home._write_regular(WORLD, SHEET.replace("usually gym", "usually gym {core owner 2026-11-20}"))
    message = await app.proposals.decide(proposal, True)
    assert "pinned" in message
    assert [row["id"] for row in await app.proposals.pending()] == [proposal]
    assert "usually gym {core owner 2026-11-20}" in app.home.read(WORLD)


async def test_world_edit_checks_and_writes_directly(make_app):
    app = make(make_app)
    home = app.home
    assert world_edit_problem(home, "Not in the sheet", "x") == "WORLD.md must contain the old text exactly once"
    assert world_edit_problem(home, "", "two\nlines") == "add one line at a time to WORLD.md"
    full = SHEET + "- " + "x" * (limit_for(WORLD) - notes.visible_len(SHEET) - 4) + "\n"  # tags are free
    home._write_regular(WORLD, full)
    assert "longer than" in world_edit_problem(home, "", "One more habit")
    home._write_regular(WORLD, SHEET)
    assert world_edit_problem(home, "Works at a small design studio", "Works at a bakery") is None
    with pytest.raises(ValueError, match="pinned"):
        apply_world_edit(home, "Lives near the old market", "Lives by the sea")
    with pytest.raises(ValueError):
        await app.proposals.create("soul_edit", "x", {"file": "private/USER.md", "old": "a", "new": "b"}, "dashboard", notify=False)
    assert soul_edit_lines({"file": WORLD, "old": "", "new": "A"}) == ["+ A"]
    assert soul_edit_lines({"old": "A", "new": ""}) == ["- A", "+ "]  # SOUL.md's card is as it was
    assert issubclass(Refused, ValueError)


async def test_an_approved_world_edit_is_the_owners_call_not_dom_weekly_change(make_app):
    """The reflection changed the sheet yesterday, so Dom's own week is spent; the owner still approves an edit from
    the conversation, the week's marker is left as it was, and the new line is Dom's, not pinned."""
    from datetime import date, timedelta

    from dom import world_change

    app = make(make_app)
    marker = json.dumps({"day": (date.today() - timedelta(days=1)).isoformat(), "reason": "Started at the studio"})
    app.home.write_file(world_change.MARKER, marker)
    assert not await world_change.due(app, date.today())
    proposal = await app.proposals.create(
        "soul_edit", "Kopi", {"file": WORLD, "old": "", "new": "Sunday morning: kopi at the market"}, "dashboard", notify=False,
    )
    assert await app.proposals.decide(proposal, True) == "Approved"
    added = [line for line in app.home.read(WORLD).splitlines() if "kopi at the market" in line]
    assert added == ["- Sunday morning: kopi at the market"]  # Dom's line: no {core owner ...} pin
    assert app.home.read(world_change.MARKER) == marker
