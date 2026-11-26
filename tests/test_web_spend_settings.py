"""The owner's Spend view, the cap editor and the leak scan level."""

import json
import tomllib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from dom.config import Config, load_config
from dom.web.app import create_app
from dom.web.auth import Auth
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN
from tghelpers import FRIEND, OWNER

START = """\
# Keep the instance options and comments.
timezone = "UTC"
daily_spend_cap_usd = 1.0  # the normal pace
monthly_spend_cap_usd = 30.0
leak_threshold = 1.2

[models.chat]
base_url = "http://localhost:8000/v1"
model = "chat-model"

[custom]
stuff = 1
"""
NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def dashboard(make_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)
    path = tmp_path / "config.toml"
    path.write_text(START)
    app = make_app()
    app.config = load_config(tmp_path)
    app.dispatch.now.at = NOW
    web = create_app(app, Auth(Config(home=tmp_path, web={"dev_token_env": "DOM_TEST_TOKEN"})))

    def browser(*, authorized=True):
        headers = {"Authorization": f"Bearer {TOKEN}"} if authorized else {}
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN, headers=headers)

    return app, path, browser


def caps(revision, **update):
    return {
        "revision": revision, "daily_spend_cap_usd": 1.0, "monthly_spend_cap_usd": 30.0, "cap_owner_dm": False,
        "area_caps": {"chat": None, "media": None, "search": None, "desktop": None, "memory": None}, **update,
    }


