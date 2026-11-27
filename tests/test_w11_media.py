"""v0.8.6 W11: how readily Dom adds media (E115), and watching clips as frame strips plus a transcript (E116)."""

import asyncio
import io
import json
import shutil
import subprocess
import tomllib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Video
from PIL import Image

from dom import video
from dom.config import ModelEndpoint, OwnerStudioTier, StudioConfig, StudioLimits
from dom.llm import ChatClient
from dom.media import MediaDescriber, PermanentMediaSkip, download
from dom.media_files import MediaFiles, files_of
from dom.studio.policy import (
    STUDIO_NOTE, StudioQuota, gap_ends, gap_minutes, scaled, standing, turn_note, windows,
)
from dom.telegram.handlers import handle_message
from test_media import FakeDownload
from test_studio_limits import LIMITS, STUDIO, _save
from test_usage import online
from test_web_model_settings import ORIGIN_HEADER, START, dashboard  # noqa: F401 (fixture)
from tghelpers import GROUP, OWNER, drain_dispatch, no_sleep, tg, turn_requests

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed here, so no strip can be made")


# --- E115: spontaneity ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("level, phrase", [
    ("rarely", "the rare moment one truly adds something"),
    ("sometimes", "Use these sparingly when they add something."),
    ("often", "a voice note or a photo now and then makes you feel real"),
])
def test_the_turn_note_leads_with_the_level_and_keeps_the_rules(level, phrase):
    note = turn_note({"photo": 3}, (), spontaneity=level)
    assert phrase in note
    assert "fields of your final JSON answer" in note and "never call a tool named" in note  # every level keeps the rules
    assert "At most one of these per reply." in note and '"image": a scene' in note
    assert (STUDIO_NOTE in note) == (level == "sometimes")
    assert len({turn_note({"photo": 3}, (), spontaneity=other) for other in ("rarely", "sometimes", "often")}) == 3


def test_the_default_level_is_todays_note():
    assert turn_note({"photo": 3}, ()) == turn_note({"photo": 3}, (), spontaneity="sometimes")
    assert StudioConfig().spontaneity == "sometimes"


def test_scaling_rounds_down_but_never_turns_a_limit_off_or_on():
    assert [scaled(limit, 0.5) for limit in (0, 1, 2, 3, 5)] == [0, 1, 1, 1, 2]
    assert [scaled(limit, 2.0) for limit in (0, 1, 3)] == [0, 2, 6]
    assert scaled(7, 1.0) == 7


def test_the_levels_scale_each_kinds_limit_and_gap():
    tier = OwnerStudioTier(voice_daily=4, image_daily=2, gap_minutes=30, voice_gap_minutes=0, image_gap_minutes=45)
    assert {k: v[0] for k, v in windows(tier, 0.5).items()}["voice"] == 2
    assert {k: v[0] for k, v in windows(tier, 2.0).items()}["photo"] == 4
    assert gap_minutes(tier, "photo", 0.5) == 90 and gap_minutes(tier, "photo", 2.0) == 22  # 45 doubled, 45 halved
    assert gap_minutes(tier, "voice", 2.0) == 0  # no gap stays none
    assert gap_minutes(tier, "video_note", 2.0) == 15  # the general 30
    rows = [("photo", NOW - timedelta(minutes=40))]
    assert "photo" not in gap_ends(tier, rows, NOW, 2.0)  # a gap of 22 minutes is over
    assert "photo" in gap_ends(tier, rows, NOW, 0.5)  # 90 minutes is not


def test_a_level_never_passes_the_global_daily_total():
    limits = StudioLimits(daily=3, owner_dm=OwnerStudioTier(voice_daily=4, image_daily=5))
    often = standing(limits, "owner_dm", 0, [], NOW, spontaneity="often")
    assert often.left["voice"] == 3 and often.left["photo"] == 3 and often.daily_left == 3  # clamped to the total
    assert standing(limits, "owner_dm", 2, [], NOW, spontaneity="often").left["voice"] == 1  # what is left of the total
    assert standing(limits, "owner_dm", 3, [], NOW, spontaneity="often").left["voice"] == 0
    roomy = StudioLimits(daily=50, owner_dm=OwnerStudioTier(voice_daily=4))
    assert [standing(roomy, "owner_dm", 0, [], NOW, spontaneity=level).left["voice"]
            for level in ("rarely", "sometimes", "often")] == [2, 4, 8]


