"""v0.8.4 W7: scenes for video notes, a gap per kind, event days, a voice per backup."""

import base64
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from dom.config import GEMINI_VOICES, OwnerStudioTier, StudioFallback, StudioLimits
from dom.media_files import _PATH, MediaFiles
from dom.models import StoredMessage
from dom.studio import policy
from dom.studio.policy import StudioQuota, gap_ends, standing, turn_note
from dom.tools import EVENT_DAY_TOOL, TurnCtx, enabled_tools
from test_studio import connect, studio_server  # noqa: F401  (the studio fixture)
from test_studio_cloud import IMAGE, PORTRAIT, VOICE, allowed, cloud_server, configure  # noqa: F401
from test_studio_limits import LIMITS, _deliver, _save
from test_studio_video import VIDEO
from test_usage import deliver, online
from test_web_model_settings import ORIGIN_HEADER, START, dashboard  # noqa: F401 (fixture)
from tghelpers import FRIEND, GROUP, OWNER, tg

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def minutes(n):
    return NOW - timedelta(minutes=n)


# --- a gap per kind --------------------------------------------------------------------------------------------------

def test_without_gaps_of_their_own_any_media_holds_back_the_next():
    tier = OwnerStudioTier()  # a 30-minute general gap, no per-kind gaps: as it always was
    ends = gap_ends(tier, [("photo", minutes(10))], NOW)
    assert ends == dict.fromkeys(("voice", "video_note", "photo", "special"), minutes(-20))


def test_a_kind_with_its_own_gap_is_spaced_only_from_its_own_last_item():
    tier = OwnerStudioTier(voice_gap_minutes=0)  # voice has no gap; the rest share the general 30 minutes
    ends = gap_ends(tier, [("photo", minutes(10)), ("voice", minutes(1))], NOW)
    assert "voice" not in ends  # a photo, or another voice note, doesn't hold back a voice note
    assert set(ends) == {"photo", "video_note", "special"} and ends["photo"] == minutes(-20)
    # ...and a voice note holds nothing else back
    assert set(gap_ends(tier, [("voice", minutes(1))], NOW)) == set()


def test_each_kind_can_have_its_own_length_and_the_general_gap_fills_the_rest():
    tier = OwnerStudioTier(gap_minutes=60, image_gap_minutes=45, voice_gap_minutes=0)
    rows = [("photo", minutes(40)), ("photo", minutes(100)), ("video_note", minutes(50))]
    ends = gap_ends(tier, rows, NOW)
    assert ends["photo"] == minutes(-5)  # 45 minutes from the last photo only, not the video note
    assert ends["video_note"] == minutes(-10) and ends["special"] == minutes(-10)  # 60 from the video note
    assert "voice" not in ends


def test_a_voice_note_then_a_photo_can_arrive_together_but_two_photos_stay_apart():
    limits = StudioLimits(owner_dm=OwnerStudioTier(voice_gap_minutes=0, gap_minutes=30))
    after_voice = standing(limits, "owner_dm", 1, [("voice", minutes(0))], NOW)
    assert "photo" in after_voice.left and "photo" not in after_voice.gaps
    after_photo = standing(limits, "owner_dm", 1, [("photo", minutes(0))], NOW)
    assert "photo" in after_photo.gaps and "voice" not in after_photo.gaps
    assert after_photo.next_at == NOW + timedelta(minutes=30)


async def test_the_quota_offers_what_its_own_gap_allows(make_app):
    app = make_app()
    store, config = app.store, app.config.studio.model_copy(update={
        "enabled": True, "base_url": "https://studio.example.org", "voice": True, "images": True,
        "limits": StudioLimits(owner_dm=OwnerStudioTier(voice_gap_minutes=0)),
    })
    await _deliver(app, "photo", app.dispatch.now() - timedelta(minutes=5))
    left = await StudioQuota(store).remaining(config, "owner_dm", OWNER, app.dispatch.now())
    assert set(left) == {"voice"}  # the photo's gap holds back photos, not voice notes


def test_gap_settings_take_false_and_stay_within_a_week():
    assert OwnerStudioTier(voice_gap_minutes=False).voice_gap_minutes is None
    with pytest.raises(ValidationError):
        OwnerStudioTier(video_gap_minutes=10081)
    with pytest.raises(ValidationError):
        OwnerStudioTier(image_gap_minutes=-1)


