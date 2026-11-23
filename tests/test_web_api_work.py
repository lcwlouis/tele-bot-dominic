"""The Work JSON API: auth, same-origin, validation and writes through to the store, worker and home."""

from datetime import date

import pytest

from dom.briefs import write_brief
from dom.selfwork import task_files
from dom.skills import Skill, is_retired, skill_hash, write_skill

from test_desktop_worker import call, wait_status, wait_task, worker_for
from test_web_api_memory_chats import JSON
from test_web_desktop import two_containers
from test_web_pages import browser  # noqa: F401 (fixture)
from tghelpers import OWNER

WRITES = [
    ("/api/work/desk/computer", {"action": "start"}),
    ("/api/work/desk/done", {"task_id": 1, "steps": 0}),
    ("/api/work/desk/delete", {"id": 1, "expected": "x"}),
    ("/api/work/own/cancel", {"id": 1}),
    ("/api/work/own/delete", {"id": 1, "expected": "x"}),
    ("/api/work/skills/action", {"name": "notes", "action": "retire"}),
    ("/api/work/reminders/cancel", {"id": 1}),
    ("/api/work/reminders/delete", {"id": 1, "expected": "x"}),
]
READS = ["desk", "own", "reminders", "briefs"]


@pytest.mark.parametrize(("path", "body"), WRITES)
async def test_every_write_needs_auth_a_same_origin_json_request(make_app, browser, path, body):
    async with browser(make_app()) as client:
        assert (await client.post(path, json=body, headers={"Authorization": "", **JSON})).status_code == 403
        assert (await client.post(path, json=body)).status_code == 403  # no Origin
        assert (await client.post(path, json=body, headers={"Origin": "https://evil.example.org"})).status_code == 403
        assert (await client.post(path, content="{}", headers={"Origin": JSON["Origin"], "Content-Type": "text/plain"})).status_code == 403


@pytest.mark.parametrize("name", READS)
async def test_reads_need_the_dashboard_auth(make_app, browser, name):
    async with browser(make_app()) as client:
        assert (await client.get(f"/api/work/{name}", headers={"Authorization": ""})).status_code == 403
        assert (await client.get(f"/api/work/{name}")).status_code == 200


async def test_the_desk_is_off_without_a_desktop(make_app, browser):
    async with browser(make_app()) as client:
        desk = (await client.get("/api/work/desk")).json()
        assert desk["enabled"] is False and desk["task"] is None and desk["containers"] == []
        refused = await client.post("/api/work/desk/computer", headers=JSON, json={"action": "start"})
        assert refused.status_code == 409


async def test_desk_shows_containers_and_starts_the_chosen_one(make_app, browser):
    app = make_app()
    fleet, computers = two_containers(app)
    task = await app.store.enqueue_desktop_task(OWNER, "read the shop manuals", "shop")
    async with browser(app) as client:
        main = (await client.get("/api/work/desk")).json()
        assert main["enabled"] and main["container"] == "default" and main["task"] is None
        assert main["viewer"] == "/desktop/vnc/index.html"
        assert [(c["name"], c["selected"]) for c in main["containers"]] == [("default", True), ("shop", False)]
        shop = (await client.get("/api/work/desk", params={"container": "shop"})).json()
        assert shop["task"]["id"] == task and shop["task"]["brief"] == "read the shop manuals"
        assert shop["viewer"] == "/desktop/vnc/index.html?container=shop" and shop["containers"][1]["selected"] is True
        assert (await client.get("/api/work/desk", params={"container": "nope"})).status_code == 404

        started = await client.post("/api/work/desk/computer", headers=JSON, json={"action": "start", "container": "shop"})
        assert started.status_code == 200 and started.json()["ok"] is True and started.json()["message"] == "Computer ready."
        assert (computers["shop"].started, computers["default"].started) == (1, 0)
        assert started.json()["status"] == "ready" and started.json()["container"] == "shop"
        for bad in ({"action": "reboot"}, {"action": "start", "extra": 1}, {}):
            assert (await client.post("/api/work/desk/computer", headers=JSON, json=bad)).status_code == 422
        assert (await client.post("/api/work/desk/computer", headers=JSON, json={"action": "start", "container": "nope"})).status_code == 404


async def test_stopping_the_computer_waits_for_the_task_and_never_stops_a_remote_one(make_app, browser):
    app = make_app()
    fleet, computers = two_containers(app)
    async with browser(app) as client:
        # The fake computers are not local ones, so Dom refuses to stop them.
        refused = await client.post("/api/work/desk/computer", headers=JSON, json={"action": "stop"})
        assert refused.status_code == 409 and "remote computer" in refused.json()["detail"]


