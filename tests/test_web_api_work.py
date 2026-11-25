"""The Work JSON API: auth, same-origin, validation and writes through to the store, worker and home."""

from datetime import date

import pytest

from dom.briefs import write_brief
from dom.selfwork import task_files
from dom.skills import Skill, is_retired, skill_hash, write_skill

from test_desktop_worker import call, wait_status, wait_task, worker_for
from test_web_api_memory_chats import JSON
from test_web_desktop import two_containers
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
    ("/api/work/reminders/bulk", {"action": "cancel", "items": [{"id": 1, "expected": "x"}]}),
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
        assert main["enabled"] and main["container"] == "default" and main["task"] is None and main["on_this_machine"] is False
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


async def test_a_stop_answers_at_once_and_the_dashboard_keeps_serving_while_docker_stops(make_app, browser, monkeypatch):
    import asyncio

    import dom.web.desktop as desktop_web
    from test_web_desktop import Computer

    class SlowStop(Computer):
        """A local computer whose `docker stop` takes as long as the test says."""

        def __init__(self, url):
            super().__init__(url)
            self.release = asyncio.Event()

        async def stop(self):
            await self.release.wait()
            self.state = "stopped"

    app = make_app()
    fleet, computers = two_containers(app)
    slow = computers["default"] = SlowStop("https://main.example.org")
    fleet.lanes.pop("default")
    lane = fleet.lane("default")
    monkeypatch.setattr(desktop_web, "LocalSandbox", SlowStop)
    async with browser(app) as client:
        stopped = await asyncio.wait_for(client.post("/api/work/desk/computer", headers=JSON, json={"action": "stop"}), 2)
        assert stopped.status_code == 200 and stopped.json()["ok"] is True
        assert stopped.json()["message"] == "Stopping the computer." and stopped.json()["status"] == "stopping"
        # Other pages are served while the container is still stopping, and a second stop or a start waits.
        assert (await asyncio.wait_for(client.get("/api/home"), 2)).status_code == 200
        assert (await client.get("/api/work/desk")).json()["status"] == "stopping"
        again = await client.post("/api/work/desk/computer", headers=JSON, json={"action": "start"})
        assert again.json()["message"] == "The computer is still stopping." and slow.started == 0
        slow.release.set()
        await lane.stopping
        assert (await client.get("/api/work/desk")).json()["status"] == "stopped"


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
        (app.home.root / "secret.txt").write_text("not for the browser")
        (shots / "link.png").symlink_to(app.home.root / "secret.txt")  # a link in the task folder is never followed
        assert (await client.get(f"/api/work/own/{finished['id']}/files/link.png")).status_code == 404
        assert (await client.get(f"/own-work/{finished['id']}/files/link.png")).status_code == 404

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


async def test_two_stops_at_once_start_one_background_stop_and_a_booting_computer_is_not_stopped(
    make_app, browser, monkeypatch, caplog,
):
    import asyncio

    import dom.web.desktop as desktop_web
    from test_web_desktop import Computer

    class SlowStop(Computer):
        def __init__(self, url, state="ready"):
            super().__init__(url, state)
            self.release = asyncio.Event()
            self.stops = 0

        async def stop(self):
            self.stops += 1
            await self.release.wait()
            raise RuntimeError("docker went away")

    app = make_app()
    fleet, computers = two_containers(app)
    slow = computers["default"] = SlowStop("https://main.example.org")
    fleet.lanes.pop("default")
    lane = fleet.lane("default")
    monkeypatch.setattr(desktop_web, "LocalSandbox", SlowStop)
    async with browser(app) as client:
        slow.state = "starting"
        booting = await client.post("/api/work/desk/computer", headers=JSON, json={"action": "stop"})
        assert booting.json()["message"] == "The computer is still starting." and lane.stopping is None
        slow.state = "ready"

        first, second = await asyncio.gather(*(
            client.post("/api/work/desk/computer", headers=JSON, json={"action": "stop"}) for _ in range(2)
        ))
        assert first.json()["message"] == second.json()["message"] == "Stopping the computer."
        claimed = lane.stopping
        await asyncio.sleep(0)
        assert lane.stopping is claimed and slow.stops == 1
        # An unexpected failure in the background stop is logged, not left for garbage collection.
        slow.release.set()
        await claimed
        assert "background desktop stop failed" in caplog.text