async def test_routes_need_owner_auth_same_origin_and_json(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        for route in ("/api/spend", "/api/leak-scan"):
            assert (await client.get(route)).status_code == 403
        for route in ("/api/spend/caps", "/api/leak-scan"):
            assert (await client.post(route, json={}, headers={"Origin": ORIGIN})).status_code == 403
    async with browser() as client:
        for route in ("/api/spend/caps", "/api/leak-scan"):
            for headers in ({}, {"Origin": "https://other.example.test"}, {"HX-Request": "true"}):
                assert (await client.post(route, headers=headers, json={})).status_code == 403
            assert (await client.post(route, headers=POST_HEADERS, json={})).status_code == 422
            assert (await client.post(route, headers={"Origin": ORIGIN}, data={})).status_code == 403
        app.store.owner_id = None
        assert (await client.get("/api/spend")).status_code == 409
    assert path.read_text() == START


async def test_spend_view_shows_today_the_month_each_area_and_a_30_day_series(dashboard):
    app, _, browser = dashboard
    usage = app.dispatch.usage
    await usage.record("chat", "m", "h", {"cost": 0.25}, chat_id=OWNER)
    await usage.record("studio_voice", "m", "h", {"cost": 0.1})
    app.dispatch.now.at = NOW - timedelta(days=3)
    await usage.record("search", "m", "h", {"cost": 0.4})
    app.dispatch.now.at = NOW - timedelta(days=20)  # last month: in the series, not in this month
    await usage.record("desktop", "m", "h", {"cost": 2.0})
    app.dispatch.now.at = NOW
    async with browser() as client:
        view = (await client.get("/api/spend")).json()
    assert view["today"] == {"spent": 0.35, "cap": 1.0, "left": 0.65}
    assert view["month_label"] == "2026-01" and view["month_ends"] == "2026-02-01"
    assert view["month"] == {"spent": 0.75, "cap": 30.0, "left": 29.25}
    areas = {item["area"]: item for item in view["areas"]}
    assert list(areas) == ["chat", "media", "search", "desktop", "memory"]
    assert (areas["chat"]["spent"], areas["media"]["spent"], areas["search"]["spent"]) == (0.25, 0.1, 0.4)
    assert areas["desktop"]["spent"] == 0 and areas["chat"]["cap"] is None and areas["chat"]["label"]
    daily = view["daily"]
    assert len(daily) == 30 and daily[-1]["date"] == "2026-01-15" and daily[-1]["spent"] == 0.35
    assert daily[-4]["areas"]["search"] == 0.4 and daily[-21]["areas"]["desktop"] == 2.0
    assert view["cap_owner_dm"] is False and view["one_off_room"] == pytest.approx(29.25)
    assert set(view["help"]) == {"daily_spend_cap_usd", "monthly_spend_cap_usd", "cap_owner_dm", "area_caps"}


async def test_a_late_cost_shows_in_the_view_once_it_is_set(dashboard):
    app, _, browser = dashboard
    row = await app.dispatch.usage.record("studio_image", "m", "h", {"cost": 0})
    async with browser() as client:
        assert (await client.get("/api/spend")).json()["month"]["spent"] == 0
        await app.store.set_usage_cost(row, 0.07)
        assert (await client.get("/api/spend")).json()["month"]["spent"] == 0.07


async def test_saving_caps_writes_only_what_changed_and_applies_at_once(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get("/api/spend")).json()["revision"]
        saved = await client.post("/api/spend/caps", headers=POST_HEADERS, json=caps(
            revision, daily_spend_cap_usd=2.5, monthly_spend_cap_usd=40, cap_owner_dm=True,
            area_caps={"chat": None, "media": 5, "search": 0, "desktop": 3.5, "memory": None},
        ))
    assert saved.status_code == 200
    view = saved.json()
    assert view["today"]["cap"] == 2.5 and view["month"]["cap"] == 40 and view["cap_owner_dm"] is True
    assert {item["area"]: item["cap"] for item in view["areas"]} == {
        "chat": None, "media": 5, "search": None, "desktop": 3.5, "memory": None,
    }
    text = path.read_text()
    assert "# Keep the instance options and comments." in text and "[custom]" in text
    assert "daily_spend_cap_usd = 2.5  # the normal pace" in text  # the comment beside a value stays
    written = tomllib.loads(text)
    assert written["monthly_spend_cap_usd"] == 40 and written["cap_owner_dm"] is True
    assert written["area_spend_caps_usd"] == {"media": 5, "desktop": 3.5}
    assert (path.parent / "config.toml.bak").read_text() == START
    assert app.config.monthly_spend_cap_usd == 40 and app.config.cap_owner_dm
    assert app.config.area_spend_caps_usd == {"media": 5, "desktop": 3.5}
    # The new caps are in force now, with no restart.
    await app.dispatch.usage.record("chat", "m", "h", {"cost": 41})
    assert await app.dispatch.usage.over_cap(app.config, app.bot, sleep=lambda _: None)
    assert await app.dispatch.spend_paused(OWNER) and await app.dispatch.spend_paused(FRIEND)


async def test_saving_caps_again_removes_a_limit_that_was_cleared(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get("/api/spend")).json()["revision"]
        await client.post("/api/spend/caps", headers=POST_HEADERS, json=caps(revision, area_caps={
            "chat": 4, "media": None, "search": None, "desktop": None, "memory": None}))
        revision = (await client.get("/api/spend")).json()["revision"]
        await client.post("/api/spend/caps", headers=POST_HEADERS, json=caps(revision))
    assert "chat" not in tomllib.loads(path.read_text()).get("area_spend_caps_usd", {})
    assert app.config.area_spend_caps_usd == {}


@pytest.mark.parametrize(("update", "message"), [
    ({"daily_spend_cap_usd": -1}, None),
    ({"monthly_spend_cap_usd": 1e9}, None),
    ({"daily_spend_cap_usd": 5, "monthly_spend_cap_usd": 4}, "The monthly cap must be at least the daily cap."),
    ({"area_caps": {"chat": None}}, "Give every area a limit, or null for none."),
    ({"area_caps": {"chat": None, "media": None, "search": None, "desktop": None, "memory": -2}}, None),
    ({"area_caps": {"chat": None, "media": None, "search": None, "desktop": None, "memory": None, "voice": 1}}, None),
    ({"daily_spend_cap_usd": "2"}, None),
    ({"cap_owner_dm": "yes"}, None),
    ({"surprise": 1}, "Unexpected fields. Use only the documented settings fields."),
])
async def test_bad_caps_are_refused_and_nothing_is_written(dashboard, update, message):
    app, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get("/api/spend")).json()["revision"]
        refused = await client.post("/api/spend/caps", headers=POST_HEADERS, json=caps(revision, **update))
    assert refused.status_code == 422
    if message:
        assert refused.json()["detail"] == message
    assert path.read_text() == START and app.config.monthly_spend_cap_usd == 30.0