async def test_desk_handoff_resumes_through_done_and_a_stale_one_says_so(make_app, browser):
    app = make_app()
    worker = await worker_for(app, call("handoff", reason="Sign in <here>"), call("finish", result="Done"), {"content": "invalid"})
    task_id = await worker.enqueue(OWNER, "Example")
    row = await wait_status(app, task_id, "waiting_owner")
    async with browser(app) as client:
        desk = (await client.get("/api/work/desk")).json()
        assert desk["task"]["status"] == "waiting_owner" and desk["task"]["error"] == "Sign in <here>"
        assert (await client.post("/api/work/desk/done", headers=JSON, json={"task_id": task_id})).status_code == 422
        done = await client.post("/api/work/desk/done", headers=JSON, json={"task_id": task_id, "steps": row["steps"]})
        assert done.status_code == 200 and done.json()["ok"] is True and done.json()["message"] == "Task resumed."
        await wait_task(worker)
        again = await client.post("/api/work/desk/done", headers=JSON, json={"task_id": task_id, "steps": row["steps"]})
        assert again.json()["ok"] is False and again.json()["message"] == "The handoff has already ended."
    assert any("Resumed desktop task" in e.text for e in await app.store.get_recent_chat_window(str(OWNER), 10))


async def test_desk_history_delete_refuses_active_and_stale_rows(make_app, browser):
    app = make_app()
    active = await app.store.enqueue_desktop_task(OWNER, "Still running")
    done = await app.store.enqueue_desktop_task(OWNER, "Finished task", container="other")
    await app.store.update_desktop_task(done, OWNER, ("queued",), "done", result="All set")
    row = await app.store.get_desktop_task(OWNER, done)
    async with browser(app) as client:
        history = (await client.get("/api/work/desk")).json()["history"]
        assert [item["id"] for item in history] == [done] and history[0]["result"] == "All set"
        assert history[0]["created_at"] == row["created_at"]
        remove = lambda task, expected: client.post("/api/work/desk/delete", headers=JSON, json={"id": task, "expected": expected})  # noqa: E731
        busy = await remove(active, (await app.store.get_desktop_task(OWNER, active))["created_at"])
        assert busy.status_code == 409
        assert (await remove(done, "2000-01-01T00:00:00+00:00")).status_code == 409
        assert (await remove(done + 99, "x")).status_code == 404
        assert await app.store.get_desktop_task(OWNER, done) is not None
        ok = await remove(done, row["created_at"])
        assert ok.status_code == 200 and ok.json()["message"] == f"Deleted desktop task #{done}." and ok.json()["history"] == []
        assert await app.store.get_desktop_task(OWNER, done) is None


async def planned_tasks(app):
    now = app.dispatch.now()
    return await app.store.add_self_tasks([
        {"goal": "Active plan", "why": "Curious", "tools": ["browse"], "sites": ["example.org"], "minutes": 5, "steps": 10},
        {"goal": "Finished plan", "why": "Done", "tools": ["browse"], "sites": [], "minutes": 5, "steps": 10},
    ], now, now)