def test_an_event_day_raises_the_scaled_limit_and_still_stops_at_the_total():
    limits = StudioLimits(daily=10, owner_dm=OwnerStudioTier(voice_daily=4))
    now = standing(limits, "owner_dm", 0, [], NOW, boost=1.5, spontaneity="often")
    assert now.normal["voice"] == 8 and now.left["voice"] == 12  # 4 doubled, then raised by the event day
    assert now.daily_left == 15
    big = StudioLimits(daily=10, owner_dm=OwnerStudioTier(voice_daily=8))
    now = standing(big, "owner_dm", 0, [], NOW, boost=1.5, spontaneity="often")
    assert now.normal["voice"] == 10 and now.left["voice"] == 15  # each held to its total, normal or event day


async def test_the_quota_reads_the_level_from_the_studio_config(make_app):
    app = make_app()
    limits = StudioLimits(daily=50, owner_dm=OwnerStudioTier(voice_daily=4, gap_minutes=0))
    base = StudioConfig(enabled=True, base_url="https://studio.example.org", voice=True, limits=limits)
    quota = StudioQuota(app.store)
    for level, voices in (("rarely", 2), ("sometimes", 4), ("often", 8)):
        config = base.model_copy(update={"spontaneity": level})
        assert (await quota.remaining(config, "owner_dm", OWNER, NOW))["voice"] == voices
        assert (await quota.standing(config, "owner_dm", OWNER, NOW)).left["voice"] == voices


async def test_a_reply_turn_carries_the_levels_wording(make_app):
    app = make_app()
    await online(app, OWNER)
    app.config = app.config.model_copy(update={"studio": StudioConfig(
        enabled=True, base_url="https://studio.example.org", voice=True, spontaneity="often")})
    app.studio = SimpleNamespace(ready=True)  # the local studio is set up: voice is on offer
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hey"))
    await drain_dispatch(app.dispatch)
    [request] = turn_requests(app)
    assert "makes you feel real" in request["messages"][-1]["content"]