async def test_monthly_cap_may_stay_off_with_a_daily_cap(dashboard):
    _, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get("/api/spend")).json()["revision"]
        saved = await client.post("/api/spend/caps", headers=POST_HEADERS, json=caps(
            revision, daily_spend_cap_usd=5, monthly_spend_cap_usd=0))
    assert saved.status_code == 200 and saved.json()["month"]["cap"] is None and saved.json()["month"]["left"] is None
    assert tomllib.loads(path.read_text())["monthly_spend_cap_usd"] == 0


async def test_a_stale_revision_is_refused(dashboard):
    _, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get("/api/spend")).json()["revision"]
        path.write_text(START + "\n# edited by hand\n")
        stale = await client.post("/api/spend/caps", headers=POST_HEADERS, json=caps(revision))
    assert stale.status_code == 409 and "Reload" in stale.json()["detail"]
    assert path.read_text().endswith("# edited by hand\n")


async def test_leak_scan_view_maps_a_legacy_threshold(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        view = (await client.get("/api/leak-scan")).json()
        assert (view["level"], view["cutoff"]) == ("standard", 1.2)
        assert [level["id"] for level in view["levels"]] == ["strict", "standard", "relaxed", "off"]
        assert [level["cutoff"] for level in view["levels"]] == [0.7, 1.2, 2.0, None]
        assert "local check" in view["help"] and "always stays on" in view["help"]
        assert view["available"] is False  # no triage key in this fixture
        for threshold, level in ((0.65, "strict"), (1.25, "standard"), (1.95, "relaxed"), (1.6, "custom"), (0.0, "custom")):
            path.write_text(START.replace("leak_threshold = 1.2", f"leak_threshold = {threshold}"))
            view = (await client.get("/api/leak-scan")).json()
            assert (view["level"], view["cutoff"]) == (level, threshold)


@pytest.mark.parametrize(("level", "cutoff"), [("strict", 0.7), ("standard", 1.2), ("relaxed", 2.0), ("off", None)])
async def test_setting_the_leak_scan_applies_at_once_and_drops_the_legacy_threshold(dashboard, level, cutoff):
    app, path, browser = dashboard
    path.write_text(START.replace("leak_threshold = 1.2", "leak_threshold = 1.2\nleak_check = false"))
    async with browser() as client:
        revision = (await client.get("/api/leak-scan")).json()["revision"]
        assert (await client.get("/api/leak-scan")).json()["level"] == "off"  # the switch in What Dom may do is off
        saved = await client.post("/api/leak-scan", headers=POST_HEADERS, json={"revision": revision, "level": level})
    assert saved.status_code == 200 and saved.json()["level"] == level and saved.json()["cutoff"] == cutoff
    written = tomllib.loads(path.read_text())
    assert written["leak_scan"] == level and "leak_threshold" not in written
    assert "# Keep the instance options and comments." in path.read_text()
    assert app.config.leak_cutoff == cutoff and app.config.leak_level == level
    assert tomllib.loads(path.read_text())["leak_check"] is (level != "off")  # a level turns the check on; off leaves it
    assert app.config.leak_check is (level != "off")


async def test_leak_scan_refuses_unknown_levels_and_a_stale_page(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        revision = (await client.get("/api/leak-scan")).json()["revision"]
        for body in ({"revision": revision, "level": "paranoid"}, {"revision": revision}, {"level": "off"},
                     {"revision": revision, "level": "off", "threshold": 3}):
            assert (await client.post("/api/leak-scan", headers=POST_HEADERS, json=body)).status_code == 422
        path.write_text(START.replace("1.2", "1.3"))
        stale = await client.post("/api/leak-scan", headers=POST_HEADERS, json={"revision": revision, "level": "off"})
    assert stale.status_code == 409
    assert app.config.leak_level != "off"
