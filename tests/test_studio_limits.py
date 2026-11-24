"""The media limits editor (/api/studio/limits) and where a chat stands against its limits."""

import json
import tomllib
from datetime import datetime, timedelta, timezone

from dom.config import OwnerStudioTier, StudioConfig, StudioLimits
from dom.models import StoredMessage
from dom.studio.policy import standing
from test_web_model_settings import ORIGIN_HEADER, START, dashboard  # noqa: F401 (fixture)
from tghelpers import OWNER

LIMITS = "/api/studio/limits"
NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
STUDIO = """
[studio] # the studio
voice = true

[studio.limits]
daily = 12 # all chats

[studio.limits.owner_dm]
gap_minutes = 30 # between media
"""


def test_standing_counts_each_window_the_global_total_and_the_gap():
    limits = StudioLimits()  # owner DM: voice 3/day, video 1/day, images 2/day, special 1/day, 30-minute gap
    rows = [("photo", NOW - timedelta(minutes=10)), ("voice", NOW - timedelta(hours=3)),
            ("voice", NOW - timedelta(days=2))]  # outside the voice window
    now = standing(limits, "owner_dm", 3, rows, NOW)
    assert now.left == {"voice": 2, "video_note": 1, "photo": 1, "special": 1}
    assert now.daily_left == 9
    assert now.next_at == NOW + timedelta(minutes=20)  # the photo ten minutes ago, plus the gap

    later = standing(limits, "owner_dm", 3, rows, NOW + timedelta(minutes=25))
    assert later.next_at is None
    # The global total caps every kind, and never below zero.
    assert standing(limits, "owner_dm", 11, rows, NOW).left == {"voice": 1, "video_note": 1, "photo": 1, "special": 1}
    spent = standing(limits, "owner_dm", 20, [], NOW)
    assert spent.daily_left == 0 and set(spent.left.values()) == {0} and spent.next_at is None


def test_standing_uses_the_weekly_window_when_the_daily_limit_is_unset():
    limits = StudioLimits(owner_dm=OwnerStudioTier(image_daily=None, image_weekly=3))
    rows = [("photo", NOW - timedelta(days=2)), ("photo", NOW - timedelta(days=8))]
    assert standing(limits, "owner_dm", 0, rows, NOW).left["photo"] == 2
    assert standing(StudioLimits(), "owner_dm", 0, rows, NOW).left["photo"] == 2  # daily: the old photos fell out


def test_false_unsets_a_daily_default():
    assert OwnerStudioTier(video_daily=False).video_daily is None
    assert OwnerStudioTier().video_daily == 1


async def _deliver(app, kind, at):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(OWNER), role="assistant", visibility_scope=f"chat:{OWNER}", text="",
        media_json=json.dumps({"kind": kind}), created_at=at.isoformat(),
    ))


async def test_read_shows_the_saved_limits_and_what_is_left_in_the_owner_dm(dashboard):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    app.config = app.config.model_copy(update={"studio": StudioConfig(
        enabled=True, base_url="https://studio.example.org", voice=True, images=True)})
    now = app.dispatch.now()
    await _deliver(app, "photo", now - timedelta(minutes=5))
    before = await app.studio_quota.remaining(app.config.studio, "owner_dm", OWNER, now)
    async with browser() as client:
        response = await client.get(LIMITS)
    assert response.status_code == 200
    body = response.json()
    assert body["limits"]["owner_dm"]["gap_minutes"] == 30 and body["limits"]["group"]["video_daily"] is None
    kinds = {item["kind"]: item for item in body["owner"]["kinds"]}
    assert kinds["photo"] == {"kind": "photo", "left": 1, "on": True}
    assert kinds["video_note"]["on"] is False  # its switch is off
    assert body["owner"]["daily_left"] == 11
    assert datetime.fromisoformat(body["owner"]["next_at"]) == now + timedelta(minutes=25)
    # Reading reserves nothing.
    assert await app.studio_quota.remaining(app.config.studio, "owner_dm", OWNER, now) == before