# --- event days ------------------------------------------------------------------------------------------------------

def test_a_boost_never_rounds_a_limit_past_the_ceiling_or_below_the_normal_one():
    limits = StudioLimits(owner_dm=OwnerStudioTier(voice_daily=1, special_daily=0), daily=1)
    now = standing(limits, "owner_dm", 0, [], NOW, boost=1.1)
    assert now.left["voice"] == 1 and now.daily_left == 1  # 1 x 1.1 stays 1: rounded down, not up to 2
    assert standing(limits, "owner_dm", 0, [], NOW, boost=1.0).left["voice"] == 1
    big = StudioLimits(owner_dm=OwnerStudioTier(voice_daily=3), daily=12)
    assert standing(big, "owner_dm", 0, [], NOW, boost=1.5).left["voice"] == 4  # 4.5 down to 4
    assert standing(big, "owner_dm", 0, [], NOW, boost=1.5).daily_left == 18


def test_an_event_day_raises_every_limit_and_leaves_the_normal_ones_visible():
    limits = StudioLimits()  # owner DM: 3 voice, 1 video, 2 photos, 1 special; 12 a day in all
    rows = [("voice", minutes(60 * i)) for i in range(1, 4)] + [("video_note", minutes(30))]
    normal = standing(limits, "owner_dm", 4, rows, NOW)
    boosted = standing(limits, "owner_dm", 4, rows, NOW, boost=2)
    assert normal.left["voice"] == normal.left["video_note"] == 0
    assert boosted.left["voice"] == 3 and boosted.left["video_note"] == 1 and boosted.left["photo"] == 4
    assert boosted.normal == normal.normal and boosted.daily_left == 20 and normal.daily_left == 8


async def test_an_item_past_the_normal_limit_needs_the_months_unused_budget(make_app):
    app = make_app()
    config = app.config.studio.model_copy(update={"enabled": True, "base_url": "https://studio.example.org",
                                                  "video_notes": True})
    now = app.dispatch.now()
    await _deliver(app, "video_note", now - timedelta(hours=2))  # the one video note a day, used
    budget = {"extra": True}

    class Events:
        async def multiplier(self, at):
            return 2.0

        async def may_extra(self, kind):
            return budget["extra"]

    quota = StudioQuota(app.store, Events())
    assert await quota.remaining(config, "owner_dm", OWNER, now) == {"video_note": 1}
    budget["extra"] = False
    assert await quota.remaining(config, "owner_dm", OWNER, now) == {}
    async with quota.reserve(config, "owner_dm", OWNER, "video_note", now) as reserved:
        assert not reserved
    assert not await StudioQuota(app.store).remaining(config, "owner_dm", OWNER, now)  # no event day: none


async def test_an_item_within_the_normal_limit_never_asks_for_budget(make_app):
    app = make_app()
    config = app.config.studio.model_copy(update={"enabled": True, "base_url": "https://studio.example.org",
                                                  "voice": True})

    class Events:
        async def multiplier(self, at):
            return 2.0

        async def may_extra(self, kind):
            raise AssertionError("an ordinary item is not a one-off")

    assert await StudioQuota(app.store, Events()).remaining(config, "owner_dm", OWNER, app.dispatch.now()) == {"voice": 6}


def event_config(app, **updates):
    app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update=updates)})


async def test_dom_declares_an_event_day_within_the_owners_ceiling(make_app):
    app = make_app()
    now = app.dispatch.now()
    ok = await app.event_days.declare("Concert", now, multiplier=5)
    assert ok.startswith("ok:") and "2×" in ok  # the ceiling is 2 by default
    assert await app.event_days.multiplier(now) == 2.0
    [row] = await app.store.event_days("2000-01-01")
    assert row["title"] == "Concert" and row["multiplier"] == 2.0 and row["cancelled_at"] is None
    assert any("Concert" in event for event in [e async for e in _owner_events(app)])


async def _owner_events(app):
    for message in await app.store.get_recent_chat_window(OWNER, 20):
        yield message.text


async def test_the_owners_ceiling_caps_a_declared_day_even_after_it_is_lowered(make_app):
    app = make_app()
    now = app.dispatch.now()
    await app.event_days.declare("Trip", now)
    event_config(app, event_day_max_multiplier=1.5)
    assert await app.event_days.multiplier(now) == 1.5
    event_config(app, event_day_max_multiplier=0)  # off: the day does nothing
    assert await app.event_days.multiplier(now) == 1.0
    assert (await app.event_days.declare("Another", now)).startswith("error: event days are turned off")