async def test_the_dashboard_reads_and_saves_the_level(dashboard):
    app, path, browser = dashboard
    path.write_text(START + STUDIO)
    async with browser() as client:
        assert (await client.get(LIMITS)).json()["spontaneity"] == "sometimes"
        current = (await client.get(LIMITS)).json()
        body = {"revision": current["revision"], "limits": current["limits"], "spontaneity": "often"}
        response = await client.post(LIMITS, headers=ORIGIN_HEADER, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["spontaneity"] == "often"
        assert tomllib.loads(path.read_text())["studio"]["spontaneity"] == "often"
        assert app.config.studio.spontaneity == "often"  # applies at once, without a restart
        # a limits save that leaves the level out keeps it
        response = await _save(client, lambda limits: limits.update(daily=11))
        assert response.status_code == 200 and response.json()["spontaneity"] == "often"
        assert tomllib.loads(path.read_text())["studio"]["spontaneity"] == "often"
        current = (await client.get(LIMITS)).json()
        bad = {"revision": current["revision"], "limits": current["limits"], "spontaneity": "constantly"}
        assert (await client.post(LIMITS, headers=ORIGIN_HEADER, json=bad)).status_code == 422
    assert app.config.studio.spontaneity == "often"


# --- E116: frame timing maths ----------------------------------------------------------------------------------------

def test_a_short_clip_is_one_strip_with_a_frame_every_two_seconds():
    [span], cut = plan_of(6)
    assert not cut and span.times == (1.0, 3.0, 5.0) and (span.start, span.end) == (0.0, 6.0)


def plan_of(seconds):
    return video.plan(seconds)


@pytest.mark.parametrize("seconds, frames, step", [(30, 15, 2.0), (40, 16, 2.5), (80, 16, 5.0)])
def test_one_strip_spaces_up_to_sixteen_frames_between_two_and_five_seconds(seconds, frames, step):
    [span], cut = plan_of(seconds)
    assert not cut and len(span.times) == frames
    gaps = {round(b - a, 6) for a, b in zip(span.times, span.times[1:])}
    assert gaps == {step}
    assert span.times[0] == step / 2 and span.times[-1] == seconds - step / 2  # the middles of equal slices


def test_a_very_short_clip_still_gets_a_frame():
    for seconds in (0, 0.4, 1):
        [span], cut = plan_of(seconds)
        assert not cut and len(span.times) >= 1 and all(0 <= t <= max(seconds, 0.001) for t in span.times)


def test_a_long_clip_is_split_into_equal_parts_of_one_strip_each():
    spans, cut = plan_of(100)
    assert not cut and len(spans) == 2
    assert [(s.start, s.end) for s in spans] == [(0.0, 50.0), (50.0, 100.0)]
    assert all(len(s.times) == 16 for s in spans)
    flat = [t for s in spans for t in s.times]
    assert flat == sorted(flat) and len(set(flat)) == 32
    assert all(s.start < t < s.end for s in spans for t in s.times)
    assert len(plan_of(81)[0]) == 2 and len(plan_of(80)[0]) == 1


def test_the_total_of_strips_and_seconds_is_capped_and_the_cut_is_reported():
    spans, cut = plan_of(video.MAX_SECONDS)
    assert len(spans) == video.MAX_STRIPS and not cut and spans[-1].end == video.MAX_SECONDS
    spans, cut = plan_of(3600)
    assert len(spans) == video.MAX_STRIPS and cut and spans[-1].end == video.MAX_SECONDS
    assert all(len(s.times) <= video.FRAMES_PER_STRIP for s in spans)
    assert video.MAX_SECONDS == 320 and video.DOWNLOAD_BYTES == 20_000_000


def test_clock_and_probe_parsing():
    assert [video.clock(s) for s in (0, 5.9, 65, 600, 3725)] == ["0:00", "0:05", "1:05", "10:00", "1:02:05"]
    report = ("Input #0, mov,mp4, from 'clip':\n  Duration: 00:01:02.50, start: 0.000000, bitrate: 214 kb/s\n"
              "  Stream #0:0[0x1](und): Video: h264\n  Stream #0:1[0x2](und): Audio: aac (LC)\n")
    assert video.parse_probe(report) == (62.5, True)
    assert video.parse_probe("Duration: N/A, bitrate: N/A\n  Stream #0:0: Video: vp9") == (None, False)


# --- E116: the path a clip takes -------------------------------------------------------------------------------------

@pytest.mark.parametrize("video_in, images_in, ffmpeg_in, expected", [
    (True, True, True, "native"), (True, False, False, "native"),  # a model that takes video gets the clip itself
    (False, True, True, "strips"),  # the main way otherwise
    (False, True, False, "clip"),  # no ffmpeg: the media model describes the whole clip
    (False, False, True, "clip"),  # no image-capable chat model
    (False, False, False, "clip"),
])
def test_the_path_a_clip_takes(video_in, images_in, ffmpeg_in, expected):
    assert video.route(takes_video=video_in, takes_images=images_in, has_ffmpeg=ffmpeg_in) == expected


def test_the_sound_is_transcribed_by_the_media_route_first_then_the_chat_model_else_nobody():
    assert video.listener(True, True) == "media" and video.listener(True, False) == "media"
    assert video.listener(False, True) == "chat"
    assert video.listener(False, False) is None


async def test_a_clip_bigger_than_eight_megabytes_downloads_for_strips_only_up_to_twenty():
    class Big:
        def __init__(self, size):
            self.size = size

        async def download(self, file_id):
            return io.BytesIO(b"x" * self.size)

    clip = {"kind": "video", "file_id": "v", "file_size": 15_000_000}
    with pytest.raises(PermanentMediaSkip):  # described whole it still stops at 8 MB
        await download(Big(10), clip)
    assert (await download(Big(10), clip, ceiling=video.DOWNLOAD_BYTES))[1] == b"x" * 10
    with pytest.raises(PermanentMediaSkip):  # Telegram's own limit for a bot
        await download(Big(10), {**clip, "file_size": 25_000_000}, ceiling=video.DOWNLOAD_BYTES)


# --- E116: strips from a real (tiny, generated) video ----------------------------------------------------------------

def make_clip(path, seconds, sound=True):
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=96x64:rate=2:duration={seconds}"]
    if sound:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    subprocess.run([*command, "-c:v", "mpeg4", str(path)], check=True)
    return path.read_bytes()


@needs_ffmpeg
async def test_a_short_clip_becomes_one_strip_with_its_sound(tmp_path):
    seen = await video.watch(make_clip(tmp_path / "a.mp4", 6))
    assert not seen.cut and len(seen.strips) == 1 and 5.5 < seen.total < 6.6
    sheet = Image.open(io.BytesIO(seen.strips[0].image))
    assert sheet.format == "JPEG" and sheet.size[0] == 3 * video.CELL_WIDTH  # three frames in a row
    assert seen.audio is not None and seen.audio[4:8] == b"ftyp"  # an m4a the audio input takes


@needs_ffmpeg
async def test_the_timestamps_are_burnt_into_the_frames(tmp_path):
    seen = await video.watch(make_clip(tmp_path / "a.mp4", 6, sound=False))
    sheet = Image.open(io.BytesIO(seen.strips[0].image)).convert("RGB")
    cell_height = sheet.size[1]
    corner = sheet.crop((0, 0, 60, min(30, cell_height))).getcolors(10_000)
    assert any(colour == (255, 255, 255) for _, colour in corner)  # the white of the label's text
    assert seen.audio is None  # no sound track


@needs_ffmpeg
async def test_a_longer_clip_makes_several_strips_each_covering_a_part(tmp_path):
    seen = await video.watch(make_clip(tmp_path / "long.mp4", 100, sound=False))
    assert len(seen.strips) == 2 and not seen.cut
    assert [round(s.start) for s in seen.strips] == [0, 50] and round(seen.strips[-1].end) == 100


@needs_ffmpeg
async def test_a_clip_past_the_cap_is_cut_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "MAX_STRIPS", 1)
    monkeypatch.setattr(video, "MAX_SECONDS", 80.0)
    seen = await video.watch(make_clip(tmp_path / "long.mp4", 100, sound=False))
    assert len(seen.strips) == 1 and seen.cut and seen.seconds == 80.0 and 99 < seen.total < 101