def test_brief_code_blocks_carry_their_fence_language_for_highlighting():
    from dom.briefs import markdown_blocks

    blocks = markdown_blocks("# Title\n\n## Change\n\n```Python extra\nprint('hi')\n```\n\n~~~\nplain\n~~~\n\nText")
    assert blocks == [
        ("heading", ["Change"], ""), ("code", ["print('hi')"], "python"), ("code", ["plain"], ""), ("p", ["Text"], ""),
    ]


async def test_reminders_bulk_cancel_and_delete_report_each_one_they_could_not_do(make_app, browser):
    app = make_app()
    ids = [await app.dispatch.reminders.create(f"2026-06-0{day}T12:00", f"Reminder {day}") for day in (1, 2, 3)]
    stamps = {i: (await app.store.get_reminder(OWNER, i))["created_at"] for i in ids}
    items = lambda *chosen: [{"id": i, "expected": stamps.get(i, "x")} for i in chosen]  # noqa: E731
    bulk = lambda action, chosen: client.post(  # noqa: E731
        "/api/work/reminders/bulk", headers=JSON, json={"action": action, "items": chosen},
    )
    async with browser(app) as client:
        for bad in ({"action": "snooze", "items": items(ids[0])}, {"action": "cancel", "items": []},
                    {"action": "cancel", "items": items(*range(1, 102))}, {"action": "cancel", "items": [{"id": 0, "expected": "x"}]},
                    {"action": "cancel", "items": items(ids[0]), "extra": 1}):
            assert (await client.post("/api/work/reminders/bulk", headers=JSON, json=bad)).status_code == 422, bad

        cancelled = (await bulk("cancel", items(ids[0], ids[1], ids[0]))).json()
        assert cancelled["ok"] is True and cancelled["message"] == "Cancelled 2 reminders."
        assert {r["id"]: r["status"] for r in cancelled["reminders"]}[ids[2]] == "active"
        assert {r["id"]: r["status"] == "active" for r in cancelled["reminders"]} == {ids[0]: False, ids[1]: False, ids[2]: True}

        again = (await bulk("cancel", items(ids[0], ids[2]))).json()
        assert again["ok"] is False
        assert again["message"] == f"Cancelled 1 of 2 reminders. Reminder #{ids[0]} is no longer active or has changed."

        stale = items(*ids)
        stale[1]["expected"] = "1999-01-01T00:00:00+00:00"
        deleted = (await bulk("delete", stale)).json()
        assert deleted["ok"] is False and deleted["message"].startswith("Deleted 2 of 3 reminders.")
        assert [r["id"] for r in deleted["reminders"]] == [ids[1]]  # the changed one stays
        assert await app.store.get_reminder(OWNER, ids[0]) is None and await app.store.get_reminder(OWNER, ids[2]) is None


async def test_reminders_bulk_cancel_never_cancels_a_new_reminder_that_reused_a_deleted_ones_id(make_app, browser):
    app = make_app()
    first = await app.dispatch.reminders.create("2026-06-01T12:00", "Water the plants")
    stale = (await app.store.get_reminder(OWNER, first))["created_at"]
    await app.store.delete_reminder(OWNER, first, stale, "Deleted in another tab", stale)
    app.dispatch.now.advance(minutes=1)
    reused = await app.dispatch.reminders.create("2026-06-02T12:00", "Call the plumber")
    assert reused > first  # AUTOINCREMENT never reuses the deleted reminder's id
    async with browser(app) as client:
        result = (await client.post(
            "/api/work/reminders/bulk", headers=JSON,
            json={"action": "cancel", "items": [{"id": reused, "expected": stale}]},
        )).json()
    assert result["ok"] is False and "has changed" in result["message"]
    assert (await app.store.get_reminder(OWNER, reused))["status"] == "active"