@pytest.mark.parametrize(("day", "problem"), [
    ("not a date", "day must be a date"), ("2000-01-01", "today or within"), ("2099-01-01", "today or within"),
])
async def test_a_day_must_be_a_real_date_from_today_to_two_weeks_ahead(make_app, day, problem):
    app = make_app()
    assert problem in await app.event_days.declare("Trip", app.dispatch.now(), day)


async def test_one_event_day_a_week_by_default_and_a_cancelled_one_does_not_count(make_app):
    app = make_app()
    now = app.dispatch.now()
    local = app.event_days.local_date(now)
    assert (await app.event_days.declare("Concert", now)).startswith("ok")
    assert "already an event day" in await app.event_days.declare("Again", now)
    nearby = (local + timedelta(days=3)).isoformat()
    assert "weekly allowance" in await app.event_days.declare("Trip", now, nearby)
    [row] = await app.store.event_days("2000-01-01")
    assert await app.store.cancel_event_day(row["id"], now)
    assert not await app.store.cancel_event_day(row["id"], now)  # already cancelled
    assert await app.event_days.multiplier(now) == 1.0  # its limits are gone at once
    assert (await app.event_days.declare("Trip", now, nearby)).startswith("ok")
    event_config(app, event_days_per_week=2)
    assert (await app.event_days.declare("Birthday", now, (local + timedelta(days=1)).isoformat())).startswith("ok")


async def test_event_days_blank_title_is_refused_and_a_long_one_cut(make_app):
    app = make_app()
    assert "say what the day is for" in await app.event_days.declare("   ", app.dispatch.now())
    await app.event_days.declare("x" * 500, app.dispatch.now())
    [row] = await app.store.event_days("2000-01-01")
    assert len(row["title"]) == 80


async def test_extra_items_draw_only_on_the_months_unused_budget(make_app):
    app = make_app()
    event_config(app)
    app.config = app.config.model_copy(update={"monthly_spend_cap_usd": 1.0})
    assert await app.event_days.may_extra("video_note")  # nothing spent yet
    await app.dispatch.usage.record("chat", "m", "host", {"cost": 0.8})
    assert await app.event_days.may_extra("photo") and not await app.event_days.may_extra("video_note")
    await app.dispatch.usage.record("chat", "m", "host", {"cost": 0.2})
    assert not await app.event_days.may_extra("voice")  # at the cap: nothing more


async def test_a_turn_note_says_when_it_is_an_event_day():
    note = turn_note({"voice": 3}, (), "Today is an event day you declared (Concert).")
    assert "event day you declared (Concert)" in note
    assert "event day" not in turn_note({"voice": 3}, ())


async def test_dom_is_told_in_his_turn_when_an_event_day_is_on(make_app, studio_server):
    app = make_app(contents=('{"bubbles":["hey"]}',))
    await connect(app, studio_server)
    await online(app, OWNER)
    await app.event_days.declare("Concert", app.dispatch.now())
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert "event day you declared (Concert)" in json.dumps(app.script.requests[0])


@pytest.mark.parametrize("chat", [FRIEND, GROUP])
async def test_the_event_days_title_stays_in_the_owner_dm(make_app, studio_server, chat):
    app = make_app(contents=('{"bubbles":["hey"]}',))
    await connect(app, studio_server)
    app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update={"voice": True})})
    await online(app, chat)
    await app.event_days.declare("Surprise party for the owner", app.dispatch.now())
    await deliver(app, tg(chat_id=chat, sender=FRIEND))
    prompt = json.dumps(app.script.requests[0])
    assert "Surprise party" not in prompt and "event day you declared" not in prompt
    assert "can send" in prompt  # media is on in this chat, so the leak would have had somewhere to land


def ctx(app, scope="owner_dm", chat=OWNER):
    return TurnCtx(scope, chat, chat, app.home, app.store, OWNER, app=app)