@needs_ffmpeg
async def test_a_file_ffmpeg_cannot_read_is_unwatchable(tmp_path):
    with pytest.raises(video.Unwatchable):
        await video.watch(b"this is not a video at all")


async def test_without_ffmpeg_nothing_is_watched(monkeypatch):
    monkeypatch.setattr(video, "ffmpeg", lambda: None)
    with pytest.raises(video.Unwatchable, match="ffmpeg"):
        await video.watch(b"anything")


# --- E116: kept strips count toward the media cap ----------------------------------------------------------------------

JPEG = io.BytesIO()
Image.new("RGB", (8, 8), "red").save(JPEG, "JPEG")
STRIP = JPEG.getvalue()


async def test_strips_are_saved_beside_the_media_counted_and_evicted_with_it(make_app, tmp_path):
    app = make_app()
    files = MediaFiles(tmp_path, app.store, lambda: 3 * len(STRIP))
    await app.store.record_new_message(StoredMessage_(7, {"kind": "video", "file_id": "v"}))
    kept = await files.keep_strips(GROUP, 7, [STRIP, STRIP], NOW)
    assert kept == {
        "strips": [f"media/{GROUP}/2026-01/7-strip1.jpg", f"media/{GROUP}/2026-01/7-strip2.jpg"],
        "bytes": 2 * len(STRIP),
    }
    await app.store.update_media(GROUP, 7, kept)
    assert files_of(kept) == kept["strips"]
    assert await files.read(GROUP, kept["strips"][0]) == STRIP
    assert await files.read(OWNER, kept["strips"][0]) is None  # only through its own chat
    assert (await files.usage())["bytes"] == 2 * len(STRIP) and (await files.usage())["files"] == 1
    # a second clip pushes the total past the cap: the oldest goes, its message keeps nothing but a note
    await app.store.record_new_message(StoredMessage_(8, {"kind": "video", "file_id": "w"}))
    later = await files.keep_strips(GROUP, 8, [STRIP, STRIP], NOW + timedelta(days=40))
    await app.store.update_media(GROUP, 8, later)
    assert await files.evict() == 1
    assert await files.read(GROUP, kept["strips"][0]) is None
    assert await files.read(GROUP, later["strips"][0]) == STRIP
    stored = json.loads((await app.store.get_message(GROUP, 7)).media_json)
    assert "strips" not in stored and stored["removed"] is True


