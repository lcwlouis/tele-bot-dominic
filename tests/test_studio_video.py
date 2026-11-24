"""The OpenRouter video backup: job, poll, authenticated download and the Telegram video-note encode."""

import asyncio
import shutil
import subprocess

import pytest
from aiohttp import web
from pydantic import ValidationError

import dom.studio.cloud as cloud
from dom.config import StudioConfig, StudioFallback
from dom.studio.client import MAX_MEDIA_BYTES
from test_studio import connect, studio_server  # noqa: F401 (fixture)
from test_studio_cloud import PORTRAIT, ROUTER_KEY, allowed, cloud_server, configure  # noqa: F401 (fixture)
from test_traits import put
from test_usage import deliver, online, usage_rows
from tghelpers import GROUP, OWNER, tg

VIDEO = StudioFallback(provider="openrouter_video", model="x-ai/grok-imagine-video-1.5-lite", api_key_env="ROUTER_TEST",
                       data_policy="no_training")
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


def serve_video(cloud_server, clip, *, polls=1, status="completed", content=None):
    """A job that is pending for `polls` polls, then ends in `status` and serves `content` (the clip by default)."""
    state = {"polls": 0}

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

    cloud_server[2].update({"/api/v1/videos": create, "/api/v1/videos/job-1": poll,
                            "/api/v1/videos/job-1/content": download})
    return state


def record_notes(app):
    sent = []

    async def send(chat_id, file, **kwargs):
        sent.append(file.data)
        return await app.bot.send_message(chat_id, "fixture video note", **kwargs)

    app.bot.send_video_note = send
    return sent


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
    configure(app, cloud_server, video_fallbacks=(VIDEO,))
    portrait(app)
    serve_video(cloud_server, clip, polls=2)
    note = await app.cloud_studio.generate(app.config, "video_note", "Hey, good morning to you", "casual", OWNER, allowed)
    assert note[4:8] == b"ftyp"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", "-"],
        input=note, capture_output=True, check=True,
    ).stdout.decode().split()
    assert probe == ["h264,480,480", "aac"]  # square, at most 640 px, H.264 with sound
    paths = [path for path, _ in cloud_server[1]]
    # The job's own URLs are never followed: every request goes to the fixed origin.
    assert paths == ["/api/v1/videos", "/api/v1/videos/job-1", "/api/v1/videos/job-1", "/api/v1/videos/job-1",
                     "/api/v1/videos/job-1/content"]
    payload = cloud_server[1][0][1]
    assert payload["provider"] == {"data_collection": "deny"}
    assert payload["frame_images"][0]["frame_type"] == "first_frame"
    assert payload["frame_images"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (payload["aspect_ratio"], payload["resolution"], payload["duration"], payload["generate_audio"]) == (
        "1:1", "480p", 4, True)
    assert '"Hey, good morning to you"' in payload["prompt"]
    assert usage_rows(app)[-1] == ("studio_video", VIDEO.model, "openrouter.ai", OWNER, 0, 0, 0.13, 1)


@pytest.mark.parametrize(("supports", "permit", "has_portrait", "called"), [
    (True, False, True, True), (True, False, False, False), (False, False, True, False), (False, True, False, True),
])
async def test_video_reference_rule(make_app, cloud_server, clip, supports, permit, has_portrait, called):
    app = make_app()
    video = VIDEO.model_copy(update={"supports_reference": supports, "allow_without_reference": permit})
    configure(app, cloud_server, video_fallbacks=(video,))
    if has_portrait:
        portrait(app)
    serve_video(cloud_server, clip, status="failed")  # stops after the job, so ffmpeg isn't needed here
    assert await app.cloud_studio.generate(app.config, "special", "Happy birthday", "casual", OWNER, allowed) is None
    if not called:
        assert not cloud_server[1]  # a stranger's face is worse than no video
        return
    payload = cloud_server[1][0][1]
    assert ("frame_images" in payload) is (supports and has_portrait)
    assert payload["prompt"].startswith("The person in the first frame" if supports and has_portrait else "A friendly")


@pytest.mark.parametrize("failure", ["timeout", "oversize", "mime", "redirect", "failed", "bad_id"])
async def test_video_limits(make_app, cloud_server, clip, failure):
    app = make_app()
    timeouts = app.config.studio.timeouts.model_copy(update={"video_notes": 0.3})
    configure(app, cloud_server, video_fallbacks=(VIDEO,), timeouts=timeouts)
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
    assert await app.cloud_studio.generate(app.config, "video_note", "Hi", "casual", OWNER, allowed) is None
    paths = [path for path, _ in cloud_server[1]]
    if failure == "timeout":
        assert state["polls"] > 1 and "/api/v1/videos/job-1/content" not in paths
    elif failure in ("failed", "bad_id"):
        assert "/api/v1/videos/job-1/content" not in paths
    else:
        assert paths[-1] == "/api/v1/videos/job-1/content"
        assert "/elsewhere.mp4" not in paths
    assert usage_rows(app)[-1][0] == "studio_video"  # a failed job is still recorded


async def test_local_studio_first_then_video_backup(make_app, studio_server, cloud_server, clip):
    app = make_app(contents=('{"video_note":{"text":"Morning!"}}',))
    await connect(app, studio_server)
    configure(app, cloud_server, video_fallbacks=(VIDEO,))
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
    configure(app, cloud_server, video_fallbacks=(VIDEO,), enabled=False, default_outfit="casual")
    portrait(app)
    serve_video(cloud_server, clip)
    sent = record_notes(app)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(sent) == 1 and sent[0][4:8] == b"ftyp"
    assert cloud_server[1][0][1]["frame_images"]  # the approved outfit's portrait went as the first frame
    _, usage = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert [kind for kind, _ in usage] == ["video_note"]


@pytest.mark.parametrize("blocked", ["none", "leak", "spend", "special_group", "quota"])
async def test_video_backup_keeps_the_turn_gates(make_app, cloud_server, clip, blocked):
    text = "The fictional private passphrase is lemon waffles" if blocked == "leak" else "Happy birthday"
    special = "true" if blocked == "special_group" else "false"
    app = make_app(contents=(f'{{"video_note":{{"text":"{text}","special":{special}}}}}',))
    video = VIDEO.model_copy(update={"allow_without_reference": True})
    configure(app, cloud_server, video_fallbacks=(video,), enabled=False)
    serve_video(cloud_server, clip)
    record_notes(app)
    if blocked == "leak":
        put(app.home, "private/USER.md", "The fictional private passphrase is lemon waffles")
    if blocked == "spend":
        app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.5})
        await app.dispatch.usage.record("chat", "fixture", "example.org", {"cost": 0.5})
    if blocked == "quota":
        configure(app, cloud_server, video_fallbacks=(video,), enabled=False,
                  limits=app.config.studio.limits.model_copy(update={"daily": 0}))
    chat = GROUP if blocked == "special_group" else OWNER
    await online(app, chat)
    await deliver(app, tg(chat_id=chat, sender=OWNER))
    assert bool(cloud_server[1]) is (blocked == "none")  # the leak screen, caps and spend cap come first


def test_video_seconds_stay_short():
    assert cloud.video_seconds("Hi there") == 4
    assert cloud.video_seconds(" ".join(["word"] * 12)) == 6
    assert cloud.video_seconds(" ".join(["word"] * 40)) == 8


async def test_video_note_encode_refuses_garbage():
    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg")
    with pytest.raises(ValueError):
        await asyncio.wait_for(cloud.video_note(b"not a video"), 30)