async def test_the_declare_tool_is_for_the_owner_dm_and_only_while_media_and_event_days_are_on(make_app, studio_server):
    app = make_app()
    names = lambda tools: [tool.name for tool in tools]  # noqa: E731
    assert "declare_event_day" not in names(await enabled_tools(app, "owner_dm"))  # studio off
    await connect(app, studio_server)
    assert "declare_event_day" in names(await enabled_tools(app, "owner_dm"))
    assert "declare_event_day" not in names(await enabled_tools(app, "friend_dm"))
    event_config(app, event_day_max_multiplier=0)
    assert "declare_event_day" not in names(await enabled_tools(app, "owner_dm"))
    event_config(app, event_day_max_multiplier=2)
    assert (await EVENT_DAY_TOOL.handler({"title": "Concert"}, ctx(app))).startswith("ok:")
    assert (await EVENT_DAY_TOOL.handler({"title": "Concert"}, ctx(app, "friend_dm", FRIEND))).startswith("error: event days are available only")
    assert (await EVENT_DAY_TOOL.handler({"title": "x", "multiplier": "lots"}, ctx(app))).startswith("error: multiplier")


async def test_the_owner_lists_and_cancels_event_days(dashboard):
    app, path, browser = dashboard
    path.write_text(START)
    await app.event_days.declare("Concert", app.dispatch.now())
    async with browser() as client:
        body = (await client.get("/api/studio/event-days")).json()
        assert body["ceiling"] == 2 and body["per_week"] == 1
        [event] = body["events"]
        assert event["title"] == "Concert" and event["active"] and not event["cancelled"]
        cancelled = await client.post("/api/studio/event-days/cancel", headers=ORIGIN_HEADER, json={"id": event["id"]})
        assert cancelled.status_code == 200
        assert cancelled.json()["events"][0]["cancelled"] and not cancelled.json()["events"][0]["active"]
        assert (await client.post("/api/studio/event-days/cancel", headers=ORIGIN_HEADER, json={"id": event["id"]})).status_code == 404
        assert (await client.post("/api/studio/event-days/cancel", headers=ORIGIN_HEADER, json={"id": 1, "x": 1})).status_code == 422
    assert await app.event_days.multiplier(app.dispatch.now()) == 1.0


async def test_studio_status_lists_the_event_day(make_app, studio_server):
    from dom.studio.profile import status_text

    app = make_app()
    await connect(app, studio_server)
    await app.event_days.declare("Concert", app.dispatch.now())
    text = await status_text(app)
    assert "Event day" in text and "Concert" in text and "on now" in text


async def test_the_limits_api_reads_and_saves_a_gap_per_kind(dashboard):
    app, path, browser = dashboard
    path.write_text(START)
    async with browser() as client:
        body = (await client.get(LIMITS)).json()
        assert body["limits"]["owner_dm"]["voice_gap_minutes"] is None
        assert {item["kind"]: item["next_at"] for item in body["owner"]["kinds"]} == dict.fromkeys(
            ("voice", "video_note", "photo", "special"))

        def change(limits):
            limits["owner_dm"]["voice_gap_minutes"] = 0
            limits["owner_dm"]["image_gap_minutes"] = 90

        assert (await _save(client, change)).status_code == 200
        saved = (await client.get(LIMITS)).json()["limits"]["owner_dm"]
        assert (saved["voice_gap_minutes"], saved["image_gap_minutes"], saved["video_gap_minutes"]) == (0, 90, None)
        assert app.config.studio.limits.owner_dm.image_gap_minutes == 90
        assert "image_gap_minutes = 90" in path.read_text()
        assert (await _save(client, lambda limits: limits["owner_dm"].update(image_gap_minutes=None))).status_code == 200
        assert (await client.get(LIMITS)).json()["limits"]["owner_dm"]["image_gap_minutes"] is None
        assert (await _save(client, lambda limits: limits["owner_dm"].update(special_gap_minutes=10081))).status_code == 422
        # every limit must be sent, the new gaps included
        current = (await client.get(LIMITS)).json()
        del current["limits"]["owner_dm"]["voice_gap_minutes"]
        assert (await client.post(LIMITS, headers=ORIGIN_HEADER, json=current)).status_code == 422


# --- a voice for each backup -----------------------------------------------------------------------------------------

def backup(provider="openrouter_tts", model="google/gemini-3.8-flash-lite-tts", **extra):
    return StudioFallback(provider=provider, model=model, api_key_env="MEDIA_KEY", **extra)


