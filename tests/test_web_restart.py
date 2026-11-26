"""The dashboard's Restart: refused while in-memory work would be lost, otherwise an owner event, a graceful
SIGTERM and `dom run` exiting 75 so the service manager starts Dom again."""

import asyncio
import signal
from types import SimpleNamespace

import pytest

from dom import cli
from dom.web import api_system
from test_web_api_memory_chats import JSON
from tghelpers import OWNER


class Worker:
    def __init__(self, busy=False, handoff=False, budget=False):
        self._busy, self._handoff = busy, handoff
        self._budget_ask = ("task", 1, "token", None) if budget else None

    def busy(self):
        return self._busy

    def in_handoff(self):
        return self._handoff


def fleet(**state):
    async def close():
        pass

    return SimpleNamespace(lanes={"default": SimpleNamespace(worker=Worker(**state))}, close=close)


@pytest.fixture
def kills(monkeypatch):
    sent = []
    monkeypatch.setattr(api_system, "RESTART_DELAY", 0)
    monkeypatch.setattr(api_system.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


async def test_health_names_the_process_and_needs_the_dashboard_auth(make_app, browser):
    async with browser(make_app()) as client:
        assert (await client.get("/api/health", headers={"Authorization": ""})).status_code == 403
        data = (await client.get("/api/health")).json()
        assert data["ok"] is True and data["boot"] == api_system.BOOT_ID and data["version"]


@pytest.mark.parametrize(("state", "words"), [
    ({"busy": True}, "desktop task is running"),
    ({"busy": True, "handoff": True}, "log in"),
    ({"busy": True, "budget": True}, "more steps"),
])
async def test_a_restart_is_refused_while_a_desktop_task_or_its_card_is_active(make_app, browser, kills, state, words):
    app = make_app()
    app.desktop_worker = fleet(**state)
    async with browser(app) as client:
        response = await client.post("/api/system/restart", json={}, headers=JSON)
    assert response.status_code == 409 and words in response.json()["detail"]
    assert not app.restart_requested and kills == []


async def test_a_restart_is_refused_while_a_model_switch_or_t3_card_waits(make_app, browser, kills):
    app = make_app()
    app.dispatch.escalation.pending["token"] = object()
    async with browser(app) as client:
        response = await client.post("/api/system/restart", json={}, headers=JSON)
        assert response.status_code == 409 and "model switch" in response.json()["detail"]
        app.dispatch.escalation.pending.clear()
        app.t3 = SimpleNamespace(pending={"card": object()})
        response = await client.post("/api/system/restart", json={}, headers=JSON)
        assert response.status_code == 409 and "T3 Code" in response.json()["detail"]
    assert not app.restart_requested and kills == []


async def test_a_restart_records_an_event_and_stops_dom_gracefully_once(make_app, browser, kills):
    app = make_app()
    app.desktop_worker = fleet()  # an idle container does not block it
    async with browser(app) as client:
        assert (await client.post("/api/system/restart", json={"x": 1}, headers=JSON)).status_code == 422
        response = await client.post("/api/system/restart", json={}, headers=JSON)
        assert response.status_code == 200 and response.json()["boot"] == api_system.BOOT_ID
        await asyncio.sleep(0.01)
        again = await client.post("/api/system/restart", json={}, headers=JSON)
        assert again.json()["message"] == "Dom is already restarting."
        await asyncio.sleep(0.01)
    assert app.restart_requested
    assert len(kills) == 1 and kills[0][1] == signal.SIGTERM
    events = await app.store._run(lambda: app.store._conn.execute(
        "SELECT text FROM messages WHERE role = 'event' AND chat_id = ?", (str(OWNER),),
    ).fetchall())
    assert [row["text"] for row in events] == ["The owner restarted Dom from the dashboard."]


async def test_the_restart_route_needs_a_same_origin_json_write(make_app, browser, kills):
    async with browser(make_app()) as client:
        assert (await client.post("/api/system/restart", json={})).status_code == 403  # no Origin
        assert (await client.post("/api/system/restart", json={}, headers={"Origin": "https://evil.example.org"})).status_code == 403
    assert kills == []


@pytest.mark.parametrize(("restart", "code"), [(True, cli.RESTART_EXIT), (False, 0)])
def test_dom_run_exits_75_only_after_a_dashboard_restart(monkeypatch, tmp_path, restart, code):
    async def run(home):
        return restart

    monkeypatch.setattr(cli, "run", run)
    assert cli.main(["run", str(tmp_path)]) == code
    assert cli.RESTART_EXIT == 75