async def _save(client, change):
    current = (await client.get(LIMITS)).json()
    limits = current["limits"]
    change(limits)
    return await client.post(LIMITS, headers=ORIGIN_HEADER, json={"revision": current["revision"], "limits": limits})


async def test_save_writes_only_changes_keeps_comments_and_applies_at_once(dashboard):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    original = path.read_text()

    def change(limits):
        limits["owner_dm"]["gap_minutes"] = 5
        limits["owner_dm"]["image_daily"] = None  # per week
        limits["owner_dm"]["image_weekly"] = 4
        limits["friend_dm"]["voice_daily"] = 2
        limits["group"]["video_daily"] = 1  # per day
        limits["daily"] = 20

    async with browser() as client:
        response = await _save(client, change)
    assert response.status_code == 200, response.text
    text = path.read_text()
    assert "# Keep this comment." in text and "# the studio" in text
    assert "gap_minutes = 5 # between media" in text and "daily = 20 # all chats" in text
    assert "image_daily = false" in text  # the owner's daily default is set, so false unsets it
    saved = tomllib.loads(text)["studio"]["limits"]
    assert saved["friend_dm"] == {"voice_daily": 2} and saved["group"] == {"video_daily": 1}
    assert (path.parent / "config.toml.bak").read_text() == original
    limits = app.config.studio.limits  # the running config, without a restart
    assert limits.owner_dm.gap_minutes == 5 and limits.owner_dm.image_daily is None and limits.daily == 20
    assert response.json()["limits"]["owner_dm"]["image_weekly"] == 4

    # Back to per day: the daily value replaces false.
    async with browser() as client:
        response = await _save(client, lambda limits: limits["owner_dm"].update(image_daily=3))
    assert response.status_code == 200
    assert "image_daily = 3" in path.read_text() and app.config.studio.limits.owner_dm.image_daily == 3


async def test_save_creates_the_tables_when_config_has_none(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        response = await _save(client, lambda limits: limits["owner_dm"].update(gap_minutes=0))
    assert response.status_code == 200
    assert tomllib.loads(path.read_text())["studio"]["limits"]["owner_dm"] == {"gap_minutes": 0}
    assert app.config.studio.limits.owner_dm.gap_minutes == 0


async def test_save_validates_with_the_config_bounds_and_keeps_everything_on_failure(dashboard):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    original, running = path.read_text(), app.config
    bad = [
        lambda limits: limits["owner_dm"].update(gap_minutes=10081),
        lambda limits: limits["owner_dm"].update(gap_minutes=-1),
        lambda limits: limits["friend_dm"].update(voice_daily=-1),
        lambda limits: limits.update(daily=-1),
        lambda limits: limits["group"].update(voice_daily="3"),  # strict: no strings
        lambda limits: limits["group"].update(voice_daily=1.5),
        lambda limits: limits["group"].update(extra=1),
        lambda limits: limits.pop("daily"),
    ]
    async with browser() as client:
        for change in bad:
            assert (await _save(client, change)).status_code == 422
        stale = await client.post(LIMITS, headers=ORIGIN_HEADER, json={
            "revision": "0" * 64, "limits": (await client.get(LIMITS)).json()["limits"]})
        assert stale.status_code == 409
    assert path.read_text() == original and app.config is running
    assert not (path.parent / "config.toml.bak").exists()


async def test_a_failed_write_keeps_the_old_file_and_runtime(dashboard, monkeypatch):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    original, running = path.read_text(), app.config

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dom.web.model_settings._replace_file", fail)
    async with browser() as client:
        response = await _save(client, lambda limits: limits["owner_dm"].update(gap_minutes=5))
    assert response.status_code == 409 and "media limits" in response.json()["detail"]
    assert path.read_text() == original and app.config is running


async def test_limits_need_the_owner_and_a_same_origin_post(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        assert (await client.get(LIMITS)).status_code == 403
        assert (await client.post(LIMITS, headers=ORIGIN_HEADER, json={})).status_code == 403
    async with browser() as client:
        assert (await client.post(LIMITS, json={})).status_code == 403  # no Origin