def test_a_gemini_voice_is_checked_where_it_can_be():
    assert backup(voice="Kore").voice == "Kore"
    assert backup("gemini_tts", "gemini-2.5-flash-preview-tts", voice="Puck", paid=True).voice == "Puck"
    assert {"Achird", "Kore", "Puck"} <= GEMINI_VOICES and len(GEMINI_VOICES) == 30
    for bad in (dict(voice="Nobody"), dict(provider="gemini_tts", model="gemini-2.5-flash-preview-tts", voice="alloy")):
        with pytest.raises(ValidationError, match="not a Gemini voice"):
            backup(**bad)


def test_another_openrouter_model_names_its_own_voices_and_only_a_voice_backup_takes_one():
    assert backup(model="openai/gpt-4o-mini-tts", voice="alloy").voice == "alloy"
    for bad in ("two words", "9lives", "x" * 65, "a/b"):
        with pytest.raises(ValidationError):
            backup(model="openai/gpt-4o-mini-tts", voice=bad)
    with pytest.raises(ValidationError, match="Only a voice backup"):
        backup("openrouter_image", "google/gemini-2.5-flash-image", voice="Kore")
    assert backup().voice is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
async def test_a_backup_speaks_in_its_own_voice_else_achird(make_app, cloud_server):
    app = make_app()
    configure(app, cloud_server, voice=(VOICE.model_copy(update={"voice": "Kore"}),))
    assert await app.cloud_studio.generate(app.config, "voice", "Hi", None, OWNER, allowed)
    assert cloud_server[1][0][1]["voice"] == "Kore"
    configure(app, cloud_server, voice=(VOICE,))
    assert await app.cloud_studio.generate(app.config, "voice", "Hi", None, OWNER, allowed)
    assert [call[1]["voice"] for call in cloud_server[1] if call[0] == "/api/v1/audio/speech"] == ["Kore", "Achird"]


async def test_the_dashboard_saves_a_voice_with_a_backup_and_refuses_a_wrong_one(dashboard):
    app, path, browser = dashboard
    path.write_text(START)
    async with browser() as client:
        settings = (await client.get("/api/permissions")).json()
        body = {"revision": settings["revision"], "chain": "voice", "index": None, "backup": {
            "provider": "gemini_tts", "model": "gemini-2.5-flash-preview-tts", "api_key_env": "GEMINI_MEDIA_API_KEY",
            "paid": True, "voice": "Nobody", "supports_reference": True, "allow_without_reference": False,
            "data_policy": "zdr"}}
        refused = await client.post("/api/studio/save", headers=ORIGIN_HEADER, json=body)
        assert refused.status_code == 422 and "Gemini voice" in refused.text
        assert 'voice = "' not in path.read_text()


# --- scenes for video notes ------------------------------------------------------------------------------------------

STILL = base64.b64encode(b"fake media").decode()


@pytest.fixture
def raw_stills(monkeypatch):
    """The fixtures' pictures are not real images, so the conversion to a PNG still passes them through."""
    from dom.telegram import executor

    monkeypatch.setattr(executor, "png_still", lambda data: data)


async def scene_app(make_app, studio_server, video_note=None, *replies, **studio):
    video_note = video_note or {"text": "waving hello", "scene": "on a rooftop at dusk"}
    app = make_app(contents=(json.dumps({"bubbles": ["hey"], "video_note": video_note}), *replies))
    await connect(app, studio_server)
    app.studio._health = {**app.studio._health, "video_still": True}
    if studio:
        app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update=studio)})
    await online(app, OWNER)
    return app


def posts(studio_server, route):
    return [body for path, body in studio_server[2] if path == route]


async def test_a_video_note_is_filmed_from_a_fresh_still_of_the_scene(make_app, studio_server, raw_stills):
    app = await scene_app(make_app, studio_server)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [image], [video] = posts(studio_server, "/v1/image"), posts(studio_server, "/v1/video_note")
    assert "on a rooftop at dusk" in image["scene"] and image["scene"].endswith("nothing covering it.")
    assert image.get("outfit") == video.get("outfit")  # the same outfit for the still and the note
    assert video["still"] == STILL and video["text"] == "waving hello"
    assert [path for path, _ in studio_server[2] if path != "/v1/health"] == ["/v1/image", "/v1/video_note"]
    assert len(app.bot.of("video_note")) == 1
    [stored] = [m for m in await app.store.get_recent_chat_window(OWNER, 5) if m.media_json]
    media = json.loads(stored.media_json)
    assert media["still"] is True and media["filmed_in"] == "on a rooftop at dusk"
    # kept beside the video, and the pair is one item to the cap
    assert (app.home.root / media["still_file"]).read_bytes() == b"fake media"
    assert (app.home.root / media["file"]).read_bytes() == b"fake media" and media["bytes"] == 20


