import json

import pytest

from dom.prompt import build_messages
from dom.skills import skill_view
from dom.tools import SKILL_MANAGE_TOOL, SKILL_PENDING_LIMIT, SKILL_VIEW_TOOL, SKILLS_LIST_TOOL, TurnCtx, enabled_tools
from test_owner_events import press
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, tg

SCOPES = ("owner_dm", "friend_dm", "group")
BODY = "Open the page, read the table, report the totals."


def ctx(app, scope="owner_dm", chat=OWNER, sender=OWNER):
    return TurnCtx(scope, chat, sender, app.home, app.store, OWNER, app=app)


async def approved(app, name="report", description="Summarise a web table", body=BODY):
    proposal_id = await app.proposals.create(
        "skill", f"Skill: {name}", {"name": name, "description": description, "body": body}, "chat",
    )
    assert await app.proposals.decide(proposal_id, True) == "Approved"


@pytest.mark.parametrize("scope", SCOPES)
async def test_skill_tools_are_offered_in_every_scope(make_app, scope):
    app = make_app()
    names = [tool.name for tool in await enabled_tools(app, scope)]
    assert {"skills_list", "skill_view", "skill_manage"} <= set(names)


async def test_capabilities_and_self_inspect_show_skills(make_app):
    from dom import capabilities
    from dom.tools import SELF_INSPECT_TOOL

    app = make_app()
    for scope in SCOPES:
        assert "skills_list" in capabilities.describe(app, scope, await enabled_tools(app, scope))
    found = json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))
    for scope in SCOPES:
        assert {"skills_list", "skill_view", "skill_manage"} <= set(found["tools"][scope])


@pytest.mark.parametrize("scope,chat,sender", [("owner_dm", OWNER, OWNER), ("friend_dm", FRIEND, FRIEND), ("group", GROUP, FRIEND)])
async def test_list_and_view_return_public_skills(make_app, scope, chat, sender):
    app = make_app()
    await approved(app)
    context = ctx(app, scope, chat, sender)
    assert json.loads(await SKILLS_LIST_TOOL.handler({}, context)) == [
        {"name": "report", "description": "Summarise a web table"},
    ]
    assert await SKILL_VIEW_TOOL.handler({"name": "report"}, context) == BODY
    assert await SKILL_VIEW_TOOL.handler({"name": "missing"}, context) == "Skill not found."
    assert await SKILL_VIEW_TOOL.handler({"name": "../private"}, context) == "Skill unavailable."
    assert await SKILL_VIEW_TOOL.handler({}, context) == "Skill unavailable."


async def test_skill_tools_never_read_private(make_app):
    app = make_app()
    root = app.home.root
    (root / "private").mkdir(exist_ok=True)
    (root / "private/SKILL.md").write_text("---\nname: sneaky\ndescription: Private\n---\nSECRET-PRIVATE-BODY")
    (root / "skills").mkdir()
    (root / "skills/sneaky").symlink_to(root / "private", target_is_directory=True)
    context = ctx(app)
    assert json.loads(await SKILLS_LIST_TOOL.handler({}, context)) == []
    assert "SECRET-PRIVATE-BODY" not in await SKILL_VIEW_TOOL.handler({"name": "sneaky"}, context)


@pytest.mark.parametrize("scope,chat,sender", [("owner_dm", OWNER, OWNER), ("group", GROUP, FRIEND)])
async def test_manage_creates_a_proposal_the_owner_must_approve(make_app, scope, chat, sender):
    app = make_app()
    args = {"name": "report", "description": "Summarise a web table", "body": BODY}
    result = json.loads(await SKILL_MANAGE_TOOL.handler(args, ctx(app, scope, chat, sender)))
    proposal, = await app.proposals.pending()
    assert result["id"] == proposal["id"] and proposal["kind"] == "skill" and proposal["source"] == "chat"
    assert skill_view(app.home, "report") is None
    assert BODY in app.bot.of("send", OWNER)[-1][2]
    await press(app, f"prop:yes:{proposal['id']}")
    assert skill_view(app.home, "report") == BODY
    # the card now says so, and its Approve and Reject buttons are gone
    (button,), = app.bot.of("markup")[-1][3].inline_keyboard
    assert (button.text, button.callback_data) == ("✅ Approved", "noop")


