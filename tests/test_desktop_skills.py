import httpx
import pytest

from dom.skills import Skill, skills_list, skill_view
from test_desktop_worker import call, wait_task, worker_for
from test_owner_events import press
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN
from test_web_desktop import dashboard
from tghelpers import FRIEND, OWNER


@pytest.mark.parametrize("name", ["../private", "a/b", r"a\b", ".", "-start", "UPPER", "a" * 65])
def test_invalid_skill_names(name):
    with pytest.raises(ValueError, match="name"):
        Skill(name, "Description", "Procedure")


def test_skill_caps():
    assert len(Skill("example", "Description", "a" * 4000).body) == 4000
    for description, body in [("a" * 201, "Procedure"), ("line\nbreak", "Procedure"), ("Valid", "a" * 4001)]:
        with pytest.raises(ValueError):
            Skill("example", description, body)


HIDDEN = {
    "zero-width": "\u200b", "bidi override": "\u202e", "bidi isolate": "\u2066", "tag": "\U000e0041",
    "private use": "\ue000", "unassigned": "\u0378", "lone surrogate": "\ud800",
    "combining grapheme joiner": "\u034f", "variation selector": "\ufe0f", "soft hyphen": "\u00ad",
    "hangul filler": "\u3164", "variation selector supplement": "\U000e0100", "invisible operator": "\u2063",
    "interlinear annotation": "\ufff9", "musical begin beam": "\U0001d173",
    "escape": "\x1b", "carriage return": "\r", "null": "\x00", "delete": "\x7f", "c1": "\x85",
}


@pytest.mark.parametrize("hidden", HIDDEN.values(), ids=HIDDEN.keys())
def test_hidden_characters_are_rejected_everywhere(hidden):
    with pytest.raises(ValueError):
        Skill("example", "Description", f"Open the page{hidden} and read it.")
    with pytest.raises(ValueError):
        Skill("example", f"Read{hidden} text", "Procedure")
    with pytest.raises(ValueError):
        Skill(f"exam{hidden}ple", "Description", "Procedure")


@pytest.mark.parametrize("neighbour", ["\u00ac", "\u00ae", "\u034e", "\u0350", "\u2070", "\u3165", "\ufe10", "\uffa1", "\U0001d172"])
def test_characters_next_to_the_ignorable_ranges_are_allowed(neighbour):
    assert Skill("example", "Example", f"a{neighbour}b").body == f"a{neighbour}b"


def test_visible_text_newlines_and_tabs_are_allowed():
    body = "Step 1:\n\t- open the page \u00e9 \u2014 \u4f60\u597d \U0001f600\n"
    assert Skill("example", "Description", body).body == body


async def propose(app, body="Use the text view.", description='Read "text" safely'):
    return await app.proposals.create(
        "skill", "Skill: browse", {"name": "browse", "description": description, "body": body}, "desktop",
    )


async def test_approval_from_owner_dm_writes_skill(make_app):
    app = make_app()
    proposal_id = await propose(app)
    target = app.home.root / "skills/browse/SKILL.md"
    assert not target.exists()
    sent = app.bot.of("send", OWNER)[0][2]
    assert "Use the text view." in sent
    await press(app, f"prop:yes:{proposal_id}", FRIEND)
    assert not target.exists()
    await press(app, f"prop:yes:{proposal_id}")
    assert target.read_text().startswith("---\nname: browse\n")
    assert skills_list(app.home) == [{"name": "browse", "description": 'Read "text" safely'}]
    assert skill_view(app.home, "browse") == "Use the text view."


async def test_dashboard_skill_edit_shows_full_text_and_diff_then_applies(make_app, monkeypatch):
    app = make_app()
    await app.proposals.decide(await propose(app, "Use the old tool."), True)
    proposal_id = await propose(app, "Use the new tool.\n<example>")
    dm = "\n".join(item[2] for item in app.bot.of("send", OWNER))
    assert "-Use the old tool." in dm and "+Use the new tool." in dm
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dashboard(app, monkeypatch)), base_url=ORIGIN,
    ) as client:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        page = await client.get("/proposals?kind=skill", headers=auth)
        assert "Skill · desktop" in page.text
        assert "<del>- Use the old tool." in page.text
        assert "<ins>+ Use the new tool." in page.text
        assert "&lt;example&gt;" in page.text
        assert "Approved" in (await client.post(
            f"/proposals/{proposal_id}/approve", headers=auth | POST_HEADERS,
        )).text
    assert skill_view(app.home, "browse") == "Use the new tool.\n<example>"


async def test_skill_draft_cannot_include_a_configured_credential(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-telegram-token")
    with pytest.raises(ValueError, match="credential"):
        await propose(app, "Use fake-telegram-token.")
    assert await app.proposals.pending() == []


async def test_a_stale_edit_does_not_overwrite_an_approved_skill(make_app):
    app = make_app()
    old = await propose(app, "Old")
    fresh = await propose(app, "New")
    assert await app.proposals.decide(fresh, True) == "Approved"
    assert "changed" in await app.proposals.decide(old, True)
    assert skill_view(app.home, "browse") == "New"


async def test_worker_sees_metadata_until_it_views_skill_and_proposes_once(make_app):
    app = make_app()
    await app.proposals.decide(await propose(app, "APPROVED-BODY-MARKER", "Approved description"), True)
    worker = await worker_for(
        app, call("skill_view", name="browse"),
        call("propose_skill", name="first", description="First", body="A public procedure."),
        call("propose_skill", name="second", description="Second", body="Another procedure."),
        call("finish", result="Done"), {"content": "invalid"},
    )
    await worker.enqueue(OWNER, "Do the thing")
    await wait_task(worker)
    first, after_view, _, after_second_proposal, _ = app.client.requests
    assert "Approved description" in first[0][0]["content"]
    assert "APPROVED-BODY-MARKER" not in str(first)
    assert "APPROVED-BODY-MARKER" in after_view[0][-1]["content"]
    assert "once" in after_second_proposal[0][-1]["content"]
    proposal, = await app.proposals.pending()
    assert proposal["kind"] == "skill" and "first" in proposal["body_json"]
    assert skill_view(app.home, "first") is None


async def test_a_failed_task_discards_its_draft_skill(make_app):
    app = make_app()
    worker = await worker_for(
        app, call("propose_skill", name="example", description="Example", body="Public procedure."),
        {"content": "invalid"}, max_steps=1,
    )
    await worker.enqueue(OWNER, "Do the thing")
    await wait_task(worker)
    assert await app.proposals.pending() == []


def test_skill_paths_follow_home_rules(tmp_path):
    from dom.home import Home

    home = Home(tmp_path)
    (tmp_path / "private").mkdir()
    (tmp_path / "private/SKILL.md").write_text("---\nname: example\ndescription: Private\n---\nSecret")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills/example").symlink_to(tmp_path / "private", target_is_directory=True)
    assert skills_list(home) == []
    with pytest.raises((ValueError, PermissionError)):
        skill_view(home, "example")