async def test_the_still_counts_toward_the_studio_wide_total_but_not_the_chats_photo_allowance(
    make_app, studio_server, raw_stills,
):
    app = await scene_app(make_app, studio_server)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    total, rows = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert total == 2 and [kind for kind, _ in rows] == ["video_note"]  # two items of studio work, one delivered note
    left = (await app.studio_quota.standing(app.config.studio, "owner_dm", OWNER, app.dispatch.now())).left
    assert left["photo"] == 2 and left["video_note"] == 0


async def test_a_still_that_has_no_room_left_in_the_daily_total_is_not_made(make_app, studio_server, raw_stills):
    app = await scene_app(make_app, studio_server, limits=StudioLimits(daily=1))
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert posts(studio_server, "/v1/image") == []  # one slot: the note takes it, animating the portrait
    assert "still" not in posts(studio_server, "/v1/video_note")[0] and len(app.bot.of("video_note")) == 1
    total, _ = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert total == 1


async def test_a_still_counts_toward_the_total_even_when_the_note_is_never_delivered(
    make_app, studio_server, raw_stills,
):
    studio_server[3]["/v1/video_note"] = 503
    app = await scene_app(make_app, studio_server)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(posts(studio_server, "/v1/image")) == 1 and not app.bot.of("video_note")
    total, rows = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert total == 1 and rows == []  # the picture was made and paid for; no media was delivered


async def test_no_scene_given_the_still_is_made_from_what_he_says(make_app, studio_server, raw_stills):
    app = await scene_app(make_app, studio_server, {"text": "waving hello"})
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [image] = posts(studio_server, "/v1/image")
    assert "somewhere that suits what he says: waving hello" in image["scene"] and len(image["scene"]) <= 300
    assert posts(studio_server, "/v1/video_note")[0]["still"] == STILL


async def test_if_the_still_cannot_be_made_the_note_uses_the_portrait(make_app, studio_server, raw_stills):
    studio_server[3]["/v1/image"] = 503
    app = await scene_app(make_app, studio_server)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [video] = posts(studio_server, "/v1/video_note")
    assert "still" not in video and len(app.bot.of("video_note")) == 1
    [stored] = [m for m in await app.store.get_recent_chat_window(OWNER, 5) if m.media_json]
    assert "still" not in json.loads(stored.media_json)  # nothing was filmed from one


async def test_a_picture_that_is_not_an_image_is_not_used_as_a_still(make_app, studio_server):
    app = await scene_app(make_app, studio_server)  # the fixture's "fake media" is not a picture: png_still refuses it
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert "still" not in posts(studio_server, "/v1/video_note")[0] and len(app.bot.of("video_note")) == 1


@pytest.mark.parametrize("setup", ["switch off", "older studio"])
async def test_no_still_when_scenes_are_off_or_the_studio_cannot_take_one(make_app, studio_server, raw_stills, setup):
    app = await scene_app(make_app, studio_server, video_scenes=setup != "switch off")
    if setup == "older studio":
        app.studio._health = {k: v for k, v in app.studio._health.items() if k != "video_still"}
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert posts(studio_server, "/v1/image") == [] and "still" not in posts(studio_server, "/v1/video_note")[0]


async def test_the_look_checks_the_still_before_animating_not_the_video(make_app, studio_server, raw_stills, monkeypatch):
    from dom.studio import look
    from test_native_media import see

    async def never(video):
        raise AssertionError("the video is not looked at when its still was")

    monkeypatch.setattr(look, "first_frame", never)
    app = await scene_app(
        make_app, studio_server, None, '{"match": false, "fix": "on a rooftop at dusk, facing the camera"}', '{"better": "B"}',
    )
    see(app, ("text", "image"))
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    images = posts(studio_server, "/v1/image")
    from dom.studio.policy import STILL_FRAMING
    assert [tail in post["scene"] for post, tail in zip(images, ("dusk", "facing the camera"))] == [True, True]
    assert all(post["scene"].endswith(STILL_FRAMING) for post in images)  # the retake keeps the framing too
    assert len(posts(studio_server, "/v1/video_note")) == 1 and len(app.script.requests) == 3  # turn, look, compare


