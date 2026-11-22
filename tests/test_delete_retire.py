"""W7 (E40): owner-only retire and delete for skills, plugins, reminders, tasks and proposals."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from dom import backup as backup_mod
from dom.prompt import build_messages
from dom.selfwork import FILES_DIR, task_files
from dom.skills import Skill, is_retired, prompt_block, skill_view, skills_list, write_skill
from dom.telegram import handlers
from dom.telegram.owner import OWNER_COMMANDS
from dom.tools import SKILL_VIEW_TOOL, SKILLS_LIST_TOOL, TurnCtx
from test_owner_events import events, press
from test_plugin_surfaces import SURFACES, approve as approve_surface_plugin
from test_plugin_tools import MANIFEST, draft as draft_plugin, review_code, review_path, status as plugin_status
from test_web_auth import POST_HEADERS
from test_web_pages import browser  # noqa: F401
from tghelpers import FRIEND, OWNER, tg

BODY = "Open the page, check the table, and write a brief note."


def _backups(home: Path) -> set[Path]:
    folder = home.resolve().parent / "backups"
    return set(folder.glob("dom-*.tar.gz")) if folder.is_dir() else set()


def _new_backups(home: Path, before: set[Path]) -> list[Path]:
    return sorted(_backups(home) - before)


def _archive_members(archive: Path) -> set[str]:
    with tarfile.open(archive) as tar:
        return {member.name for member in tar}


# ---- Skills -------------------------------------------------------------------------------


async def test_skill_retire_restore_and_delete_with_backup_and_events(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    write_skill(app.home, Skill("notes", "Take structured notes", BODY), "")
    ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, app.store, OWNER, app=app)

    async with browser(app) as client:
        # Memory page renders the skills section with confirm prompts
        page = (await client.get("/memory")).text
        assert 'id="skills"' in page and "notes" in page and "hx-confirm=" in page
        # CSRF check
        assert (await client.post("/skills/notes/retire")).status_code == 403
        assert (await client.post("/skills/notes/delete")).status_code == 403
        assert (await client.post("/skills/Bad Name/delete", headers=POST_HEADERS)).status_code == 404

        # Retire: files stay on disk, Dom no longer lists or uses it
        retired = await client.post("/skills/notes/retire", headers=POST_HEADERS)
        assert retired.status_code == 200 and "Retired skill notes." in retired.text
        assert (app.home.root / "skills" / "notes" / "SKILL.md").is_file()
        assert is_retired(app.home, "notes")
        assert skills_list(app.home) == []
        assert skill_view(app.home, "notes") is None
        assert prompt_block(app.home) == ""
        assert "<skills>" not in build_messages(app.home, "owner_dm", OWNER, [], "<chat_window/>", "note")[0]["content"]
        assert json.loads(await SKILLS_LIST_TOOL.handler({}, ctx)) == []
        assert await SKILL_VIEW_TOOL.handler({"name": "notes"}, ctx) == "Skill not found."
        assert _new_backups(app.home.root, before) == []  # retiring does not delete anything
        assert any("Retired skill notes" in row["text"] for row in await events(app))

        # Restore: Dom sees and uses it again
        restored = await client.post("/skills/notes/restore", headers=POST_HEADERS)
        assert restored.status_code == 200 and "Restored skill notes." in restored.text
        assert not is_retired(app.home, "notes")
        assert [row["name"] for row in skills_list(app.home)] == ["notes"]
        assert skill_view(app.home, "notes") == BODY
        assert any("Restored skill notes" in row["text"] for row in await events(app))

        # Delete: backup taken first, files removed, event recorded
        deleted = await client.post("/skills/notes/delete", headers=POST_HEADERS)
        assert deleted.status_code == 200 and "Deleted skill notes." in deleted.text
        assert not (app.home.root / "skills" / "notes").exists()
        archives = _new_backups(app.home.root, before)
        assert len(archives) == 1
        assert "skills/notes/SKILL.md" in _archive_members(archives[0])
        assert any("Deleted skill notes" in row["text"] for row in await events(app))
        # Deleting again returns 404
        assert (await client.post("/skills/notes/delete", headers=POST_HEADERS)).status_code == 404


async def test_skill_delete_aborts_if_backup_fails(make_app, browser, monkeypatch):
    app = make_app()
    write_skill(app.home, Skill("notes", "Take structured notes", BODY), "")

    def broken_backup(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup_mod, "backup", broken_backup)
    async with browser(app) as client:
        response = await client.post("/skills/notes/delete", headers=POST_HEADERS)
    assert response.status_code == 409 and "backup" in response.text.lower()
    assert (app.home.root / "skills" / "notes" / "SKILL.md").is_file()
    assert not any("Deleted skill" in row["text"] for row in await events(app))


# ---- Plugins ------------------------------------------------------------------------------


async def test_plugin_retire_and_delete_dashboard_and_menu_refresh(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    await approve_surface_plugin(app, browser)
    row = await plugin_status(app)
    route = review_path(row)

    # Approved: command is in menu and panel is visible
    commands_before = app.bot.of("set_commands")[-1][1]
    assert any(cmd.command == "weather" for cmd in commands_before)

    async with browser(app) as client:
        page = (await client.get("/plugins")).text
        assert "hx-confirm=" in page and 'hx-post="/plugins/convert/delete"' in page
        assert "/plugin-panels/convert/summary" in page
        # CSRF check
        assert (await client.post("/plugins/convert/delete")).status_code == 403

        # Retire reuses the disable path: files stay, command & panel disappear
        retired = await client.post(f"{route}/retire", headers=POST_HEADERS)
        assert retired.status_code == 200 and "Disabled" in retired.text
        assert (app.home.root / "plugins" / "convert" / "main.py").is_file()
        assert (await plugin_status(app))["status"] == "disabled"
        assert "/plugin-panels/convert/summary" not in (await client.get("/plugins")).text
        assert app.bot.of("set_commands")[-1][1] == OWNER_COMMANDS
        assert any("Disabled plugin convert" in e["text"] for e in await events(app))

        # Seed stored state to confirm deletion clears every piece of plugin state
        await app.store.kv_set("plugin-storage:convert", '{"count": 1}')

        # Mid-call refusal: cannot delete while plugin is running (and no backup is taken)
        with app.plugin_runner.active("convert"):
            busy = await client.post("/plugins/convert/delete", headers=POST_HEADERS)
            assert busy.status_code == 409 and "running" in busy.text.lower()
        assert _new_backups(app.home.root, before) == []
        assert (app.home.root / "plugins" / "convert" / "main.py").is_file()

        # Delete when idle: backup taken first, files + DB + KV removed, menu refreshed, event recorded
        app.bot.calls.clear()
        deleted = await client.post("/plugins/convert/delete", headers=POST_HEADERS)
        assert deleted.status_code == 200 and "Deleted" in deleted.text
        assert not (app.home.root / "plugins" / "convert").exists()
        assert await app.store.list_plugins() == []
        assert await app.store.kv_get("plugin-files:convert") is None
        assert await app.store.kv_get(f"plugin-reviewed:convert:{row['hash']}") is None
        assert await app.store.kv_get("plugin-storage:convert") is None
        assert app.bot.of("set_commands")[-1][1] == OWNER_COMMANDS
        archives = _new_backups(app.home.root, before)
        assert len(archives) == 1
        members = _archive_members(archives[0])
        assert "plugins/convert/main.py" in members and "state.db" in members
        assert any("Deleted plugin convert" in e["text"] for e in await events(app))


async def test_plugin_delete_from_owner_dm_confirms_and_settles_pending_proposal(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    proposal_id = await draft_plugin(app, manifest=SURFACES)
    card_id = app.bot._next_id
    app.bot.calls.clear()

    await handlers.handle_message(app, tg(OWNER, OWNER, "/plugins"))
    sent, = app.bot.of("send", OWNER)
    buttons = sent[3]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["plugin:disable:convert", "plugin:del:convert"]

    # Non-owner is ignored
    await press(app, "plugin:delyes:convert", FRIEND)
    assert len(await app.store.list_plugins()) == 1

    # Owner taps Delete -> confirmation card -> Cancel leaves it intact
    await press(app, "plugin:del:convert")
    confirm = app.bot.of("send", OWNER)[-1]
    assert "Delete plugin convert" in confirm[2]
    await press(app, "plugin:delno:convert")
    assert len(await app.store.list_plugins()) == 1

    # Confirm delete -> removes plugin, settles pending proposal card, takes backup
    await press(app, "plugin:delyes:convert")
    assert await app.store.list_plugins() == []
    assert await app.proposals.pending() == []
    assert any(call[2] == card_id for call in app.bot.of("markup"))
    assert len(_new_backups(app.home.root, before)) == 1
    assert any("Deleted plugin convert" in e["text"] for e in await events(app))

    # Deleting a plugin whose files were already removed manually still cleans the DB row
    await draft_plugin(app, manifest=MANIFEST)
    for path in list((app.home.root / "plugins" / "convert").rglob("*"))[::-1]:
        path.unlink() if path.is_file() else path.rmdir()
    (app.home.root / "plugins" / "convert").rmdir()
    assert await app.plugins.delete("convert") == "Deleted"
    assert await app.store.list_plugins() == []


# ---- Reminders ----------------------------------------------------------------------------


async def test_reminder_cancel_delete_in_use_and_dm_confirm(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    r1 = await app.dispatch.reminders.create("2026-06-01T12:00", "Take umbrella")
    r2 = await app.dispatch.reminders.create("2026-06-02T12:00", "Water plants")

    async with browser(app) as client:
        page = (await client.get("/reminders")).text
        assert "Take umbrella" in page and "hx-confirm=" in page
        assert (await client.post(f"/reminders/{r1}/delete")).status_code == 403

        # Refuses while mid-delivery
        app.dispatch.reminders._in_flight.add(r1)
        try:
            busy = await client.post(f"/reminders/{r1}/delete", headers=POST_HEADERS)
            assert busy.status_code == 409 and "delivered" in busy.text.lower()
            assert _new_backups(app.home.root, before) == []
        finally:
            app.dispatch.reminders._in_flight.discard(r1)

        # Cancel then delete r1 via dashboard
        cancelled = await client.post(f"/reminders/{r1}/cancel", headers=POST_HEADERS)
        assert cancelled.status_code == 200 and f"Cancelled reminder #{r1}." in cancelled.text
        deleted = await client.post(f"/reminders/{r1}/delete", headers=POST_HEADERS)
        assert deleted.status_code == 200 and f"Deleted reminder #{r1}." in deleted.text
        assert await app.store.get_reminder(OWNER, r1) is None
        assert len(_new_backups(app.home.root, before)) == 1
        assert any(f"Deleted reminder #{r1}" in e["text"] for e in await events(app))

    # Delete r2 via owner DM confirm flow (and reject non-owner)
    before2 = _backups(app.home.root)
    await press(app, f"rem:delyes:{r2}", FRIEND)
    assert await app.store.get_reminder(OWNER, r2) is not None
    await handlers.handle_message(app, tg(OWNER, OWNER, f"/reminders delete {r2}"))
    assert f"Delete reminder #{r2}" in app.bot.of("send", OWNER)[-1][2]
    await press(app, f"rem:delyes:{r2}")
    assert await app.store.get_reminder(OWNER, r2) is None
    assert len(_new_backups(app.home.root, before2)) == 1
    assert any(f"Deleted reminder #{r2}" in e["text"] for e in await events(app))


# ---- Desktop task history -----------------------------------------------------------------


async def test_desktop_task_history_delete_refuses_active_and_cleans_files(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    active_id = await app.store.enqueue_desktop_task(OWNER, "Still running")
    done_id = await app.store.enqueue_desktop_task(OWNER, "Finished task", container="other")
    await app.store.update_desktop_task(done_id, OWNER, ("queued",), "done", result="All set")
    # Put a leftover inbox folder for done_id to verify file cleanup
    inbox = app.home.root.resolve().parent / "desktop-inbox" / f"task-{done_id}"
    inbox.mkdir(parents=True)
    (inbox / "report.txt").write_text("leftover")

    async with browser(app) as client:
        page = (await client.get("/desktop")).text
        assert "Finished task" in page and "hx-confirm=" in page
        assert (await client.post(f"/desktop/tasks/{done_id}/delete")).status_code == 403

        # Active task cannot be deleted (and no backup taken)
        refused = await client.post(f"/desktop/tasks/{active_id}/delete", headers=POST_HEADERS)
        assert refused.status_code == 409 and "queued" in refused.text.lower()
        assert _new_backups(app.home.root, before) == []
        assert await app.store.get_desktop_task(OWNER, active_id) is not None

        # Finished task deletes with backup, cleans bound files, records owner event
        deleted = await client.post(f"/desktop/tasks/{done_id}/delete", headers=POST_HEADERS)
        assert deleted.status_code == 200 and f"Deleted desktop task #{done_id}." in deleted.text
        assert await app.store.get_desktop_task(OWNER, done_id) is None
        assert not inbox.exists()
        assert len(_new_backups(app.home.root, before)) == 1
        assert any(f"Deleted desktop task #{done_id}" in e["text"] for e in await events(app))

    # Finish active_id and delete via /tasks DM confirmation
    before2 = _backups(app.home.root)
    await app.store.update_desktop_task(active_id, OWNER, ("queued",), "cancelled")
    await handlers.handle_message(app, tg(OWNER, OWNER, "/tasks"))
    sent = app.bot.of("send", OWNER)[-1]
    assert sent[3]["reply_markup"].inline_keyboard[0][0].callback_data == f"desktop:del:{active_id}"
    await press(app, f"desktop:delyes:{active_id}", FRIEND)
    assert await app.store.get_desktop_task(OWNER, active_id) is not None
    await press(app, f"desktop:del:{active_id}")
    await press(app, f"desktop:delyes:{active_id}")
    assert await app.store.get_desktop_task(OWNER, active_id) is None
    assert len(_new_backups(app.home.root, before2)) == 1


# ---- Self-directed task history -----------------------------------------------------------


async def test_self_work_task_delete_refuses_active_and_removes_screenshots(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    now = app.dispatch.now()
    planned, done = await app.store.add_self_tasks([
        {
            "goal": "Active plan", "why": "Curious", "tools": ["browse"], "sites": ["example.org"],
            "minutes": 5, "steps": 10,
        },
        {
            "goal": "Finished plan", "why": "Done", "tools": ["browse"], "sites": ["example.org"],
            "minutes": 5, "steps": 10,
        },
    ], now, now)
    await app.store.update_self_task(done["id"], ("planned",), "done", result="Found notes", files=["shot.png"])
    shots = task_files(app.home.root, done["id"])
    shots.mkdir(parents=True)
    (shots / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    async with browser(app) as client:
        page = (await client.get("/own-work")).text
        assert f'/own-work/{done["id"]}/delete' in page and "hx-confirm=" in page
        assert f'/own-work/{planned["id"]}/delete' not in page
        assert (await client.post(f"/own-work/{done['id']}/delete")).status_code == 403

        # Planned/running task refused without backup
        refused = await client.post(f"/own-work/{planned['id']}/delete", headers=POST_HEADERS)
        assert refused.status_code == 409 and "planned" in refused.text.lower()
        assert _new_backups(app.home.root, before) == []

        # Finished task deleted with backup + screenshots removed + owner event
        deleted = await client.post(f"/own-work/{done['id']}/delete", headers=POST_HEADERS)
        assert deleted.status_code == 200 and f"Deleted task #{done['id']}." in deleted.text
        assert await app.store.get_self_task(done["id"]) is None
        assert not shots.exists()
        archives = _new_backups(app.home.root, before)
        assert len(archives) == 1
        assert f"{FILES_DIR}/{done['id']}/shot.png" in _archive_members(archives[0])
        assert any(f"Deleted self-directed task #{done['id']}" in e["text"] for e in await events(app))


# ---- Approval cards (proposals) -----------------------------------------------------------


async def test_proposals_delete_settled_only_via_dashboard_and_dm(make_app, browser):
    app = make_app()
    before = _backups(app.home.root)
    app.home._write_regular("SOUL.md", "Old line")
    pending_id = await app.proposals.create("soul_edit", "Pending edit", {"old": "Old line", "new": "New line"}, "reflection")
    settled_id = await app.proposals.create("memory_fact", "Likes tea", {"file": "USER.md", "text": "Likes tea"}, "chat")
    settled_dm_id = await app.proposals.create("memory_fact", "Likes coffee", {"file": "USER.md", "text": "Likes coffee"}, "chat")
    assert await app.proposals.decide(settled_id, False) == "Rejected"
    assert await app.proposals.decide(settled_dm_id, False) == "Rejected"

    async with browser(app) as client:
        page = (await client.get("/proposals")).text
        assert "Settled (2)" in page and f"/proposals/{settled_id}/delete" in page and "hx-confirm=" in page
        assert "\u00b7" not in page
        assert (await client.post(f"/proposals/{settled_id}/delete")).status_code == 403

        # Pending proposal cannot be deleted (and no backup taken)
        refused = await client.post(f"/proposals/{pending_id}/delete", headers=POST_HEADERS)
        assert refused.status_code == 409 and "pending" in refused.text.lower()
        assert _new_backups(app.home.root, before) == []
        assert await app.store.get_proposal(pending_id) is not None

        # Settled proposal deletes with backup and owner event
        deleted = await client.post(f"/proposals/{settled_id}/delete", headers=POST_HEADERS)
        assert deleted.status_code == 200 and f"Deleted proposal #{settled_id}." in deleted.text
        assert await app.store.get_proposal(settled_id) is None
        assert len(_new_backups(app.home.root, before)) == 1
        assert any(f"Deleted proposal #{settled_id}" in e["text"] for e in await events(app))

    # Delete settled_dm_id via /proposals settled + confirmation callback
    before2 = _backups(app.home.root)
    await handlers.handle_message(app, tg(OWNER, OWNER, "/proposals settled"))
    sent = app.bot.of("send", OWNER)[-1]
    assert sent[3]["reply_markup"].inline_keyboard[0][0].callback_data == f"prop:del:{settled_dm_id}"
    await press(app, f"prop:delyes:{settled_dm_id}", FRIEND)
    assert await app.store.get_proposal(settled_dm_id) is not None
    await press(app, f"prop:del:{settled_dm_id}")
    await press(app, f"prop:delyes:{settled_dm_id}")
    assert await app.store.get_proposal(settled_dm_id) is None
    assert len(_new_backups(app.home.root, before2)) == 1
    assert any(f"Deleted proposal #{settled_dm_id}" in e["text"] for e in await events(app))

    # /proposals delete on pending refuses without sending a confirm card
    app.bot.calls.clear()
    await handlers.handle_message(app, tg(OWNER, OWNER, f"/proposals delete {pending_id}"))
    assert "pending" in app.bot.of("send", OWNER)[-1][2].lower()
    assert await app.store.get_proposal(pending_id) is not None


async def test_backup_failure_aborts_delete_for_all_kinds(make_app, browser, monkeypatch):
    app = make_app()
    await draft_plugin(app, manifest=MANIFEST)
    rem_id = await app.dispatch.reminders.create("2026-06-01T12:00", "Keep me")
    desk_id = await app.store.enqueue_desktop_task(OWNER, "Keep task")
    await app.store.update_desktop_task(desk_id, OWNER, ("queued",), "done", result="ok")
    now = app.dispatch.now()
    (own_row,) = await app.store.add_self_tasks([{
        "goal": "Keep self", "why": "Test", "tools": ["browse"], "sites": ["example.org"], "minutes": 5, "steps": 10,
    }], now, now)
    await app.store.update_self_task(own_row["id"], ("planned",), "done", result="ok")
    prop_id = await app.proposals.create("memory_fact", "Keep prop", {"file": "USER.md", "text": "Keep prop"}, "chat")
    assert await app.proposals.decide(prop_id, False) == "Rejected"

    def broken_backup(*args, **kwargs):
        raise OSError("no space left")

    monkeypatch.setattr(backup_mod, "backup", broken_backup)
    async with browser(app) as client:
        for path in (
            "/plugins/convert/delete",
            f"/reminders/{rem_id}/delete",
            f"/desktop/tasks/{desk_id}/delete",
            f"/own-work/{own_row['id']}/delete",
            f"/proposals/{prop_id}/delete",
        ):
            response = await client.post(path, headers=POST_HEADERS)
            assert response.status_code == 409 and "backup" in response.text.lower(), path
    assert len(await app.store.list_plugins()) == 1
    assert await app.store.get_reminder(OWNER, rem_id) is not None
    assert await app.store.get_desktop_task(OWNER, desk_id) is not None
    assert await app.store.get_self_task(own_row["id"]) is not None
    assert await app.store.get_proposal(prop_id) is not None
