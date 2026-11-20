import asyncio
import json

import pytest

from dom import reflection
from dom.telegram import handlers
from test_owner_events import events, press
from test_traits import put
from tghelpers import FRIEND, GROUP, OWNER, tg


async def soul_edit(app):
    put(app.home, "SOUL.md", "A curious friend.")
    return await app.proposals.create(
        "soul_edit", "A gentler voice", {"old": "curious", "new": "thoughtful"}, "example",
    )


async def test_create_dms_owner_silently_and_lists_pending_with_buttons(make_app):
    app = make_app()
    proposal_id = await soul_edit(app)
    (sent,) = app.bot.of("send")
    assert sent[1] == OWNER and "A gentler voice" in sent[2] and sent[3]["disable_notification"]
    assert [(b.text, b.callback_data) for b in sent[3]["reply_markup"].inline_keyboard[0]] == [
        ("Approve", f"prop:yes:{proposal_id}"), ("Reject", f"prop:no:{proposal_id}"),
    ]
    assert "A gentler voice" in (await events(app))[0]["text"]
    assert await events(app, GROUP) == []

    app.bot.calls.clear()
    await handlers.handle_message(app, tg(OWNER, OWNER, "/proposals"))
    listed = app.bot.of("send", OWNER)[0][3]["reply_markup"]
    assert listed.inline_keyboard[0] == sent[3]["reply_markup"].inline_keyboard[0]


async def test_approve_applies_once_even_with_two_taps(make_app):
    app = make_app()
    proposal_id = await app.proposals.create(
        "memory_fact", "Prefers tea", {"file": "USER.md", "text": "Prefers tea"}, "example",
    )
    await asyncio.gather(*(press(app, f"prop:yes:{proposal_id}") for _ in range(2)))
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Prefers tea"
    assert {call[2] for call in app.bot.of("answer")} == {"Approved", "already handled"}
    assert any("Approved" in event["text"] for event in await events(app))


async def test_reject_leaves_files_unchanged_and_non_owner_tap_is_ignored(make_app):
    app = make_app()
    proposal_id = await soul_edit(app)
    await press(app, f"prop:yes:{proposal_id}", FRIEND)
    assert app.bot.of("answer") == []
    assert app.home.read("SOUL.md") == "A curious friend."
    await press(app, f"prop:no:{proposal_id}")
    assert app.home.read("SOUL.md") == "A curious friend."
    assert app.bot.of("answer")[-1][2] == "Rejected"
    assert await app.proposals.pending() == []


@pytest.mark.parametrize("soul", ["A new friend.", "curious, curious"])
async def test_soul_edit_requires_exact_unique_old_text(make_app, soul):
    app = make_app()
    proposal_id = await soul_edit(app)
    put(app.home, "SOUL.md", soul)
    await press(app, f"prop:yes:{proposal_id}")
    assert app.home.read("SOUL.md") == soul
    assert app.bot.of("answer")[-1][2] == "SOUL.md must contain the old text exactly once"
    assert [row["id"] for row in await app.proposals.pending()] == [proposal_id]

    put(app.home, "SOUL.md", "A curious friend.")
    await press(app, f"prop:yes:{proposal_id}")
    assert app.home.read("SOUL.md") == "A thoughtful friend."


async def test_memory_fact_over_limit_stays_pending(make_app):
    app = make_app()
    put(app.home, "private/USER.md", "x" * 1375)
    proposal_id = await app.proposals.create(
        "memory_fact", "A fact", {"file": "USER.md", "text": "A fact"}, "example",
    )
    await press(app, f"prop:yes:{proposal_id}")
    assert "over limit" in app.bot.of("answer")[-1][2]
    assert [row["id"] for row in await app.proposals.pending()] == [proposal_id]
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "x" * 1375


async def test_daily_expiry_only_expires_proposals_older_than_14_days(make_app):
    app = make_app()
    old = await soul_edit(app)
    app.dispatch.now.advance(hours=1)
    current = await soul_edit(app)
    app.dispatch.now.advance(days=14)
    await app.dispatch.jobs.tick()
    assert [row["id"] for row in await app.proposals.pending()] == [current]
    await press(app, f"prop:yes:{old}")
    assert app.bot.of("answer")[-1][2] == "already handled"


async def test_invalid_plugin_proposal_cannot_be_created(make_app):
    app = make_app()
    with pytest.raises(ValueError):
        await app.proposals.create("plugin", "Example", {}, "example")
    assert not app.bot.calls


async def test_public_reflection_proposes_at_most_once_a_week_without_editing_soul(make_app):
    reply = json.dumps({
        "entry": "A quiet day.", "mood": "calm", "nudges": {},
        "soul_edit": {"old": "curious", "new": "thoughtful", "why": "A gentler voice"},
    })
    app = make_app(contents=(reply, reply, reply))
    put(app.home, "SOUL.md", "A curious friend.")
    await reflection.run(app)
    app.dispatch.now.advance(days=6)
    await reflection.run(app)
    assert len(await app.proposals.pending()) == 1
    app.dispatch.now.advance(days=1)
    await reflection.run(app)

    assert len(await app.proposals.pending()) == len(app.bot.of("send", OWNER)) == 2
    assert app.home.read("SOUL.md") == "A curious friend."
    assert "soul_edit" in app.script.requests[0]["messages"][0]["content"]
    assert "soul_edit" not in app.script.requests[1]["messages"][0]["content"]