async def test_a_cloud_note_is_filmed_from_a_still_the_cloud_image_backup_makes(make_app, cloud_server, monkeypatch):
    from test_studio_video import VOICE as VIDEO_VOICE  # noqa: F401

    app = make_app(contents=(json.dumps({"bubbles": ["hey"], "video_note": {"text": "hi", "scene": "at a cafe"}}),))
    configure(app, cloud_server, voice=(VOICE,), images=(IMAGE,), video_fallbacks=(VIDEO,), enabled=False,
              default_outfit="casual")
    folder = app.config.home / "studio-portraits"
    folder.mkdir()
    (folder / "casual.png").write_bytes(PORTRAIT)
    stills = []

    async def generate_video(config, kind, outfit, chat_id, allowed, make_voice, still=None):
        stills.append(still)
        return b"cloud video"

    async def send_video_note(chat_id, file, **kwargs):
        return await app.bot.send_message(chat_id, "fixture video note", **kwargs)

    app.cloud_studio.generate_video = generate_video
    app.bot.send_video_note = send_video_note
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [(route, payload)] = [call for call in cloud_server[1] if call[0] == "/api/v1/images"]
    assert "at a cafe" in payload["prompt"] and payload["input_references"]  # the approved portrait is the reference
    [still] = stills
    assert still.startswith(b"\x89PNG")  # whatever the image backup returned, converted to the PNG the avatar takes
    assert any(row[0] == "studio_image" for row in __import__("test_studio_cloud").usage_rows(app))  # cost recorded


def test_png_still_shrinks_and_converts_and_refuses_what_is_not_a_picture():
    import io

    from PIL import Image

    from dom.studio.cloud import png_still

    out = io.BytesIO()
    Image.new("RGB", (2000, 1000), "red").save(out, "JPEG")
    made = png_still(out.getvalue())
    image = Image.open(io.BytesIO(made))
    assert made.startswith(b"\x89PNG") and max(image.size) == 768
    assert png_still(b"not an image") is None


# --- kept media: the still beside the video ---------------------------------------------------------------------------

async def stored_note(app, still=b"S" * 10, video=b"V" * 30, msg=7):
    files = MediaFiles(app.home.root, app.store, lambda: 100)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fields = await files.keep(OWNER, msg, "video_note", video, at, still)
    await app.store.record_new_message(StoredMessage(
        chat_id=str(OWNER), msg_id=msg, role="assistant", visibility_scope=f"chat:{OWNER}", text="",
        media_json=json.dumps({"kind": "video_note", **fields}), created_at=at.isoformat(),
    ))
    return files, fields


async def test_the_still_is_kept_beside_the_video_and_the_pair_counts_once(make_app):
    app = make_app()
    files, fields = await stored_note(app)
    assert fields["still_file"] == fields["file"].replace(".mp4", "-still.png") and fields["bytes"] == 40
    assert await files.read(OWNER, fields["still_file"]) == b"S" * 10
    assert await files.read(FRIEND, fields["still_file"]) is None  # only through its own chat
    assert _PATH.fullmatch(fields["still_file"]) and (await files.usage())["bytes"] == 40


async def test_a_still_that_does_not_fit_the_cap_is_dropped_not_the_video(make_app):
    app = make_app()
    _, fields = await stored_note(app, still=b"S" * 80)  # 30 + 80 is over the 100-byte cap; the video alone fits
    assert "still_file" not in fields and fields["bytes"] == 30


async def test_eviction_removes_both_files_and_the_sweep_keeps_both(make_app):
    app = make_app()
    files, fields = await stored_note(app)
    assert await files.reconcile() == 0 and (app.home.root / fields["still_file"]).exists()
    (app.home.root / fields["still_file"].replace("-still.png", "-orphan.png")).write_bytes(b"x")
    assert await files.reconcile() == 1  # a file no message records goes; the still, which one does, stays
    assert (app.home.root / fields["still_file"]).exists()
    files._cap = lambda: 10
    assert await files.evict() == 1
    assert not (app.home.root / fields["file"]).exists() and not (app.home.root / fields["still_file"]).exists()
    [row] = [m for m in await app.store.get_recent_chat_window(OWNER, 5) if m.media_json]
    media = json.loads(row.media_json)
    assert media["removed"] is True and not {"file", "still_file", "bytes"} & set(media)