async def test_strips_that_cannot_fit_are_not_kept_and_a_zero_cap_keeps_nothing(make_app, tmp_path):
    app = make_app()
    assert await MediaFiles(tmp_path, app.store, lambda: 0).keep_strips(GROUP, 1, [STRIP], NOW) == {}
    assert await MediaFiles(tmp_path, app.store, lambda: len(STRIP) - 1).keep_strips(GROUP, 1, [STRIP], NOW) == {}
    assert not list(tmp_path.glob("media/**/*.jpg"))


async def test_reconcile_removes_strips_no_message_records(make_app, tmp_path):
    app = make_app()
    files = MediaFiles(tmp_path, app.store, lambda: 10_000)
    await app.store.record_new_message(StoredMessage_(9, {"kind": "video", "file_id": "v"}))
    kept = await files.keep_strips(GROUP, 9, [STRIP], NOW)
    await files.keep_strips(GROUP, 10, [STRIP], NOW)  # saved, but the process died before it was recorded
    await app.store.update_media(GROUP, 9, kept)
    assert await files.reconcile() == 1
    assert await files.read(GROUP, kept["strips"][0]) == STRIP
    assert await files.read(GROUP, f"media/{GROUP}/2026-01/10-strip1.jpg") is None


def StoredMessage_(msg_id, media):
    from dom.models import StoredMessage

    return StoredMessage(
        chat_id=str(GROUP), role="user", visibility_scope=f"chat:{GROUP}", text="", msg_id=msg_id,
        media_json=json.dumps(media), created_at=NOW.isoformat(),
    )


# --- E116: a clip arriving, end to end --------------------------------------------------------------------------------

