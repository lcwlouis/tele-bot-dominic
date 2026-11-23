"""Tests for W8 memory self-edit proposals."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from dom.agent import run_turn
from dom.config import Config, load_config
from dom.home import LIMITS
from dom.llm import ChatClient
from dom.memory_edit import PROPOSE_MEMORY_EDIT_TOOL
from dom.memory_pass import MemoryPass
from dom.models import StoredMessage
from dom.store.store import MIGRATIONS, MIGRATIONS_DIR, TARGET_VERSION, migrate_database
from dom.telegram import handlers
from dom.tools import TurnCtx, enabled_tools
from test_owner_events import events, press
from test_traits import put
from test_web_auth import POST_HEADERS
from test_web_pages import browser  # noqa: F401
from tghelpers import FINAL, FRIEND, GROUP, OWNER, tg


def _enable_self_edit(app) -> None:
    app.config = app.config.model_copy(update={"memory_self_edit": True})


def _ctx(app, *, scope="owner_dm", chat_id=OWNER, sender_id=OWNER) -> TurnCtx:
    return TurnCtx(
        scope=scope,
        chat_id=chat_id,
        sender_id=sender_id,
        home=app.home.scoped(allow_private=scope == "owner_dm"),
        store=app.store,
        owner_id=app.owner_id,
        app=app,
    )


# ---- off by default & config ------------------------------------------------


def test_memory_self_edit_is_off_by_default(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[models.chat]\nbase_url = "https://example.test/v1"\nmodel = "m"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.memory_self_edit is False
    assert Config(home=tmp_path).memory_self_edit is False

    (tmp_path / "config.toml").write_text(
        'memory_self_edit = true\n[models.chat]\nbase_url = "https://example.test/v1"\nmodel = "m"\n'
    )
    assert load_config(tmp_path).memory_self_edit is True


async def test_off_by_default_tool_not_offered_and_handler_refuses(make_app):
    app = make_app()
    assert app.config.memory_self_edit is False
    for scope in ("owner_dm", "friend_dm", "group"):
        names = [tool.name for tool in await enabled_tools(app, scope)]
        assert PROPOSE_MEMORY_EDIT_TOOL.name not in names

    result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "private/USER.md", "op": "add", "text": "Likes tea", "reason": "Add preference"},
        _ctx(app),
    )
    assert result.startswith("error:") and "turned off" in result
    assert await app.proposals.pending() == []
    assert app.home.scoped(allow_private=True).read("private/USER.md") == ""


# ---- owner-DM-only enforcement ----------------------------------------------


async def test_owner_dm_only_when_enabled(make_app):
    app = make_app()
    _enable_self_edit(app)

    assert PROPOSE_MEMORY_EDIT_TOOL.name in [t.name for t in await enabled_tools(app, "owner_dm")]
    assert PROPOSE_MEMORY_EDIT_TOOL.name not in [t.name for t in await enabled_tools(app, "friend_dm")]
    assert PROPOSE_MEMORY_EDIT_TOOL.name not in [t.name for t in await enabled_tools(app, "group")]

    bad_contexts = [
        _ctx(app, scope="friend_dm", chat_id=FRIEND, sender_id=FRIEND),
        _ctx(app, scope="group", chat_id=GROUP, sender_id=OWNER),
        _ctx(app, scope="group", chat_id=GROUP, sender_id=FRIEND),
        _ctx(app, scope="owner_dm", chat_id=OWNER, sender_id=FRIEND),
        _ctx(app, scope="owner_dm", chat_id=FRIEND, sender_id=OWNER),
    ]
    for ctx in bad_contexts:
        result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
            {"file": "private/USER.md", "op": "add", "text": "Likes tea", "reason": "Add preference"},
            ctx,
        )
        assert result.startswith("error:") and "owner's DM" in result
    assert await app.proposals.pending() == []


# ---- approve, reject, card diff, owner events -------------------------------


async def test_approve_writes_note_shows_diff_card_and_records_owner_events(make_app):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Drinks coffee\n- Lives near the park")

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {
            "file": "USER.md",
            "op": "replace",
            "old": "Drinks coffee",
            "text": "Drinks matcha",
            "reason": "Switched from coffee to matcha",
        },
        _ctx(app),
    )
    payload = json.loads(raw)
    proposal_id = payload["id"]
    assert payload["status"] == "waiting for the owner's approval"

    # Nothing is written before the owner approves.
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Drinks coffee\n- Lives near the park"

    # Approval card sent silently to owner DM with unified diff, one-line reason, and buttons.
    (sent,) = app.bot.of("send", OWNER)
    card_text = sent[2]
    assert sent[3]["disable_notification"] is True
    assert f"#{proposal_id}" in card_text
    assert "private/USER.md: Switched from coffee to matcha" in card_text
    assert "--- a/private/USER.md" in card_text
    assert "+++ b/private/USER.md" in card_text
    assert "-- Drinks coffee" in card_text
    assert "+- Drinks matcha" in card_text
    assert [(b.text, b.callback_data) for b in sent[3]["reply_markup"].inline_keyboard[0]] == [
        ("Approve", f"prop:yes:{proposal_id}"),
        ("Reject", f"prop:no:{proposal_id}"),
    ]

    # Non-owner tap is ignored.
    await press(app, f"prop:yes:{proposal_id}", FRIEND)
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Drinks coffee\n- Lives near the park"

    # Double-tap applies exactly once.
    await asyncio.gather(*(press(app, f"prop:yes:{proposal_id}") for _ in range(2)))
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Drinks matcha\n- Lives near the park"
    assert {call[2] for call in app.bot.of("answer")} == {"Approved", "already handled"}
    assert await app.proposals.pending() == []

    # Creation and decision are recorded as owner events.
    event_texts = [e["text"] for e in await events(app)]
    assert any(f"Proposed #{proposal_id}" in t for t in event_texts)
    assert any(f"Approved proposal #{proposal_id}" in t for t in event_texts)


async def test_reject_discards_proposal_leaves_file_unchanged_and_records_event(make_app):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/NOTES.md", "- Keep this intact")

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {
            "file": "NOTES.md",
            "op": "remove",
            "old": "Keep this intact",
            "reason": "Clean up old note",
        },
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    await press(app, f"prop:no:{proposal_id}")

    assert app.bot.of("answer")[-1][2] == "Rejected"
    assert app.home.scoped(allow_private=True).read("private/NOTES.md") == "- Keep this intact"
    assert await app.proposals.pending() == []
    assert any(f"Rejected proposal #{proposal_id}" in e["text"] for e in await events(app))


async def test_bulk_approve_does_not_silently_apply_memory_edits(make_app):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Old fact")

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "private/USER.md", "op": "replace", "old": "Old fact", "text": "New fact", "reason": "Update"},
        _ctx(app),
    )
    edit_id = json.loads(raw)["id"]
    await app.proposals.create("memory_fact", "Prefers tea", {"file": "NOTES.md", "text": "Prefers tea"}, "example")

    await handlers.handle_message(app, tg(OWNER, OWNER, "/proposals"))
    bulk = next(
        b.callback_data
        for row in app.bot.of("send", OWNER)[-1][3]["reply_markup"].inline_keyboard
        for b in row
        if b.text == "Approve all on this page"
    )
    await press(app, bulk)
    assert "1 memory edit needs its own Approve." in app.bot.of("answer")[-1][2]
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Old fact"
    assert [row["id"] for row in await app.proposals.pending()] == [edit_id]


# ---- all supported note types & dashboard rendering -------------------------


@pytest.mark.parametrize("target,canonical", [
    ("private/USER.md", "private/USER.md"),
    ("private/NOTES.md", "private/NOTES.md"),
    ("private/JOURNAL.md", "private/JOURNAL.md"),
    ("memory/JOURNAL.md", "memory/JOURNAL.md"),
    ("memory/MEMORY.md", "memory/MEMORY.md"),
    (f"memory/people/{FRIEND}.md", f"memory/people/{FRIEND}.md"),
    (f"people/{FRIEND}.md", f"memory/people/{FRIEND}.md"),
    (f"memory/chats/{GROUP}.md", f"memory/chats/{GROUP}.md"),
    (f"chats/{GROUP}.md", f"memory/chats/{GROUP}.md"),
])
async def test_all_supported_note_types_can_be_proposed_and_approved(make_app, target, canonical):
    app = make_app()
    _enable_self_edit(app)

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": target, "op": "add", "text": "Enjoys hiking on weekends", "reason": "Record shared detail"},
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    assert await app.proposals.decide(proposal_id, True) == "Approved"
    content = app.home.scoped(allow_private=canonical.startswith("private/")).read(canonical)
    assert "Enjoys hiking on weekends" in content


async def test_dashboard_renders_memory_edit_diff_and_approves(make_app, browser):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Drinks coffee")

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {
            "file": "private/USER.md",
            "op": "replace",
            "old": "Drinks coffee",
            "text": "Drinks oolong",
            "reason": "Switched to oolong",
        },
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]

    async with browser(app) as client:
        page = await client.get("/proposals?kind=memory_edit")
        assert page.status_code == 200
        assert "Memory edit" in page.text
        assert "private/USER.md: Switched to oolong" in page.text
        assert "<del>-- Drinks coffee</del>" in page.text
        assert "<ins>+- Drinks oolong</ins>" in page.text

        response = await client.post(f"/proposals/{proposal_id}/approve", headers=POST_HEADERS)
        assert response.status_code == 200
        assert "Approved" in response.text
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Drinks oolong"


# ---- stale-file refusal -----------------------------------------------------


async def test_stale_file_refusal_on_approve(make_app):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Original line")

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {
            "file": "private/USER.md",
            "op": "replace",
            "old": "Original line",
            "text": "Proposed line",
            "reason": "Update preference",
        },
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]

    # File changes before the owner approves.
    put(app.home, "private/USER.md", "- Original line\n- Edited meanwhile")

    await press(app, f"prop:yes:{proposal_id}")
    toast = app.bot.of("answer")[-1][2]
    assert "changed" in toast
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Original line\n- Edited meanwhile"
    assert [row["id"] for row in await app.proposals.pending()] == [proposal_id]
    assert any(f"Could not approve proposal #{proposal_id}" in e["text"] for e in await events(app))


# ---- private-to-public refusal ----------------------------------------------


@pytest.mark.parametrize("public_target", [
    "memory/MEMORY.md",
    "memory/JOURNAL.md",
    f"memory/people/{FRIEND}.md",
    f"memory/chats/{GROUP}.md",
])
async def test_private_to_public_refused_at_proposal_time(make_app, public_target):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Secret allergy: allergic to shellfish")
    put(app.home, "private/NOTES.md", "- Private project codename is Silver Heron")
    put(app.home, "private/JOURNAL.md", "## 2026-11-22\nOwner shared a private worry about changing careers.\n")

    for leaked in (
        "Secret allergy: allergic to shellfish",
        "allergic to shellfish",
        "Private project codename is Silver Heron",
        "codename is Silver Heron",
        "Owner shared a private worry about changing careers.",
    ):
        result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
            {"file": public_target, "op": "add", "text": leaked, "reason": "Copy note"},
            _ctx(app),
        )
        assert result.startswith("error:") and "private notes" in result

    assert await app.proposals.pending() == []
    assert app.home.read(public_target) == ""


async def test_credentials_cannot_be_moved_into_public_note(make_app, monkeypatch):
    app = make_app()
    _enable_self_edit(app)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "very-secret-test-value-xyz")

    result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "memory/MEMORY.md", "op": "add", "text": "Token is very-secret-test-value-xyz", "reason": "Save"},
        _ctx(app),
    )
    assert result.startswith("error:") and "credential" in result
    assert await app.proposals.pending() == []


async def test_private_note_added_after_proposal_refused_at_approval(make_app):
    app = make_app()
    _enable_self_edit(app)

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": f"memory/people/{FRIEND}.md", "op": "add", "text": "Keeps a hidden spare key under the blue pot", "reason": "Note"},
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    put(app.home, "private/NOTES.md", "- Keeps a hidden spare key under the blue pot")

    outcome = await app.proposals.decide(proposal_id, True)
    assert "private notes" in outcome
    assert app.home.read(f"memory/people/{FRIEND}.md") == ""
    assert [row["id"] for row in await app.proposals.pending()] == [proposal_id]


async def test_public_journal_same_date_heading_as_private_journal_allowed(make_app):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/JOURNAL.md", "## 2026-11-22\nOwner felt tired after a long week.\n")

    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {
            "file": "memory/JOURNAL.md",
            "op": "add",
            "text": "## 2026-11-22\nLively chat in the group about board games.",
            "reason": "Add public journal entry",
        },
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    assert await app.proposals.decide(proposal_id, True) == "Approved"
    assert "Lively chat in the group" in app.home.read("memory/JOURNAL.md")


# ---- path traversal, out-of-scope files, and symlinks -----------------------


@pytest.mark.parametrize("bad_path", [
    "SOUL.md",
    "config.toml",
    ".env",
    "memory/HABITS.md",
    "HABITS.md",
    "JOURNAL.md",
    "skills/tea/SKILL.md",
    "plugins/demo/plugin.toml",
    "../etc/passwd",
    "/etc/passwd",
    "memory/../SOUL.md",
    "private/../config.toml",
    "memory/people/../SOUL.md",
    "memory/people/../../config.toml",
    "memory/people/abc.md",
    "memory/chats/alias.md",
    "memory/people/111.md\n",
    "private\\USER.md",
    "~/USER.md",
    "",
])
async def test_path_traversal_and_out_of_scope_files_refused(make_app, bad_path):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "SOUL.md", "Original soul.")
    put(app.home, "memory/HABITS.md", 'sleep: "23:00-07:00"')

    result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": bad_path, "op": "set", "text": "tampered", "reason": "Try invalid file"},
        _ctx(app),
    )
    assert result.startswith("error:")
    assert await app.proposals.pending() == []
    assert app.home.read("SOUL.md") == "Original soul."
    assert app.home.read("memory/HABITS.md") == 'sleep: "23:00-07:00"'


async def test_symlinks_rejected_at_proposal_and_approval(make_app, tmp_path_factory):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Private owner info")

    # Symlink inside memory/ pointing to private/USER.md
    people_dir = app.home.root / "memory" / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    (people_dir / f"{FRIEND}.md").symlink_to(app.home.root / "private" / "USER.md")

    result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": f"memory/people/{FRIEND}.md", "op": "add", "text": "Hi", "reason": "Edit linked note"},
        _ctx(app),
    )
    assert result.startswith("error:")
    assert await app.proposals.pending() == []

    # Symlink pointing outside home
    outside = tmp_path_factory.mktemp("outside") / "notes.md"
    outside.write_text("- External")
    (app.home.root / "private" / "NOTES.md").symlink_to(outside)

    result = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "private/NOTES.md", "op": "add", "text": "Tamper", "reason": "Edit external symlink"},
        _ctx(app),
    )
    assert result.startswith("error:")
    assert outside.read_text() == "- External"
    # While a private note is a link, the private-to-public screen can't read it, so public edits are refused too
    blocked = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": f"memory/chats/{GROUP}.md", "op": "add", "text": "Group joke", "reason": "Add joke"},
        _ctx(app),
    )
    assert blocked.startswith("error:") and "Cannot check private/NOTES.md" in blocked
    (app.home.root / "private" / "NOTES.md").unlink()

    # File turned into a symlink AFTER proposal creation is refused on approve
    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": f"memory/chats/{GROUP}.md", "op": "add", "text": "Group joke", "reason": "Add joke"},
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    chats_dir = app.home.root / "memory" / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    (chats_dir / f"{GROUP}.md").symlink_to(outside)

    outcome = await app.proposals.decide(proposal_id, True)
    assert outcome != "Approved"
    assert outside.read_text() == "- External"


# ---- size bounds ------------------------------------------------------------


@pytest.mark.parametrize("rel,limit", [
    ("private/USER.md", LIMITS["private/USER.md"]),
    ("private/NOTES.md", LIMITS["private/NOTES.md"]),
    ("memory/MEMORY.md", LIMITS["memory/MEMORY.md"]),
    (f"memory/people/{FRIEND}.md", LIMITS["memory/people/*.md"]),
    (f"memory/chats/{GROUP}.md", LIMITS["memory/chats/*.md"]),
])
async def test_size_bounds_enforced_and_shrinking_allowed(make_app, rel, limit):
    app = make_app()
    _enable_self_edit(app)

    # Exceeding limit is refused.
    res = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": rel, "op": "set", "text": "a" * (limit + 1), "reason": "Too long"},
        _ctx(app),
    )
    assert res.startswith("error:") and "over limit" in res
    assert await app.proposals.pending() == []

    # Exact limit is accepted.
    ok_raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": rel, "op": "set", "text": "a" * limit, "reason": "At exact limit"},
        _ctx(app),
    )
    ok_id = json.loads(ok_raw)["id"]
    assert await app.proposals.decide(ok_id, True) == "Approved"
    assert len(app.home.scoped(allow_private=rel.startswith("private/")).read(rel)) == limit

    # Pre-existing over-limit file cannot grow, but can shrink.
    put(app.home, rel, "b" * (limit + 50))
    grow = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": rel, "op": "add", "text": "more text", "reason": "Grow over limit"},
        _ctx(app),
    )
    assert grow.startswith("error:") and "over limit" in grow

    shrink_raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": rel, "op": "set", "text": "b" * (limit + 10), "reason": "Trim down"},
        _ctx(app),
    )
    shrink_id = json.loads(shrink_raw)["id"]
    assert await app.proposals.decide(shrink_id, True) == "Approved"
    assert len(app.home.scoped(allow_private=rel.startswith("private/")).read(rel)) == limit + 10


async def test_journals_allow_entries_beyond_bounded_note_limits(make_app):
    app = make_app()
    _enable_self_edit(app)
    long_entry = "## 2026-11-22\n" + ("Long journal reflection. " * 200)
    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "private/JOURNAL.md", "op": "add", "text": long_entry, "reason": "Add journal entry"},
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    assert await app.proposals.decide(proposal_id, True) == "Approved"
    assert long_entry.strip() in app.home.scoped(allow_private=True).read("private/JOURNAL.md")


# ---- coexistence with MemoryPass & full agent turn --------------------------


async def test_memory_pass_keeps_working_and_triggers_stale_refusal(make_app):
    memory_reply = json.dumps({
        "ops": [{"file": "USER.md", "op": "add", "text": "Started learning pottery"}],
        "summary": "started learning pottery",
    })
    app = make_app(contents=(memory_reply,))
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Plays chess")

    # Propose a self-edit before the memory pass runs.
    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "private/USER.md", "op": "replace", "old": "Plays chess", "text": "Plays go", "reason": "Board game"},
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]

    # Record a message and run the background memory pass as today.
    await app.store.set_chat_auth_status(OWNER, "approved")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(OWNER), msg_id=10, sender_user_id=OWNER, sender_name="Owner",
        role="user", is_bot=False, visibility_scope=app.store.visibility_for(OWNER),
        text="I started learning pottery today", created_at="2025-12-31T10:00:00+00:00",
    ))
    assert await MemoryPass(app).run(OWNER) is True
    assert "Started learning pottery" in app.home.scoped(allow_private=True).read("private/USER.md")

    # Approving the now-stale self-edit refuses instead of clobbering the memory pass write.
    outcome = await app.proposals.decide(proposal_id, True)
    assert "changed" in outcome
    current = app.home.scoped(allow_private=True).read("private/USER.md")
    assert "Started learning pottery" in current and "Plays chess" in current


async def test_agent_turn_calls_propose_memory_edit_in_owner_dm(make_app):
    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Drinks coffee")

    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "propose_memory_edit",
                    "arguments": json.dumps({
                        "file": "private/USER.md",
                        "op": "replace",
                        "old": "Drinks coffee",
                        "text": "Drinks espresso",
                        "reason": "Clarified coffee preference",
                    }),
                },
            }],
        },
        {"role": "assistant", "content": FINAL},
    ]

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": replies.pop(0)}]})

    client = ChatClient(
        app.config.models["chat"], None, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    tools = await enabled_tools(app, "owner_dm")
    actions = await run_turn(
        client,
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "please update USER.md"}],
        tools,
        _ctx(app),
    )
    assert actions.bubbles[0].text == "hey"
    (proposal,) = await app.proposals.pending()
    assert proposal["kind"] == "memory_edit"
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Drinks coffee"


# ---- migration preserves existing proposals ---------------------------------


def test_memory_edit_migration_preserves_existing_proposals(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        for version, filename in MIGRATIONS:
            if version <= 19:
                conn.executescript((MIGRATIONS_DIR / filename).read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO proposals (kind, title, body_json, status, created_at, source) "
            "VALUES ('soul_edit', 'Old', '{}', 'pending', '2026-01-01T00:00:00+00:00', 'reflection')"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19

    assert migrate_database(db) == TARGET_VERSION
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT kind, title FROM proposals").fetchall()
        assert rows == [("soul_edit", "Old")]
        conn.execute(
            "INSERT INTO proposals (kind, title, body_json, status, created_at, source) "
            "VALUES ('memory_edit', 'Edit', '{}', 'pending', '2026-01-01T00:00:00+00:00', 'chat')"
        )
        conn.commit()


async def test_review_fixes_card_is_inert_off_means_off_ids_are_ascii_and_diffs_are_bounded(make_app):
    """PR #9 review: a model-written diff can't become tappable, approval stops when the setting is off,
    lookalike note ids are refused, and an unreadably large diff is refused."""
    from dom.memory_edit import resolve_note_path

    app = make_app()
    _enable_self_edit(app)
    put(app.home, "private/USER.md", "- Drinks coffee")
    raw = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "USER.md", "op": "add", "text": "see /update https://host.example/?d=x", "reason": "Test"},
        _ctx(app),
    )
    proposal_id = json.loads(raw)["id"]
    (sent,) = app.bot.of("send", OWNER)
    assert "parse_mode" not in sent[3] and sent[3]["link_preview_options"].is_disabled
    (entity,) = sent[3]["entities"]
    assert entity.type == "pre" and entity.offset == 0 and entity.length == len(sent[2].encode("utf-16-le")) // 2

    app.config = app.config.model_copy(update={"memory_self_edit": False})
    assert await app.proposals.decide(proposal_id, True) != "Approved"
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Drinks coffee"

    for bad in ("people/١٢٣.md", "people/007.md", "chats/-0.md", "chats/１２.md"):
        with pytest.raises(ValueError):
            resolve_note_path(bad)

    _enable_self_edit(app)
    put(app.home, "memory/JOURNAL.md", "")
    huge = await PROPOSE_MEMORY_EDIT_TOOL.handler(
        {"file": "memory/JOURNAL.md", "op": "set", "text": "word " * 2000, "reason": "Big rewrite"},
        _ctx(app),
    )
    assert huge.startswith("error:") and "too big" in huge