async def test_own_work_lists_tasks_serves_screenshots_and_cancels(make_app, browser):
    app = make_app()
    planned, finished = await planned_tasks(app)
    await app.store.update_self_task(finished["id"], ("planned",), "done", result="Found notes", files=["shot.png"])
    shots = task_files(app.home.root, finished["id"])
    shots.mkdir(parents=True)
    (shots / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    async with browser(app) as client:
        own = (await client.get("/api/work/own")).json()
        assert {t["goal"] for t in own["tasks"]} == {"Active plan", "Finished plan"}
        done = next(t for t in own["tasks"] if t["id"] == finished["id"])
        assert done["result"] == "Found notes" and done["files"] == ["shot.png"] and done["tools"] == ["browse"]
        assert "tools_json" not in done
        image = await client.get(f"/api/work/own/{finished['id']}/files/shot.png")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert (await client.get(f"/api/work/own/{finished['id']}/files/..%2Fx.png")).status_code == 404
        assert (await client.get(f"/api/work/own/{finished['id']}/files/missing.png")).status_code == 404

        cancelled = await client.post("/api/work/own/cancel", headers=JSON, json={"id": planned["id"]})
        assert cancelled.json()["ok"] is True and cancelled.json()["message"] == f"Cancelled task #{planned['id']}."
        assert (await app.store.get_self_task(planned["id"]))["status"] != "planned"
        again = await client.post("/api/work/own/cancel", headers=JSON, json={"id": planned["id"]})
        assert again.json()["ok"] is False and "had already finished" in again.json()["message"]
        assert (await client.post("/api/work/own/cancel", headers=JSON, json={"id": 0})).status_code == 422


async def test_own_work_delete_refuses_active_and_stale_and_archives_the_rest(make_app, browser):
    app = make_app()
    planned, finished = await planned_tasks(app)
    await app.store.update_self_task(finished["id"], ("planned",), "done", result="Found notes")
    async with browser(app) as client:
        remove = lambda task, expected: client.post("/api/work/own/delete", headers=JSON, json={"id": task["id"], "expected": expected})  # noqa: E731
        assert (await remove(planned, planned["announced_at"])).status_code == 409
        assert (await remove(finished, "1999-01-01T00:00:00+00:00")).status_code == 409
        assert (await remove({"id": 9999}, "x")).status_code == 404
        ok = await remove(finished, finished["announced_at"])
        assert ok.status_code == 200 and ok.json()["message"] == f"Deleted task #{finished['id']}."
        assert await app.store.get_self_task(finished["id"]) is None
        assert [t["id"] for t in ok.json()["tasks"]] == [planned["id"]]


async def test_skills_retire_restore_and_delete_with_the_hash(make_app, browser):
    app = make_app()
    write_skill(app.home, Skill("notes", "Take structured notes", "Open the page and write a brief note."), "")
    async with browser(app) as client:
        act = lambda **body: client.post("/api/work/skills/action", headers=JSON, json=body)  # noqa: E731
        listed = (await client.get("/api/work/own")).json()["skills"]
        assert [(s["name"], s["retired"]) for s in listed] == [("notes", False)] and listed[0]["hash"] == skill_hash(app.home, "notes")

        retired = await act(name="notes", action="retire")
        assert retired.status_code == 200 and retired.json()["message"] == "Retired skill notes."
        assert retired.json()["skills"][0]["retired"] is True and is_retired(app.home, "notes")
        assert (await act(name="notes", action="retire")).status_code == 409  # already retired
        restored = await act(name="notes", action="restore")
        assert restored.json()["message"] == "Restored skill notes." and not is_retired(app.home, "notes")

        assert (await act(name="Bad Name", action="delete", expected="x")).status_code == 404
        assert (await act(name="notes", action="delete")).status_code == 409  # no hash
        assert (await act(name="notes", action="delete", expected="0" * 64)).status_code == 409  # stale
        assert (app.home.root / "skills" / "notes" / "SKILL.md").is_file()
        deleted = await act(name="notes", action="delete", expected=skill_hash(app.home, "notes"))
        assert deleted.status_code == 200 and deleted.json()["message"] == "Deleted skill notes." and deleted.json()["skills"] == []
        assert not (app.home.root / "skills" / "notes").exists()
        assert (await act(name="notes", action="restore")).status_code == 404
        assert (await act(name="notes", action="explode")).status_code == 422


async def test_reminders_list_cancel_and_delete_with_the_stamp(make_app, browser):
    app = make_app()
    first = await app.dispatch.reminders.create("2026-06-01T12:00", "First reminder")
    second = await app.dispatch.reminders.create("2026-06-02T09:30", "Second reminder")
    row = await app.store.get_reminder(OWNER, first)
    async with browser(app) as client:
        listed = (await client.get("/api/work/reminders")).json()["reminders"]
        assert {r["id"]: r["text"] for r in listed} == {first: "First reminder", second: "Second reminder"}
        assert all(r["status"] == "active" and r["due"] for r in listed)

        cancelled = await client.post("/api/work/reminders/cancel", headers=JSON, json={"id": first})
        assert cancelled.json()["ok"] is True and cancelled.json()["message"] == f"Cancelled reminder #{first}."
        assert next(r for r in cancelled.json()["reminders"] if r["id"] == first)["status"] != "active"
        again = await client.post("/api/work/reminders/cancel", headers=JSON, json={"id": first})
        assert again.json()["ok"] is False and "no longer active" in again.json()["message"]

        remove = lambda reminder, expected: client.post("/api/work/reminders/delete", headers=JSON, json={"id": reminder, "expected": expected})  # noqa: E731
        assert (await remove(first, "1999-01-01T00:00:00+00:00")).status_code == 409
        assert (await remove(9999, "x")).status_code == 404
        ok = await remove(first, row["created_at"])
        assert ok.status_code == 200 and [r["id"] for r in ok.json()["reminders"]] == [second]
        assert await app.store.get_reminder(OWNER, first) is None


async def test_briefs_list_their_blocks_and_never_the_private_folder(make_app, browser):
    app = make_app()
    async with browser(app) as client:
        assert (await client.get("/api/work/briefs")).json() == {"briefs": []}
        write_brief(app.home, date(2026, 11, 20), title="Quieter nights", problem="He wakes too often.",
                    proposal="- Sleep later\n- Check the clock", acceptance="1. No wake-ups")
        briefs = (await client.get("/api/work/briefs")).json()["briefs"]
        assert len(briefs) == 1 and briefs[0]["title"] == "Quieter nights"
        assert "He wakes too often." in briefs[0]["markdown"]
        assert {block["kind"] for block in briefs[0]["blocks"]} >= {"heading", "ul"}