class Scribe:
    """A scripted media model that takes audio."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def of(self, part_type):
        """The requests that carried a part of this type."""
        return [sent for sent in self.sent if any(p["type"] == part_type for p in sent[0]["content"])]

    async def complete(self, messages, tools, **kwargs):
        self.sent.append(messages)
        return {"content": self.replies.pop(0) if self.replies else "Something happens."}


def clip_message(msg_id=5, size=15_000_000, duration=6):
    video_in = Video(file_id="clip", file_unique_id="u", width=96, height=64, duration=duration, file_size=size)
    return tg(msg_id=msg_id, text=None, video=video_in, chat_id=OWNER, sender=OWNER)


def fake_watch(monkeypatch, strips=1, audio=b"sound", total=6.0, cut=False):
    calls = []

    async def watch(clip, known_duration=None):
        calls.append((len(clip), known_duration))
        each = total / strips
        return video.Watched(
            [video.Strip(i * each, (i + 1) * each, STRIP) for i in range(strips)],
            total if not cut else 320.0, total, audio,
        )

    monkeypatch.setattr(video, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(video, "watch", watch)
    return calls


async def arrive(make_app, monkeypatch, *, chat_takes=("text", "image"), hears=True, strips=1, audio=b"sound",
                 contents=(), replies=("hello there",), size=15_000_000, total=6.0, cut=False, tmp_path=None):
    app = make_app(contents=contents)
    app.client = ChatClient(ModelEndpoint(
        base_url="https://llm.test/v1", model="m", api_key_env=None, input=chat_takes), None, app.client.http, sleep=no_sleep)
    await online(app, OWNER)
    scribe = Scribe(*replies)
    app.media_describer = MediaDescriber(scribe, SimpleNamespace(download=FakeDownload().download))
    app.bot.download = FakeDownload().download  # the one Telegram both the strips and the whole-clip path download through

    async def media_hears(client):
        return hears

    monkeypatch.setattr(app.dispatch.native, "_hears", media_hears)
    calls = fake_watch(monkeypatch, strips=strips, audio=audio, total=total, cut=cut)
    await handle_message(app, clip_message(size=size))
    await drain_dispatch(app.dispatch)
    return app, scribe, calls


def wire(app):
    return json.dumps(turn_requests(app)[0]["messages"][-1]["content"])


async def test_a_clip_for_a_model_that_sees_images_goes_as_a_strip_and_a_transcript(make_app, monkeypatch, tmp_path):
    app, scribe, calls = await arrive(make_app, monkeypatch)
    assert calls == [(len(b"fake attachment"), 6)]  # 15 MB is past the old 8 MB skip; Telegram's duration was passed on
    # the media model is asked for the transcript, from the clip's sound; the whole clip is never sent to it
    [sent] = scribe.of("input_audio")
    assert [part["type"] for part in sent[0]["content"]] == ["text", "input_audio"]
    assert sent[0]["content"][1]["input_audio"]["format"] == "m4a"
    assert not scribe.of("video_url")
    row = json.loads((await app.store.get_message(OWNER, 5)).media_json)
    assert row["watched"] == {
        "strips": 1, "spans": [[0.0, 6.0]], "seconds": 6.0, "total": 6.0, "cut": False,
        "sound": "heard", "transcript": "hello there",
    }
    assert row["bytes"] == len(STRIP)
    assert (app.home.root / row["strips"][0]).read_bytes() == STRIP  # kept beside the media files
    # the turn carries the strip as an image, and what was heard as text; never the clip itself
    seen = wire(app)
    assert '"image_url"' in seen and "video_url" not in seen and "input_audio" not in seen
    assert 'Heard: \\"hello there\\"' in seen and "shown as a frame strip" in seen
    assert "data:image/jpeg;base64," in seen


async def test_after_the_turn_the_strip_is_described_for_later_turns(make_app, monkeypatch):
    app, scribe, _ = await arrive(make_app, monkeypatch, replies=("hello there", "A man waves at a door."))
    await drain_dispatch(app.dispatch)
    assert len(scribe.sent) == 2  # the transcript, then the aged description from the kept strip
    part = scribe.sent[1][0]["content"]
    assert part[1]["type"] == "image_url" and part[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    description = json.loads((await app.store.get_message(OWNER, 5)).media_json)["description"]
    assert description == 'A man waves at a door. Heard: "hello there"'


async def test_a_chat_model_that_takes_video_gets_the_clip_and_no_strip_is_made(make_app, monkeypatch):
    app, scribe, calls = await arrive(make_app, monkeypatch, chat_takes=("text", "image", "video"), size=3_000_000)
    assert not calls and not scribe.of("input_audio")  # no strip, no transcript
    assert "video_url" in wire(app)


async def test_a_clip_too_big_to_send_whole_to_a_video_model_is_watched_as_strips_instead(make_app, monkeypatch):
    app, scribe, calls = await arrive(make_app, monkeypatch, chat_takes=("text", "image", "video"), size=15_000_000)
    assert calls and "video_url" not in wire(app) and '"image_url"' in wire(app)


async def test_no_image_model_falls_back_to_the_media_models_whole_clip(make_app, monkeypatch):
    app, scribe, calls = await arrive(make_app, monkeypatch, chat_takes=("text",), size=3_000_000, replies=("A cat.",))
    assert not calls
    [sent] = scribe.sent
    assert [part["type"] for part in sent[0]["content"]] == ["text", "video_url"]  # today's behaviour


async def test_no_ffmpeg_falls_back_to_the_media_models_whole_clip(make_app, monkeypatch):
    app = make_app()
    await online(app, OWNER)
    app.client = ChatClient(ModelEndpoint(
        base_url="https://llm.test/v1", model="m", api_key_env=None, input=("text", "image")), None,
        app.client.http, sleep=no_sleep)
    scribe = Scribe("A cat.")
    app.media_describer = MediaDescriber(scribe, SimpleNamespace(download=FakeDownload().download))
    app.bot.download = FakeDownload().download
    monkeypatch.setattr(video, "ffmpeg", lambda: None)
    await handle_message(app, clip_message(size=3_000_000))
    await drain_dispatch(app.dispatch)
    assert [part["type"] for part in scribe.sent[0][0]["content"]] == ["text", "video_url"]


async def test_the_chat_model_writes_the_transcript_when_the_media_route_cannot_hear(make_app, monkeypatch):
    app, scribe, _ = await arrive(
        make_app, monkeypatch, chat_takes=("text", "image", "audio"), hears=False, contents=("what he said",),
    )
    [transcription] = [r for r in app.script.requests if "tools" not in r and "input_audio" in json.dumps(r)]
    assert transcription["messages"][0]["content"][1]["type"] == "input_audio"
    assert not any("input_audio" in json.dumps(message) for message in scribe.sent)  # the media model never got it
    row = json.loads((await app.store.get_message(OWNER, 5)).media_json)["watched"]
    assert row["sound"] == "heard" and row["transcript"] == "what he said"


async def test_with_no_model_that_hears_the_note_says_the_sound_went_untranscribed(make_app, monkeypatch):
    app, scribe, _ = await arrive(make_app, monkeypatch, hears=False)
    assert not scribe.of("input_audio")
    row = json.loads((await app.store.get_message(OWNER, 5)).media_json)["watched"]
    assert row["sound"] == "not transcribed" and row["transcript"] is None
    assert "Its sound could not be transcribed." in wire(app)


async def test_a_clip_without_sound_says_so_and_asks_no_model_to_listen(make_app, monkeypatch):
    app, scribe, _ = await arrive(make_app, monkeypatch, audio=None)
    assert not scribe.of("input_audio")
    assert "It has no sound." in wire(app)


async def test_silence_is_noted_not_invented(make_app, monkeypatch):
    app, _, _ = await arrive(make_app, monkeypatch, replies=("(no speech)",))
    assert "There is no speech in it." in wire(app)


async def test_a_long_clip_is_summarised_part_by_part_and_joined_instead_of_attached(make_app, monkeypatch):
    app, scribe, _ = await arrive(
        make_app, monkeypatch, strips=2, total=100.0, replies=("hi all", "A man enters.", "He sits down."),
    )
    # the transcript, then each part in turn; the second part is told what came before
    assert len(scribe.sent) == 3
    assert "A man enters." in scribe.sent[2][0]["content"][0]["text"] and "part 2 of 2" in scribe.sent[2][0]["content"][0]["text"]
    row = json.loads((await app.store.get_message(OWNER, 5)).media_json)
    assert row["watched"]["strips"] == 2 and len(row["strips"]) == 2
    assert row["description"] == '0:00-0:50: A man enters. 0:50-1:40: He sits down. Heard: "hi all"'
    assert '"image_url"' not in wire(app)  # a turn can't carry several strips: the description stands in
    assert "A man enters. 0:50-1:40: He sits down." in wire(app)


async def test_a_clip_past_the_cap_says_how_much_was_looked_at(make_app, monkeypatch):
    app, _, _ = await arrive(make_app, monkeypatch, strips=1, total=600.0, cut=True)
    assert "Only the first 5:20 of 10:00 was looked at." in wire(app)


async def test_a_clip_telegram_will_not_hand_over_is_left_to_the_media_model(make_app, monkeypatch):
    app, scribe, calls = await arrive(make_app, monkeypatch, size=25_000_000, replies=("unused",))
    assert not calls and not scribe.sent  # not downloaded at all: it is over Telegram's 20 MB
    assert "watched" not in json.loads((await app.store.get_message(OWNER, 5)).media_json)


async def test_a_clip_that_cannot_be_read_falls_back_to_the_whole_clip(make_app, monkeypatch):
    app = make_app()
    await online(app, OWNER)
    app.client = ChatClient(ModelEndpoint(
        base_url="https://llm.test/v1", model="m", api_key_env=None, input=("text", "image")), None,
        app.client.http, sleep=no_sleep)
    scribe = Scribe("A shaky clip.")
    app.media_describer = MediaDescriber(scribe, SimpleNamespace(download=FakeDownload().download))
    app.bot.download = FakeDownload().download
    monkeypatch.setattr(video, "ffmpeg", lambda: "/usr/bin/ffmpeg")

    async def broken(clip, known=None):
        raise video.Unwatchable("no frames could be read")

    monkeypatch.setattr(video, "watch", broken)
    await handle_message(app, clip_message(size=3_000_000))
    await drain_dispatch(app.dispatch)
    assert [part["type"] for part in scribe.sent[0][0]["content"]] == ["text", "video_url"]


async def test_strips_gone_from_disk_leave_the_description_and_the_sound(make_app, monkeypatch):
    app, _, _ = await arrive(make_app, monkeypatch, replies=("hello there", "A man waves."))
    await drain_dispatch(app.dispatch)
    native = app.dispatch.native
    row = json.loads((await app.store.get_message(OWNER, 5)).media_json)
    native._strips.clear()
    (app.home.root / row["strips"][0]).unlink()
    assert await native._strip_images(OWNER, 5, row) == []
    assert await native.takes(row) and row["description"] == 'A man waves. Heard: "hello there"'