async def test_a_second_proposal_for_the_same_skill_waits_for_the_owner_to_decide_on_the_first(make_app):
    # Turn 54 on the Pi proposed sunday-routine-plan twice and the owner got two cards.
    app = make_app()
    owner = ctx(app, "owner_dm", OWNER, OWNER)
    args = {"name": "report", "description": "Summarise a web table", "body": BODY}
    first = json.loads(await SKILL_MANAGE_TOOL.handler(args, owner))
    again = await SKILL_MANAGE_TOOL.handler({**args, "name": " Report ", "body": BODY + " Revised."}, owner)
    assert again.startswith(f"error: proposal #{first['id']} for this skill already waits")
    assert [row["id"] for row in await app.proposals.pending()] == [first["id"]]
    await press(app, f"prop:no:{first['id']}")
    assert "id" in json.loads(await SKILL_MANAGE_TOOL.handler(args, owner))  # after the decision a revision is fine


async def test_manage_caps_skill_proposals_waiting_for_the_owner(make_app):
    app = make_app()
    group = ctx(app, "group", GROUP, FRIEND)
    for n in range(SKILL_PENDING_LIMIT):
        args = {"name": f"step-{n}", "description": "A procedure", "body": BODY}
        assert "id" in json.loads(await SKILL_MANAGE_TOOL.handler(args, group))
    extra = {"name": "one-more", "description": "A procedure", "body": BODY}
    assert (await SKILL_MANAGE_TOOL.handler(extra, group)).startswith("error:")
    assert len(await app.proposals.pending()) == SKILL_PENDING_LIMIT


async def test_manage_update_shows_a_diff_against_the_approved_text(make_app):
    app = make_app()
    await approved(app)
    await SKILL_MANAGE_TOOL.handler(
        {"name": "report", "description": "Summarise a web table", "body": "Read the table twice."}, ctx(app),
    )
    sent = app.bot.of("send", OWNER)[-1][2]
    assert f"-{BODY}" in sent and "+Read the table twice." in sent
    assert skill_view(app.home, "report") == BODY


async def test_manage_rejects_bad_input_without_a_proposal(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-telegram-token")
    (app.home.root / "private").mkdir(exist_ok=True)
    (app.home.root / "private/USER.md").write_text("The owner keeps a diary about Maple Street.\n")
    for args in (
        {"name": "Bad Name", "description": "d", "body": "b"},
        {"name": "ok", "description": "d", "body": ""},
        {"name": "ok", "description": "d", "body": "Use fake-telegram-token."},
        {"name": "ok", "description": "d", "body": "the owner keeps a diary about maple street."},
        {"name": "ok"},
    ):
        assert (await SKILL_MANAGE_TOOL.handler(args, ctx(app))).startswith("error:")
    assert await app.proposals.pending() == []


def prompt(app, scope="group", chat=GROUP):
    return build_messages(app.home, scope, chat, [], "<chat_window/>", "note")


async def test_prompt_lists_only_names_and_descriptions_in_the_stable_prefix(make_app):
    app = make_app()
    before = prompt(app)
    assert "<skills>" not in before[0]["content"]
    await approved(app, description="Summarise <a> web table")
    after = prompt(app)
    assert "- report: Summarise &lt;a&gt; web table" in after[0]["content"]
    assert "skill_view" in after[0]["content"]
    assert BODY not in "".join(m["content"] for m in after)
    # one prefix for every chat and scope, so the provider's prompt cache keeps working
    assert prompt(app, "owner_dm", OWNER)[0] == after[0] == prompt(app, "friend_dm", FRIEND)[0]
    assert "<skills>" not in after[1]["content"]


async def test_prompt_caps_the_list_and_points_at_skills_list(make_app):
    from dom.skills import PROMPT_LIMIT

    app = make_app()
    for n in range(PROMPT_LIMIT + 2):
        await approved(app, name=f"skill-{n:02d}", description=f"Procedure {n}")
    system = prompt(app)[0]["content"]
    assert f"skill-{PROMPT_LIMIT - 1:02d}" in system and f"skill-{PROMPT_LIMIT:02d}" not in system
    assert "2 more" in system and len(json.loads(await SKILLS_LIST_TOOL.handler({}, ctx(app)))) == PROMPT_LIMIT + 2


async def test_a_real_turn_carries_the_skill_list(make_app):
    app = make_app()
    await approved(app)
    await online(app)
    await deliver(app, tg())
    assert "- report: Summarise a web table" in app.script.requests[-1]["messages"][0]["content"]