async def test_bulk_approval_applies_memory_facts_but_never_a_soul_edit(make_app):
    app = make_app()
    soul_id = await soul_edit(app)
    fact_id = await app.proposals.create(
        "memory_fact", "Prefers tea", {"file": "USER.md", "text": "Prefers tea"}, "example",
    )
    await handlers.handle_message(app, tg(OWNER, OWNER, "/proposals"))
    bulk = next(
        b.callback_data for row in app.bot.of("send", OWNER)[-1][3]["reply_markup"].inline_keyboard
        for b in row if b.text == "Approve all on this page"
    )
    await press(app, bulk)
    assert app.bot.of("answer")[-1][2] == "Approved 1. 1 soul edit needs its own Approve."
    assert app.home.read("SOUL.md") == "A curious friend."
    assert [row["id"] for row in await app.proposals.pending()] == [soul_id]
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Prefers tea"
    assert fact_id != soul_id

    await press(app, bulk)  # only the soul edit is left: still never applied in bulk
    assert app.home.read("SOUL.md") == "A curious friend."
    assert [row["id"] for row in await app.proposals.pending()] == [soul_id]
    await press(app, f"prop:yes:{soul_id}")
    assert app.home.read("SOUL.md") == "A thoughtful friend."


@pytest.mark.parametrize("soul,old,new,problem", [
    ("A curious friend.", "kind", "thoughtful", "exactly once"),
    ("curious, curious", "curious", "thoughtful", "exactly once"),
    ("A" + "x" * 2500, "x" * 2001, "y", "2000"),
    ("A curious friend.", "curious", "y" * 2001, "2000"),
    ("A" + "x" * 5990 + " curious", "curious", "y" * 1000, "6000"),
])
async def test_soul_edits_are_bounded_at_creation(make_app, soul, old, new, problem):
    app = make_app()
    put(app.home, "SOUL.md", soul)
    with pytest.raises(ValueError, match=problem):
        await app.proposals.create("soul_edit", "A change", {"old": old, "new": new}, "example")
    assert await app.proposals.pending() == []
    assert not app.bot.calls


async def test_reflection_soul_edits_are_bounded_like_any_other(make_app):
    reply = json.dumps({
        "entry": "A quiet day.", "mood": "calm", "nudges": {},
        "soul_edit": {"old": "not in the soul", "new": "thoughtful", "why": "A gentler voice"},
    })
    app = make_app(contents=(reply,))
    put(app.home, "SOUL.md", "A curious friend.")
    await reflection.run(app)
    assert await app.proposals.pending() == []
    assert not app.bot.of("send", OWNER)


async def test_soul_edit_that_would_overflow_soul_when_applied_stays_pending(make_app):
    app = make_app()
    proposal_id = await soul_edit(app)
    put(app.home, "SOUL.md", "A curious friend." + "x" * 5990)
    await press(app, f"prop:yes:{proposal_id}")
    assert "6000" in app.bot.of("answer")[-1][2]
    assert app.home.read("SOUL.md").endswith("x" * 5990) and "curious" in app.home.read("SOUL.md")
    assert [row["id"] for row in await app.proposals.pending()] == [proposal_id]


async def test_reflection_proposal_event_never_carries_the_model_written_reason(make_app):
    reply = json.dumps({
        "entry": "A quiet day.", "mood": "calm", "nudges": {},
        "soul_edit": {"old": "curious", "new": "thoughtful", "why": "A gentler voice"},
    })
    app = make_app(contents=(reply,))
    put(app.home, "SOUL.md", "A curious friend.")
    await reflection.run(app)
    (proposal,) = await app.proposals.pending()
    assert [event["text"] for event in await events(app)] == [f"Dom proposed a soul edit (#{proposal['id']})"]
    assert "A gentler voice" in app.bot.of("send", OWNER)[0][2]  # the owner still reads the reason in the DM


async def test_bulk_approval_applies_skills_and_lan_changes_through_their_own_checks(make_app):
    app = make_app()
    old, new = "192.0.2.10", "192.0.2.11"
    path = app.home.root / "config.toml"
    path.write_text(f'[models.lab]\nbase_url = "http://{old}:8080/v1"\nmodel = "m"\n')
    lan = await app.proposals.create("lan_address", "moved", {"old": old, "new": new, "places": ["models.lab.base_url"]}, "devices")
    stale = await app.proposals.create("lan_address", "gone", {"old": "192.0.2.50", "new": "192.0.2.51", "places": []}, "devices")
    skill = await app.proposals.create("skill", "Tea", {"name": "tea", "description": "Brew tea", "body": "Steep."}, "example")
    await app.proposals.create("memory_fact", "Prefers tea", {"file": "USER.md", "text": "Prefers tea"}, "example")
    await handlers.handle_message(app, tg(OWNER, OWNER, "/proposals"))
    bulk = next(
        b.callback_data for row in app.bot.of("send", OWNER)[-1][3]["reply_markup"].inline_keyboard
        for b in row if b.text == "Approve all on this page"
    )
    await press(app, bulk)
    assert app.bot.of("answer")[-1][2].startswith("Approved 3. 1 still pending.")
    assert new in path.read_text() and old not in path.read_text()
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Prefers tea"
    assert [row["id"] for row in await app.proposals.pending()] == [stale]
    assert lan != skill


async def test_skill_approval_refuses_private_notes_added_after_the_proposal(make_app):
    from dom.skills import skill_view

    app = make_app()
    proposal = await app.proposals.create(
        "skill", "Tea", {"name": "tea", "description": "Brew tea", "body": "Steep the leaves for four minutes."}, "example",
    )
    put(app.home, "private/NOTES.md", "- Steep the leaves for four minutes.")
    assert "private notes" in await app.proposals.decide(proposal, True)
    assert skill_view(app.home, "tea") is None
    assert [row["id"] for row in await app.proposals.pending()] == [proposal]
