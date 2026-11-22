"""W7 (E40): owner-only retire and delete with deletion archive, stale/race checks, crash recovery and DM commands."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dom import deletion
from dom.deletion import (
    MANIFEST_FILE,
    STALE_MESSAGE,
    STATE_FILE,
    deleted_root,
    prune_deletions,
    recover_deletions,
    stage_deletion,
    stamp_token,
)
from dom.prompt import build_messages
from dom.selfwork import task_files
from dom.skills import Skill, is_retired, prompt_block, skill_hash, skill_view, skills_list, write_skill
from dom.telegram import handlers, owner
from dom.telegram.owner import OWNER_COMMANDS
from dom.tools import SKILL_VIEW_TOOL, SKILLS_LIST_TOOL, TurnCtx
from test_owner_events import events, press
from test_plugin_surfaces import SURFACES, approve as approve_surface_plugin
from test_plugin_tools import MANIFEST, draft as draft_plugin, review_path, status as plugin_status
from test_web_auth import POST_HEADERS
from test_web_pages import browser  # noqa: F401
from tghelpers import FRIEND, OWNER, tg

BODY = "Open the page, check the table, and write a brief note."


def _archives(home: Path) -> set[Path]:
    root = deleted_root(home)
    return {p for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()


def _new_archives(home: Path, before: set[Path]) -> list[Path]:
    return sorted(_archives(home) - before)


def _manifest(folder: Path) -> dict:
    return json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))


# ---- Skills: retire survives edit, restore, delete, DM commands, version race ------------


async def test_skill_retire_survives_approved_edit_restore_and_delete(make_app, browser):
    app = make_app()
    before = _archives(app.home.root)
    write_skill(app.home, Skill("notes", "Take structured notes", BODY), "")
    # Include a .bak file and a state.db-named file inside the skill dir to confirm move preserves everything.
    (app.home.root / "skills" / "notes" / "extra.bak").write_text("backup-named")
    (app.home.root / "skills" / "notes" / "state.db.note").write_text("state-named")
    ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, app.store, OWNER, app=app)

    async with browser(app) as client:
        page = (await client.get("/memory")).text
        assert 'id="skills"' in page and "notes" in page and "hx-confirm=" in page
        assert (await client.post("/skills/notes/retire")).status_code == 403
        assert (await client.post("/skills/notes/delete")).status_code == 403
        assert (await client.post("/skills/Bad Name/delete", headers=POST_HEADERS, data={"expected": "x"})).status_code == 404

        # Retire: files stay on disk, Dom no longer lists or uses it
        retired = await client.post("/skills/notes/retire", headers=POST_HEADERS)
        assert retired.status_code == 200 and "Retired skill notes." in retired.text
        assert is_retired(app.home, "notes")
        assert skills_list(app.home) == []
        assert skill_view(app.home, "notes") is None
        assert prompt_block(app.home) == ""
        assert "<skills>" not in build_messages(app.home, "owner_dm", OWNER, [], "<chat_window/>", "note")[0]["content"]
        assert json.loads(await SKILLS_LIST_TOOL.handler({}, ctx)) == []
        assert await SKILL_VIEW_TOOL.handler({"name": "notes"}, ctx) == "Skill not found."
        assert _new_archives(app.home.root, before) == []
        assert any("Retired skill notes" in row["text"] for row in await events(app))

        # Approving an edit to a retired skill keeps it retired!
        old_hash = skill_hash(app.home, "notes")
        proposal_id = await app.proposals.create(
            "skill", "Skill: notes", {"name": "notes", "description": "Take structured notes", "body": BODY + " v2"}, "chat",
        )
        assert await app.proposals.decide(proposal_id, True) == "Approved"
        assert is_retired(app.home, "notes")
        assert skills_list(app.home) == []
        assert skill_view(app.home, "notes") is None
        new_hash = skill_hash(app.home, "notes")
        assert new_hash != old_hash

        # Stale confirmation with old_hash is refused!
        stale = await client.post("/skills/notes/delete", headers=POST_HEADERS, data={"expected": old_hash})
        assert stale.status_code == 409 and STALE_MESSAGE in stale.text
        assert (app.home.root / "skills" / "notes" / "SKILL.md").is_file()
        assert _new_archives(app.home.root, before) == []

        # Explicit Restore clears .retired and Dom sees the updated v2 text
        restored = await client.post("/skills/notes/restore", headers=POST_HEADERS)
        assert restored.status_code == 200 and "Restored skill notes." in restored.text
        assert not is_retired(app.home, "notes")
        assert skill_view(app.home, "notes") == BODY + " v2"
        assert any("Restored skill notes" in row["text"] for row in await events(app))

        # Delete with current hash moves files (including .bak and state.db*) into deletion archive
        deleted = await client.post("/skills/notes/delete", headers=POST_HEADERS, data={"expected": new_hash})
        assert deleted.status_code == 200 and "Deleted skill notes." in deleted.text
        assert not (app.home.root / "skills" / "notes").exists()
        (archive,) = _new_archives(app.home.root, before)
        assert (archive / STATE_FILE).read_text().strip() == "committed"
        manifest = _manifest(archive)
        assert manifest["kind"] == "skill" and manifest["event"] == "Deleted skill notes"
        assert (archive / "files" / "0" / "SKILL.md").is_file()
        assert (archive / "files" / "0" / "extra.bak").read_text() == "backup-named"
        assert (archive / "files" / "0" / "state.db.note").read_text() == "state-named"
        assert any("Deleted skill notes" in row["text"] for row in await events(app))
        assert (await client.post("/skills/notes/delete", headers=POST_HEADERS, data={"expected": new_hash})).status_code == 404


async def test_skill_dm_commands_retire_restore_delete_and_long_name(make_app):
    app = make_app()
    before = _archives(app.home.root)
    long_name = "a" + "-long-skill" * 5  # 56 chars -> exceeds 64 bytes when combined with prefix+hash
    assert len(long_name) <= 64
    write_skill(app.home, Skill(long_name, "Long skill description", BODY), "")

    # Non-owner DM command is ignored
    await handlers.handle_message(app, tg(FRIEND, FRIEND, f"/skills retire {long_name}"))
    assert not is_retired(app.home, long_name)

    # /skills list sends buttons <= 64 bytes even for a 56-char skill name
    await handlers.handle_message(app, tg(OWNER, OWNER, "/skills"))
    sent = app.bot.of("send", OWNER)[-1]
    retire_btn, del_btn = sent[3]["reply_markup"].inline_keyboard[0]
    assert len(retire_btn.callback_data.encode("utf-8")) <= 64
    assert len(del_btn.callback_data.encode("utf-8")) <= 64

    # Retire and restore via DM commands and callbacks
    await handlers.handle_message(app, tg(OWNER, OWNER, f"/skills retire {long_name}"))
    assert is_retired(app.home, long_name)
    await handlers.handle_message(app, tg(OWNER, OWNER, f"/skills restore {long_name}"))
    assert not is_retired(app.home, long_name)

    # Tap Delete -> confirmation card -> non-owner tap ignored -> owner confirms
    await press(app, del_btn.callback_data)
    confirm = app.bot.of("send", OWNER)[-1]
    yes_btn, no_btn = confirm[3]["reply_markup"].inline_keyboard[0]
    assert len(yes_btn.callback_data.encode("utf-8")) <= 64
    await press(app, yes_btn.callback_data, FRIEND)
    assert (app.home.root / "skills" / long_name).is_dir()
    await press(app, yes_btn.callback_data)
    assert not (app.home.root / "skills" / long_name).exists()
    assert len(_new_archives(app.home.root, before)) == 1
    assert any(f"Deleted skill {long_name}" in row["text"] for row in await events(app))


async def test_skill_version_race_refuses_delete_if_approved_during_archive_staging(make_app, monkeypatch):
    app = make_app()
    before = _archives(app.home.root)
    write_skill(app.home, Skill("notes", "Take structured notes", BODY), "")
    old_hash = skill_hash(app.home, "notes")
    real_stage = owner.stage_deletion

    def race_stage(*args, **kwargs):
        archive = real_stage(*args, **kwargs)
        # Simulate a concurrent approved skill edit arriving right after staging!
        expected_old = (app.home.root / "skills" / "notes" / "SKILL.md").read_text()
        write_skill(app.home, Skill("notes", "Take structured notes", BODY + " concurrently updated"), expected_old)
        return archive

    monkeypatch.setattr(owner, "stage_deletion", race_stage)
    ok, message = await owner.delete_skill(app, "notes", old_hash)
    assert not ok and message == STALE_MESSAGE
    assert skill_view(app.home, "notes") == BODY + " concurrently updated"
    assert _new_archives(app.home.root, before) == []


# ---- Plugins: retire, delete, mid-call refusal, version race, menu retry -----------------


async def test_plugin_retire_delete_mid_call_stale_and_menu_retry(make_app, browser):
    app = make_app()
    before = _archives(app.home.root)
    await approve_surface_plugin(app, browser)
    row = await plugin_status(app)
    route = review_path(row)

    # Make set_my_commands fail during retire to verify background tick republish
    orig_set = app.bot.set_my_commands
    fail_menu = True

    async def flaky_set(commands, scope=None, **kw):
        if fail_menu:
            raise RuntimeError("telegram unreachable")
        return await orig_set(commands, scope=scope, **kw)

    app.bot.set_my_commands = flaky_set

    async with browser(app) as client:
        page = (await client.get("/plugins")).text
        assert "hx-confirm=" in page and 'action="/plugins/convert/delete"' in page
        assert "/plugin-panels/convert/summary" in page
        assert (await client.post("/plugins/convert/delete")).status_code == 403

        # Retire fails set_my_commands -> marks menu_dirty; files kept, panel disappears
        retired = await client.post(f"{route}/retire", headers=POST_HEADERS)
        assert retired.status_code == 200 and "Disabled" in retired.text
        assert app.menu_dirty is True
        assert (app.home.root / "plugins" / "convert" / "main.py").is_file()
        assert "/plugin-panels/convert/summary" not in (await client.get("/plugins")).text

        # Next background tick republishes from current registry state once Telegram recovers
        fail_menu = False
        app.bot.calls.clear()
        await app.dispatch._background_jobs()
        assert app.menu_dirty is False
        assert app.bot.of("set_commands")[-1][1] == OWNER_COMMANDS

        # Seed KV state to confirm deletion archives & deletes it
        await app.store.kv_set("plugin-storage:convert", '{"count": 1}')

        # Mid-call refusal
        with app.plugin_runner.active("convert"):
            busy = await client.post("/plugins/convert/delete", headers=POST_HEADERS, data={"expected": row["hash"]})
            assert busy.status_code == 409 and "running" in busy.text.lower()
        assert _new_archives(app.home.root, before) == []

        # Stale hash refusal
        stale = await client.post("/plugins/convert/delete", headers=POST_HEADERS, data={"expected": "0" * 64})
        assert stale.status_code == 409 and STALE_MESSAGE in stale.text
        assert _new_archives(app.home.root, before) == []

        # Delete with matching hash -> archives files + DB + KV, deletes all, refreshes menu
        app.bot.calls.clear()
        deleted = await client.post("/plugins/convert/delete", headers=POST_HEADERS, data={"expected": row["hash"]})
        assert deleted.status_code == 200 and "Deleted" in deleted.text
        assert not (app.home.root / "plugins" / "convert").exists()
        assert await app.store.list_plugins() == []
        assert await app.store.kv_get("plugin-files:convert") is None
        assert await app.store.kv_get("plugin-storage:convert") is None
        assert app.bot.of("set_commands")[-1][1] == OWNER_COMMANDS
        (archive,) = _new_archives(app.home.root, before)
        assert (archive / STATE_FILE).read_text().strip() == "committed"
        manifest = _manifest(archive)
        assert manifest["rows"]["plugin"]["name"] == "convert"
        assert any(kv["key"] == "plugin-storage:convert" for kv in manifest["rows"]["kv"])
        assert (archive / "files" / "0" / "main.py").is_file()
        assert any("Deleted plugin convert" in e["text"] for e in await events(app))


async def test_plugin_version_race_refuses_delete_if_redrafted_during_archive_staging(make_app, monkeypatch):
    from dom.plugins import registry as reg_mod

    app = make_app()
    before = _archives(app.home.root)
    await draft_plugin(app, manifest=MANIFEST, source="print('v1')\n")
    v1_hash = (await plugin_status(app))["hash"]
    real_stage = reg_mod.stage_deletion

    def race_stage(*args, **kwargs):
        archive = real_stage(*args, **kwargs)
        # Concurrent edit/redraft changes plugin files on disk before delete re-acquires registry.lock!
        (app.home.root / "plugins" / "convert" / "main.py").write_text("print('v2')\n")
        return archive

    monkeypatch.setattr(reg_mod, "stage_deletion", race_stage)
    outcome = await app.plugins.delete("convert", v1_hash)
    assert outcome == STALE_MESSAGE
    assert (app.home.root / "plugins" / "convert" / "main.py").read_text() == "print('v2')\n"
    assert len(await app.store.list_plugins()) == 1
    assert _new_archives(app.home.root, before) == []


async def test_plugin_delete_from_owner_dm_confirms_and_rejects_stale_hash(make_app):
    app = make_app()
    before = _archives(app.home.root)
    await draft_plugin(app, manifest=SURFACES, source="print('v1')\n")
    v1_hash = (await plugin_status(app))["hash"]
    card_id = app.bot._next_id
    app.bot.calls.clear()

    await handlers.handle_message(app, tg(OWNER, OWNER, "/plugins"))
    sent, = app.bot.of("send", OWNER)
    buttons = sent[3]["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == "plugin:disable:convert"
    assert buttons[1].callback_data == f"plugin:del:convert:{v1_hash[:16]}"

    await press(app, buttons[1].callback_data)
    yes_btn = app.bot.of("send", OWNER)[-1][3]["reply_markup"].inline_keyboard[0][0]
    # Non-owner ignored
    await press(app, yes_btn.callback_data, FRIEND)
    assert len(await app.store.list_plugins()) == 1

    # Redraft v2 -> old confirm button is now stale and refused!
    await draft_plugin(app, manifest=SURFACES, source="print('v2')\n")
    await press(app, yes_btn.callback_data)
    assert app.bot.of("answer")[-1][2] == STALE_MESSAGE
    assert len(await app.store.list_plugins()) == 1

    # Confirm with current v2 hash succeeds and settles pending proposal card
    v2_hash = (await plugin_status(app))["hash"]
    await press(app, f"plugin:delyes:convert:{v2_hash[:16]}")
    assert await app.store.list_plugins() == []
    assert await app.proposals.pending() == []
    assert any(call[2] == card_id for call in app.bot.of("markup"))
    assert len(_new_archives(app.home.root, before)) == 1


# ---- Reminders: stale ID reuse, mid-delivery refusal, DM & dashboard ---------------------


async def test_reminder_delete_stale_id_reuse_and_in_use(make_app, browser):
    app = make_app()
    before = _archives(app.home.root)
    r1 = await app.dispatch.reminders.create("2026-06-01T12:00", "First reminder")
    row1 = await app.store.get_reminder(OWNER, r1)

    async with browser(app) as client:
        page = (await client.get("/reminders")).text
        assert "First reminder" in page and "hx-confirm=" in page
        assert (await client.post(f"/reminders/{r1}/delete")).status_code == 403

        # Refuses while mid-delivery
        app.dispatch.reminders._in_flight.add(r1)
        try:
            busy = await client.post(
                f"/reminders/{r1}/delete", headers=POST_HEADERS, data={"expected": row1["created_at"]},
            )
            assert busy.status_code == 409 and "delivered" in busy.text.lower()
            assert _new_archives(app.home.root, before) == []
        finally:
            app.dispatch.reminders._in_flight.discard(r1)

        # Delete r1 succeeds
        deleted = await client.post(
            f"/reminders/{r1}/delete", headers=POST_HEADERS, data={"expected": row1["created_at"]},
        )
        assert deleted.status_code == 200 and f"Deleted reminder #{r1}." in deleted.text
        assert await app.store.get_reminder(OWNER, r1) is None
        assert len(_new_archives(app.home.root, before)) == 1

        # Simulate SQLite INTEGER PRIMARY KEY reuse with a different created_at!
        app.dispatch.now.advance(seconds=5)
        reused = await app.dispatch.reminders.create("2026-06-02T12:00", "Reused ID reminder")
        assert reused == r1
        row_reused = await app.store.get_reminder(OWNER, reused)
        assert row_reused["created_at"] != row1["created_at"]

        # Stale confirmation bound to old created_at is refused!
        stale = await client.post(
            f"/reminders/{reused}/delete", headers=POST_HEADERS, data={"expected": row1["created_at"]},
        )
        assert stale.status_code == 409 and STALE_MESSAGE in stale.text
        assert await app.store.get_reminder(OWNER, reused) is not None

        # Stale DM callback bound to old created_at fingerprint is refused too!
        await press(app, f"rem:delyes:{reused}:{stamp_token(row1['created_at'])}")
        assert app.bot.of("answer")[-1][2] == STALE_MESSAGE
        assert await app.store.get_reminder(OWNER, reused) is not None

        # Valid DM delete confirm succeeds
        await handlers.handle_message(app, tg(OWNER, OWNER, f"/reminders delete {reused}"))
        yes_btn = app.bot.of("send", OWNER)[-1][3]["reply_markup"].inline_keyboard[0][0]
        await press(app, yes_btn.callback_data)
        assert await app.store.get_reminder(OWNER, reused) is None


# ---- Desktop task history: outside-DOM_HOME files & stale check --------------------------


async def test_desktop_task_delete_archives_outside_home_inbox_and_refuses_stale(make_app, browser):
    app = make_app()
    before = _archives(app.home.root)
    active_id = await app.store.enqueue_desktop_task(OWNER, "Still running")
    done_id = await app.store.enqueue_desktop_task(OWNER, "Finished task", container="other")
    await app.store.update_desktop_task(done_id, OWNER, ("queued",), "done", result="All set")
    done_row = await app.store.get_desktop_task(OWNER, done_id)
    active_row = await app.store.get_desktop_task(OWNER, active_id)

    # Files OUTSIDE DOM_HOME (desktop-inbox/task-<id>/) including a .bak filename!
    inbox = app.home.root.resolve().parent / "desktop-inbox" / f"task-{done_id}"
    inbox.mkdir(parents=True)
    (inbox / "report.bak").write_text("attachment-outside-home")

    async with browser(app) as client:
        page = (await client.get("/desktop")).text
        assert "Finished task" in page and "hx-confirm=" in page
        assert (await client.post(f"/desktop/tasks/{done_id}/delete")).status_code == 403

        # Active refused
        refused = await client.post(
            f"/desktop/tasks/{active_id}/delete", headers=POST_HEADERS, data={"expected": active_row["created_at"]},
        )
        assert refused.status_code == 409 and "queued" in refused.text.lower()
        assert _new_archives(app.home.root, before) == []

        # Stale created_at refused
        stale = await client.post(
            f"/desktop/tasks/{done_id}/delete", headers=POST_HEADERS, data={"expected": "2000-01-01T00:00:00+00:00"},
        )
        assert stale.status_code == 409 and STALE_MESSAGE in stale.text
        assert inbox.exists()

        # Matching created_at moves outside-home inbox folder into archive and deletes row
        deleted = await client.post(
            f"/desktop/tasks/{done_id}/delete", headers=POST_HEADERS, data={"expected": done_row["created_at"]},
        )
        assert deleted.status_code == 200 and f"Deleted desktop task #{done_id}." in deleted.text
        assert await app.store.get_desktop_task(OWNER, done_id) is None
        assert not inbox.exists()
        (archive,) = _new_archives(app.home.root, before)
        assert (archive / "files" / "0" / "report.bak").read_text() == "attachment-outside-home"
        assert _manifest(archive)["rows"]["desktop_task"]["id"] == done_id
        assert any(f"Deleted desktop task #{done_id}" in e["text"] for e in await events(app))


# ---- Self-directed task history: dashboard + /ownwork DM + screenshots -------------------


async def test_self_work_delete_dashboard_and_ownwork_dm_command(make_app, browser):
    app = make_app()
    before = _archives(app.home.root)
    now = app.dispatch.now()
    planned, done_web, done_dm = await app.store.add_self_tasks([
        {"goal": "Active plan", "why": "Curious", "tools": ["browse"], "sites": ["example.org"], "minutes": 5, "steps": 10},
        {"goal": "Web delete", "why": "Done", "tools": ["browse"], "sites": ["example.org"], "minutes": 5, "steps": 10},
        {"goal": "DM delete", "why": "Done", "tools": ["browse"], "sites": ["example.org"], "minutes": 5, "steps": 10},
    ], now, now)
    await app.store.update_self_task(done_web["id"], ("planned",), "done", result="Found notes", files=["shot.png"])
    await app.store.update_self_task(done_dm["id"], ("planned",), "done", result="DM notes")
    shots = task_files(app.home.root, done_web["id"])
    shots.mkdir(parents=True)
    (shots / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    async with browser(app) as client:
        page = (await client.get("/own-work")).text
        assert f'action="/own-work/{done_web["id"]}/delete"' in page and "hx-confirm=" in page
        assert f'/own-work/{planned["id"]}/delete' not in page
        assert (await client.post(f"/own-work/{done_web['id']}/delete")).status_code == 403

        # Active refused
        refused = await client.post(
            f"/own-work/{planned['id']}/delete", headers=POST_HEADERS, data={"expected": planned["announced_at"]},
        )
        assert refused.status_code == 409 and "planned" in refused.text.lower()

        # Stale refused
        stale = await client.post(
            f"/own-work/{done_web['id']}/delete", headers=POST_HEADERS, data={"expected": "1999-01-01T00:00:00+00:00"},
        )
        assert stale.status_code == 409 and STALE_MESSAGE in stale.text
        assert shots.exists()

        # Valid web delete archives screenshots and removes row
        deleted = await client.post(
            f"/own-work/{done_web['id']}/delete", headers=POST_HEADERS, data={"expected": done_web["announced_at"]},
        )
        assert deleted.status_code == 200 and f"Deleted task #{done_web['id']}." in deleted.text
        assert await app.store.get_self_task(done_web["id"]) is None
        assert not shots.exists()
        (archive,) = _new_archives(app.home.root, before)
        assert (archive / "files" / "0" / "shot.png").read_bytes() == b"\x89PNG\r\n\x1a\nfake"

    # /ownwork DM command: non-owner ignored, owner lists & deletes with confirm + stale check
    await handlers.handle_message(app, tg(FRIEND, FRIEND, "/ownwork"))
    assert not any("DM delete" in call[2] for call in app.bot.of("send", FRIEND))
    await handlers.handle_message(app, tg(OWNER, OWNER, "/ownwork"))
    sent = app.bot.of("send", OWNER)[-1]
    del_btn = next(
        b for row in sent[3]["reply_markup"].inline_keyboard for b in row
        if b.callback_data.startswith(f"own:del:{done_dm['id']}:")
    )
    await press(app, del_btn.callback_data)
    yes_btn = app.bot.of("send", OWNER)[-1][3]["reply_markup"].inline_keyboard[0][0]
    # Stale token refused
    await press(app, f"own:delyes:{done_dm['id']}:0000000000000000")
    assert app.bot.of("answer")[-1][2] == STALE_MESSAGE
    assert await app.store.get_self_task(done_dm["id"]) is not None
    # Non-owner ignored
    await press(app, yes_btn.callback_data, FRIEND)
    assert await app.store.get_self_task(done_dm["id"]) is not None
    # Owner confirms
    await press(app, yes_btn.callback_data)
    assert await app.store.get_self_task(done_dm["id"]) is None
    assert any(f"Deleted self-directed task #{done_dm['id']}" in e["text"] for e in await events(app))


# ---- Approval cards (proposals) -----------------------------------------------------------


async def test_proposals_delete_settled_only_and_stale_refusal(make_app, browser):
    app = make_app()
    before = _archives(app.home.root)
    app.home._write_regular("SOUL.md", "Old line")
    pending_id = await app.proposals.create("soul_edit", "Pending edit", {"old": "Old line", "new": "New line"}, "reflection")
    settled_id = await app.proposals.create("memory_fact", "Likes tea", {"file": "USER.md", "text": "Likes tea"}, "chat")
    assert await app.proposals.decide(settled_id, False) == "Rejected"
    pending_row = await app.store.get_proposal(pending_id)
    settled_row = await app.store.get_proposal(settled_id)

    async with browser(app) as client:
        page = (await client.get("/proposals")).text
        assert "Settled (1)" in page and f'action="/proposals/{settled_id}/delete' in page and "hx-confirm=" in page
        assert "\u00b7" not in page
        assert (await client.post(f"/proposals/{settled_id}/delete")).status_code == 403

        # Pending refused
        refused = await client.post(
            f"/proposals/{pending_id}/delete", headers=POST_HEADERS, data={"expected": pending_row["created_at"]},
        )
        assert refused.status_code == 409 and "pending" in refused.text.lower()
        assert _new_archives(app.home.root, before) == []

        # Stale refused
        stale = await client.post(
            f"/proposals/{settled_id}/delete", headers=POST_HEADERS, data={"expected": "1999-01-01T00:00:00+00:00"},
        )
        assert stale.status_code == 409 and STALE_MESSAGE in stale.text

        # Settled deletes with archive and owner event
        deleted = await client.post(
            f"/proposals/{settled_id}/delete", headers=POST_HEADERS, data={"expected": settled_row["created_at"]},
        )
        assert deleted.status_code == 200 and f"Deleted proposal #{settled_id}." in deleted.text
        assert await app.store.get_proposal(settled_id) is None
        (archive,) = _new_archives(app.home.root, before)
        assert _manifest(archive)["rows"]["proposal"]["id"] == settled_id
        assert any(f"Deleted proposal #{settled_id}" in e["text"] for e in await events(app))


# ---- Failure injection & startup crash recovery ------------------------------------------


async def test_db_failure_rolls_back_moved_files_without_divergence(make_app, monkeypatch):
    app = make_app()
    before = _archives(app.home.root)
    await draft_plugin(app, manifest=MANIFEST, source="print('keep me')\n")
    row = await plugin_status(app)

    async def broken_delete_plugin(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.store, "delete_plugin", broken_delete_plugin)
    outcome = await app.plugins.delete("convert", row["hash"])
    assert outcome != "Deleted"
    # Files were moved right back, DB row is untouched, no pending archive left behind!
    assert (app.home.root / "plugins" / "convert" / "main.py").read_text() == "print('keep me')\n"
    assert len(await app.store.list_plugins()) == 1
    assert _new_archives(app.home.root, before) == []


async def test_startup_recovery_rolls_back_uncommitted_and_finishes_committed(make_app):
    app = make_app()
    # Case 1: Crash AFTER moving files in, BEFORE DB commit -> startup sweep restores files & removes folder.
    skill_dir = app.home.root / "skills" / "crashed"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: crashed\ndescription: \"Test\"\n---\nBody\n")
    uncommitted = stage_deletion(
        app.home.root, "skill", "crashed",
        event_text="Deleted skill crashed",
        event_at="2026-11-22T09:00:00.000001+00:00",
        rows={"skill": {"name": "crashed"}},
        sources=[skill_dir],
    )
    uncommitted.move_in()
    assert not skill_dir.exists()
    assert (uncommitted.folder / STATE_FILE).read_text().strip() == "pending"

    # Case 2: Crash AFTER DB commit (owner event present), BEFORE writing state=committed -> sweep finishes it.
    committed_event_at = "2026-11-22T09:00:01.000002+00:00"
    committed_event_text = "Deleted skill already-committed"
    with app.store._tx() as conn:
        app.store._insert_owner_event(conn, committed_event_text, committed_event_at)
    half_committed = stage_deletion(
        app.home.root, "skill", "already-committed",
        event_text=committed_event_text,
        event_at=committed_event_at,
        rows={"skill": {"name": "already-committed"}},
        sources=[],
    )
    assert (half_committed.folder / STATE_FILE).read_text().strip() == "pending"

    stats = await recover_deletions(app.home.root, app.store)
    assert stats["rolled_back"] >= 1 and stats["finished"] >= 1
    # Uncommitted skill is restored on disk and its pending folder is gone
    assert (skill_dir / "SKILL.md").is_file()
    assert not uncommitted.folder.exists()
    # Half-committed archive is now marked committed and kept
    assert half_committed.folder.is_dir()
    assert (half_committed.folder / STATE_FILE).read_text().strip() == "committed"


# ---- Pruning: older than 30 days or beyond 50 entries ------------------------------------


def test_prune_deletions_by_age_and_count_leaves_pending_alone(tmp_path):
    home = tmp_path / "instance"
    home.mkdir()
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

    # Create 55 recent committed archives + 2 old (>30 days) committed archives + 1 pending archive
    for i in range(55):
        stamp = now - timedelta(minutes=i)
        arc = stage_deletion(
            home, "reminder", i, event_text=f"Deleted reminder #{i}", event_at=stamp.isoformat(), rows={}, now=stamp,
        )
        arc.commit()
    for i in range(2):
        old_stamp = now - timedelta(days=31 + i)
        arc = stage_deletion(
            home, "reminder", f"old-{i}", event_text=f"Old {i}", event_at=old_stamp.isoformat(), rows={}, now=old_stamp,
        )
        arc.commit()
    pending = stage_deletion(
        home, "reminder", "live", event_text="Live", event_at=(now - timedelta(days=40)).isoformat(), rows={},
        now=now - timedelta(days=40),
    )

    pruned = prune_deletions(home, now=now)
    assert pruned == 7  # 2 old + 5 beyond the 50 newest
    remaining = list(deleted_root(home).iterdir())
    assert pending.folder in remaining
    committed_remaining = [p for p in remaining if (p / STATE_FILE).read_text().strip() == "committed"]
    assert len(committed_remaining) == 50
    # All old ones were pruned
    assert not any("old-" in p.name for p in committed_remaining)
