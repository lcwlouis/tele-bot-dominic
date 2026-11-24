"""The OpenRouter video backup: job, poll, authenticated download and the Telegram video-note encode."""

import asyncio
import base64
import io
import wave
import shutil
import subprocess

import httpx

import pytest
from aiohttp import web
from pydantic import ValidationError

import dom.studio.cloud as cloud
from dom.config import StudioConfig, StudioFallback
from dom.studio.client import MAX_MEDIA_BYTES
from test_studio import connect, studio_server  # noqa: F401 (fixture)
from test_studio_cloud import PCM, PORTRAIT, ROUTER_KEY, VOICE, allowed, check_voice, cloud_server, configure  # noqa: F401 (fixture)
from test_traits import put
from test_usage import deliver, online, usage_rows
from tghelpers import GROUP, OWNER, tg

VIDEO = StudioFallback(provider="openrouter_video", model="heygen/heygen-avatar-6-turbo", api_key_env="ROUTER_TEST",
                       data_policy="no_training")
# As OpenRouter's /videos/models publishes it (fictional prices in the shape of the real list).
VIDEO_ENTRY = {
    "id": VIDEO.model, "supported_resolutions": ["2K", "1080p"],
    "pricing_skus": {
        "cents_per_image_input": "1", "cents_per_video_output_second_1080p": "2", "cents_per_video_output_second_2k": "14",
    },
}
ESTIMATE = 0.012  # 0.1 s at 2 cents plus 1 cent for the portrait
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A small landscape MP4 with sound, as a video model returns it."""
    if not shutil.which("ffmpeg"):
        return b"\0\0\0\x18ftypmp42fixture"
    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=854x480:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ], check=True)
    return path.read_bytes()


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(cloud, "VIDEO_POLL_SECONDS", 0)


def serve_video(cloud_server, clip, *, polls=1, status="completed", content=None, entry=VIDEO_ENTRY):
    """A job that is pending for `polls` polls, then ends in `status` and serves `content` (the clip by default)."""
    state = {"polls": 0}

    async def models(request):
        return web.json_response({"data": [entry]})

    async def catalog(request):
        assert request.query["output_modalities"] == "video"
        return web.json_response({"data": [{"id": VIDEO.model, "architecture": {
            "input_modalities": ["text", "image", "audio"], "output_modalities": ["video"],
        }}]})

    async def create(request):
        assert request.headers["authorization"] == f"Bearer {ROUTER_KEY}"
        return web.json_response({"id": "job-1", "polling_url": "https://elsewhere.example/steal", "status": "pending"},
                                 status=202)

    async def poll(request):
        assert request.headers["authorization"] == f"Bearer {ROUTER_KEY}"
        state["polls"] += 1
        if state["polls"] <= polls:
            return web.json_response({"id": "job-1", "status": "in_progress"})
        return web.json_response({"id": "job-1", "status": status, "usage": {"cost": 0.13},
                                  "unsigned_urls": ["https://elsewhere.example/content"]})

    async def download(request):
        assert request.headers["authorization"] == f"Bearer {ROUTER_KEY}" and request.query["index"] == "0"
        if content is not None:
            return await content(request)
        return web.Response(body=clip, content_type="video/mp4")

    cloud_server[2].update({"/api/v1/models": catalog, "/api/v1/videos/models": models, "/api/v1/videos": create, "/api/v1/videos/job-1": poll,
                            "/api/v1/videos/job-1/content": download})
    return state


def record_notes(app):
    sent = []

    async def send(chat_id, file, **kwargs):
        sent.append(file.data)
        return await app.bot.send_message(chat_id, "fixture video note", **kwargs)

    app.bot.send_video_note = send
    return sent


async def render(app, kind, text, outfit, chat_id=OWNER, permit=allowed):
    async def voice():
        return await app.cloud_studio.generate(app.config, "voice", text, None, chat_id, permit)

    return await app.cloud_studio.generate_video(app.config, kind, outfit, chat_id, permit, voice)


def created(cloud_server):
    """The job requests Dom submitted (the payloads of POST /videos)."""
    return [payload for path, payload in cloud_server[1] if path == "/api/v1/videos"]


def portrait(app, outfit="casual"):
    folder = app.config.home / "studio-portraits"
    folder.mkdir(exist_ok=True)
    (folder / f"{outfit}.png").write_bytes(PORTRAIT)


def test_video_backup_cannot_be_zero_data_retention():
    with pytest.raises(ValidationError, match="no video endpoint offers zero data retention"):
        StudioFallback(provider="openrouter_video", model="acme/video", api_key_env="ROUTER_TEST")
    with pytest.raises(ValidationError, match="video_fallbacks takes openrouter_video"):
        StudioConfig(video_fallbacks=(StudioFallback(provider="openrouter_tts", model="a/b", api_key_env="K"),))
    with pytest.raises(ValidationError, match="voice_fallbacks takes"):
        StudioConfig(voice_fallbacks=(VIDEO,))
    assert StudioConfig(video_fallbacks=(VIDEO,)).video_fallbacks == (VIDEO,)


@needs_ffmpeg
async def test_video_job_polls_downloads_and_makes_a_round_note(make_app, cloud_server, clip):
    app = make_app()
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,))
    portrait(app)
    serve_video(cloud_server, clip, polls=2)
    note = await render(app, "video_note", "Hey, good morning to you", "casual")
    assert note[4:8] == b"ftyp"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", "-"],
        input=note, capture_output=True, check=True,
    ).stdout.decode().split()
    assert probe == ["h264,480,480", "aac"]  # square, at most 640 px, H.264 with sound
    await asyncio.wait_for(asyncio.gather(*app.cloud_studio._reconciling), 5)
    paths = [path for path, _ in cloud_server[1] if path != "/api/v1/generation"]
    # The job's own URLs are never followed: every request goes to the fixed origin.
    assert sorted(paths[:2]) == ["/api/v1/models", "/api/v1/videos/models"]
    assert paths[2:] == ["/api/v1/audio/speech", "/api/v1/videos", "/api/v1/videos/job-1", "/api/v1/videos/job-1", "/api/v1/videos/job-1",
                     "/api/v1/videos/job-1/content"]
    [payload] = created(cloud_server)
    assert payload["provider"] == {"data_collection": "deny"}
    image, audio = payload["input_references"]
    assert image["type"] == "image_url"
    assert base64.b64decode(image["image_url"]["url"].removeprefix("data:image/png;base64,")) == PORTRAIT
    assert audio["type"] == "audio_url"
    wav = base64.b64decode(audio["audio_url"]["url"].removeprefix("data:audio/wav;base64,"))
    with wave.open(io.BytesIO(wav)) as track:
        assert track.readframes(track.getnframes()) == PCM
    assert payload["resolution"] == "1080p"
    assert not {"frame_images", "prompt", "duration", "generate_audio", "aspect_ratio"} & payload.keys()
    # The fake avatar returns a tone. The final note must have only the original silent track.
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:a:0", "-f", "s16le", "pipe:1"],
        input=note, capture_output=True, check=True,
    ).stdout
    assert decoded and not any(decoded)
    assert usage_rows(app) == [
        ("studio_voice", VOICE.model, "openrouter.ai", OWNER, 0, 0, 0.02, 1),
        ("studio_video", VIDEO.model, "openrouter.ai", OWNER, 0, 0, 0.13, 1),
    ]
    assert not await app.store.kv_get(cloud.VIDEO_JOB + "job-1")


async def test_no_avatar_or_voice_generation_without_approved_portrait(make_app, cloud_server, clip):
    app = make_app()
    legacy = VIDEO.model_copy(update={"allow_without_reference": True})
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(legacy,))
    serve_video(cloud_server, clip)
    assert await render(app, "special", "Happy birthday", "casual") is None
    assert not cloud_server[1]


@pytest.mark.parametrize("voice_fails", [True, False])
@needs_ffmpeg
async def test_cloud_video_failure_reuses_voice_or_sends_text(make_app, cloud_server, clip, voice_fails):
    app = make_app(contents=('{"video_note":{"text":"Morning!","special":true},"outfit":"casual"}',))
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), enabled=False, default_outfit="casual")
    portrait(app)
    serve_video(cloud_server, clip, status="failed")
    sent = record_notes(app)
    check_voice(app)
    if voice_fails:
        async def failed(request):
            return web.Response(status=503)

        cloud_server[2]["/api/v1/audio/speech"] = failed
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert sum(path == "/api/v1/audio/speech" for path, _ in cloud_server[1]) == 1
    assert bool(created(cloud_server)) is not voice_fails
    assert not sent
    _, usage = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert [kind for kind, _ in usage] == ([] if voice_fails else ["voice"])
    assert app.bot.of("send")[-1][2] == ("Morning!" if voice_fails else "fixture voice")


@needs_ffmpeg
@pytest.mark.parametrize("failure", ["timeout", "oversize", "mime", "redirect", "failed", "bad_id"])
async def test_video_limits(make_app, cloud_server, clip, failure):
    app = make_app()
    timeouts = app.config.studio.timeouts.model_copy(update={"video_notes": 0.3})
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), timeouts=timeouts)
    portrait(app)

    async def content(request):
        if failure == "oversize":
            return web.Response(body=b"x" * (MAX_MEDIA_BYTES + 1), content_type="video/mp4")
        if failure == "redirect":
            return web.Response(status=302, headers={"location": "/elsewhere.mp4"})
        return web.Response(body=clip, content_type="application/octet-stream")

    state = serve_video(cloud_server, clip, polls=10**6 if failure == "timeout" else 0,
                        status="failed" if failure == "failed" else "completed",
                        content=None if failure in ("timeout", "failed", "bad_id") else content)
    if failure == "bad_id":
        async def create(request):
            return web.json_response({"id": "../../key", "status": "pending"})
        cloud_server[2]["/api/v1/videos"] = create
    assert await render(app, "video_note", "Hi", "casual") is None
    paths = [path for path, _ in cloud_server[1]]
    if failure == "timeout":
        assert state["polls"] > 1 and "/api/v1/videos/job-1/content" not in paths
    elif failure in ("failed", "bad_id"):
        assert "/api/v1/videos/job-1/content" not in paths
    else:
        assert paths[-1] == "/api/v1/videos/job-1/content"
        assert "/elsewhere.mp4" not in paths
    rows = [row for row in usage_rows(app) if row[0] == "studio_video"]
    if failure == "bad_id":
        assert rows == []  # no job was accepted, so nothing can be billed
    elif failure == "timeout":
        # Accepted and unresolved: the estimate counts toward the spend cap, and the job is kept for reconciling.
        assert [row[6] for row in rows] == [ESTIMATE]
        assert await app.store.kv_get(cloud.VIDEO_JOB + "job-1")
    else:
        assert [row[6] for row in rows] == [0.13]  # the reported cost replaced the estimate


async def test_local_studio_first_then_video_backup(make_app, studio_server, cloud_server, clip):
    app = make_app(contents=('{"video_note":{"text":"Morning!"}}',))
    await connect(app, studio_server)
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,))
    serve_video(cloud_server, clip)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert studio_server[2][-1][0] == "/v1/video_note"
    assert not cloud_server[1]  # the local studio answered, so the backup is never called
    assert app.bot.of("video_note")


@needs_ffmpeg
async def test_away_studio_sends_a_cloud_video_note(make_app, cloud_server, clip):
    app = make_app(contents=('{"video_note":{"text":"Morning!"},"outfit":"casual"}',))
    # No local studio at all: the video backup alone serves the switch.
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), enabled=False, default_outfit="casual")
    portrait(app)
    serve_video(cloud_server, clip)
    sent = record_notes(app)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(sent) == 1 and sent[0][4:8] == b"ftyp"
    assert created(cloud_server)[0]["input_references"]
    _, usage = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert [kind for kind, _ in usage] == ["video_note"]


@needs_ffmpeg
@pytest.mark.parametrize("blocked", ["none", "leak", "spend", "special_group", "quota"])
async def test_video_backup_keeps_the_turn_gates(make_app, cloud_server, clip, blocked):
    text = "The fictional private passphrase is lemon waffles" if blocked == "leak" else "Happy birthday"
    special = "true" if blocked == "special_group" else "false"
    app = make_app(contents=(f'{{"video_note":{{"text":"{text}","special":{special}}}}}',))
    video = VIDEO
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(video,), enabled=False, default_outfit="casual")
    portrait(app)
    serve_video(cloud_server, clip)
    record_notes(app)
    if blocked == "leak":
        put(app.home, "private/USER.md", "The fictional private passphrase is lemon waffles")
    if blocked == "spend":
        app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.5})
        await app.dispatch.usage.record("chat", "fixture", "example.org", {"cost": 0.5})
    if blocked == "quota":
        configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(video,), enabled=False,
                  limits=app.config.studio.limits.model_copy(update={"daily": 0}))
    chat = GROUP if blocked == "special_group" else OWNER
    await online(app, chat)
    await deliver(app, tg(chat_id=chat, sender=OWNER))
    assert bool(cloud_server[1]) is (blocked == "none")  # the leak screen, caps and spend cap come first


async def test_video_note_encode_refuses_garbage():
    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg")
    with pytest.raises(ValueError):
        await asyncio.wait_for(cloud.video_note(b"not a video", b"not audio", 0.1), 30)


@needs_ffmpeg
async def test_a_job_past_the_deadline_is_reconciled_in_the_background(make_app, cloud_server, clip, monkeypatch):
    monkeypatch.setattr(cloud, "RECONCILE_POLL_SECONDS", 0.01)
    app = make_app()
    timeouts = app.config.studio.timeouts.model_copy(update={"video_notes": 0.2})
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), timeouts=timeouts)
    portrait(app)
    serve_video(cloud_server, clip, polls=10**6)
    assert await render(app, "video_note", "Hi", "casual") is None
    assert [row[6] for row in usage_rows(app) if row[0] == "studio_video"] == [ESTIMATE]
    cloud_server[2]["/api/v1/videos/job-1"] = _finished
    await asyncio.wait_for(asyncio.gather(*app.cloud_studio._reconciling), 5)
    assert [row[6] for row in usage_rows(app) if row[0] == "studio_video"] == [0.21]
    assert not await app.store.kv_get(cloud.VIDEO_JOB + "job-1")


async def _finished(request):
    return web.json_response({"id": "job-1", "status": "completed", "usage": {"cost": 0.21}})


async def test_unresolved_estimates_count_toward_the_spend_cap(make_app, cloud_server, clip):
    app = make_app()
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.1})
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,))
    portrait(app)
    await app.dispatch.usage.record("studio_video", VIDEO.model, "openrouter.ai", {"cost": 0.1}, OWNER, fallback=True)

    async def under_cap():
        return not await app.dispatch.usage.over_cap(app.config, app.bot, sleep=app.sleep)

    assert await render(app, "video_note", "Hi", "casual", 2, under_cap) is None
    assert not created(cloud_server)


def test_video_plan_requires_portrait_and_audio_and_estimates_from_track_duration():
    entry = {**VIDEO_ENTRY, "avatar": VIDEO.model, "inputs": ["image", "audio"]}
    plan = cloud.video_plan(entry)
    assert plan == {"resolution": "1080p"}
    assert cloud.video_rate(entry, plan) == 0.02
    assert cloud.video_estimate(entry, plan, 2.5) == 0.06
    with pytest.raises(cloud.VideoUnsupported, match="portrait and an audio"):
        cloud.video_plan({**entry, "inputs": ["text", "image"]})


@needs_ffmpeg
async def test_voice_and_avatar_share_the_video_deadline(make_app, cloud_server, clip, monkeypatch):
    app = make_app()
    timeouts = app.config.studio.timeouts.model_copy(update={"voice": 1, "video_notes": 0.85})
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), timeouts=timeouts)
    portrait(app)
    serve_video(cloud_server, clip, polls=0)
    create = cloud_server[2]["/api/v1/videos"]
    monkeypatch.setattr(cloud, "VIDEO_POLL_SECONDS", 0.45)

    async def delayed_voice(request):
        await asyncio.sleep(0.3)
        return web.Response(body=PCM, content_type="audio/pcm")

    async def delayed_create(request):
        await asyncio.sleep(0.22)
        return await create(request)

    cloud_server[2]["/api/v1/audio/speech"] = delayed_voice
    cloud_server[2]["/api/v1/videos"] = delayed_create
    assert await render(app, "video_note", "Hello", "casual") is None
    assert created(cloud_server)
    assert [row[6] for row in usage_rows(app) if row[0] == "studio_video"] == [ESTIMATE]


@needs_ffmpeg
async def test_avatar_uses_local_voice_after_local_video_fails(make_app, studio_server, cloud_server, clip, monkeypatch):
    app = make_app(contents=('{"video_note":{"text":"Morning!"},"outfit":"casual"}',))
    await connect(app, studio_server)
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), default_outfit="casual")
    portrait(app)
    studio_server[3]["/v1/video_note"] = 500
    voice = await cloud._opus(PCM)
    requested = []

    async def local_voice(text):
        requested.append(text)
        return voice

    monkeypatch.setattr(app.studio, "voice", local_voice)
    serve_video(cloud_server, clip)
    sent = record_notes(app)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert requested == ["Morning!"]
    assert "/api/v1/audio/speech" not in [path for path, _ in cloud_server[1]]
    assert len(sent) == 1
    assert [row[:2] for row in usage_rows(app) if row[0].startswith("studio_")] == [
        ("studio_voice", "local-studio"), ("studio_video", VIDEO.model),
    ]


async def test_a_retired_avatar_backup_is_logged_and_never_submitted(make_app, cloud_server, clip, caplog):
    app = make_app()
    retired = VIDEO.model_copy(update={"model": "heygen/avatar-iv"})
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(retired,))
    portrait(app)
    serve_video(cloud_server, clip)
    with caplog.at_level("WARNING", logger="dom.studio.cloud"):
        assert await render(app, "video_note", "Hello", "casual") is None
    assert not created(cloud_server)
    assert "heygen/avatar-iv isn't a supported lip-sync avatar" in caplog.text


async def test_aclose_stops_background_cost_checks():
    studio = cloud.CloudStudio(httpx.AsyncClient(), None)
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(3600)

    studio._track_reconcile(forever())
    await started.wait()
    await studio.aclose()
    assert not studio._reconciling