async def test_the_library_serves_a_kept_still_only_for_its_own_note(dashboard):
    app, path, browser = dashboard
    path.write_text(START)
    app.media_files = MediaFiles(app.home.root, app.store, lambda: 100)
    files, fields = await stored_note(app)
    app.media_files = files
    async with browser() as client:
        listed = (await client.get("/api/media/files")).json()["files"][0]
        assert listed["still_url"] == f"/api/media/files/{OWNER}/7?still=true"
        served = await client.get(listed["still_url"])
        assert served.status_code == 200 and served.content == b"S" * 10 and served.headers["content-type"] == "image/png"
        assert (await client.get(f"/api/media/files/{OWNER}/8?still=true")).status_code == 404


# --- the store ------------------------------------------------------------------------------------------------------

def test_the_event_days_table_arrives_with_a_migration(tmp_path):
    import sqlite3

    from dom.store.store import MIGRATIONS, migrate_database

    path = tmp_path / "dom.db"
    migrate_database(path)
    with sqlite3.connect(path) as db:
        assert {row[1] for row in db.execute("PRAGMA table_info(event_days)")} == {
            "id", "day", "title", "multiplier", "declared_at", "cancelled_at"}
    assert (32, "0031_event_days.sql") in MIGRATIONS


async def test_two_declarations_at_once_cannot_both_take_the_weekly_allowance(make_app):
    import asyncio

    app = make_app()
    now = app.dispatch.now()
    results = await asyncio.gather(*(
        app.store.declare_event_day("2026-03-01", f"Day {i}", 2.0, now, 1) for i in range(5)
    ))
    assert sum(event_id is not None for event_id, _ in results) == 1


async def test_a_video_notes_scene_is_screened_like_its_speech(make_app, studio_server, raw_stills):
    # the private-text guard sees the scene, not only the words he says, because a still is made from it
    app = await scene_app(make_app, studio_server)
    (app.home.root / "private").mkdir(exist_ok=True)
    video = {"text": "waving hello", "scene": "reading the fictional passphrase lemon waffles aloud"}
    from dom.actions import Actions, VideoNote

    seen = []
    from dom import guard

    original = guard.check_local_private_text
    guard.check_local_private_text = lambda config, home, texts: seen.append(list(texts)) or original(config, home, texts)
    try:
        await app.dispatch._screen_studio(OWNER, "owner_dm", Actions(bubbles=["hey"], video_note=VideoNote(**video)), "Owner")
    finally:
        guard.check_local_private_text = original
    assert ["waving hello", video["scene"]] in seen


async def test_an_older_local_studio_gets_no_still_and_no_cloud_still_is_bought_for_it(
    make_app, studio_server, cloud_server, raw_stills,
):
    app = await scene_app(make_app, studio_server)
    app.studio._health = {k: v for k, v in app.studio._health.items() if k != "video_still"}  # a 0.8.3 studio
    configure(app, cloud_server, images=(IMAGE,), default_outfit="casual")
    (app.config.home / "studio-portraits").mkdir()
    (app.config.home / "studio-portraits" / "casual.png").write_bytes(PORTRAIT)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert [call for call in cloud_server[1] if call[0] == "/api/v1/images"] == []  # nothing paid for a still
    assert posts(studio_server, "/v1/image") == []
    [video] = posts(studio_server, "/v1/video_note")
    assert "still" not in video and len(app.bot.of("video_note")) == 1


async def test_the_studio_client_never_sends_a_still_to_a_studio_without_video_still(make_app, studio_server):
    app = make_app()
    await connect(app, studio_server)
    await app.studio.video("hi", False, None, b"S")
    assert "still" not in posts(studio_server, "/v1/video_note")[0]
    app.studio._health = {**app.studio._health, "video_still": True}
    await app.studio.video("hi", False, None, b"S")
    assert "still" in posts(studio_server, "/v1/video_note")[1]


def test_a_still_prompt_keeps_its_framing_within_the_scene_limit():
    from dom.studio.policy import SCENE_LIMIT, STILL_FRAMING, still_scene

    short = still_scene("on a rooftop at dusk")
    assert short == f"on a rooftop at dusk {STILL_FRAMING}"
    long = still_scene("x" * 290 + "  spaced   out ")
    assert len(long) <= SCENE_LIMIT and long.endswith(STILL_FRAMING)
