import asyncio
import base64
import io
import shutil
import socket
import struct
import wave

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from pydantic import ValidationError

from dom.config import Config, StudioConfig, StudioFallback
from dom.studio.cloud import MAX_AUDIO_BYTES, CloudStudio, speech_pcm
from test_studio import connect, studio_server
from test_traits import put
from test_usage import deliver, online, usage_rows
from tghelpers import GROUP, OWNER, tg

ROUTER_KEY = "fixture-router-key"
PCM = b"\0\0" * 2400
PORTRAIT = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jW2QAAAAASUVORK5CYII="
)
VOICE = StudioFallback(provider="openrouter_tts", model="google/gemini-3.8-flash-lite-tts", api_key_env="ROUTER_TEST")
IMAGE = StudioFallback(provider="openrouter_image", model="google/gemini-2.5-flash-image", api_key_env="ROUTER_TEST")
GEMINI = StudioFallback(provider="gemini_tts", model="gemini-2.5-flash-preview-tts", api_key_env="GEMINI_TEST")


def wav_with_c2pa(pcm=PCM):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sound:
        sound.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        sound.writeframes(pcm)
    wav = buffer.getvalue()
    marked = wav + b"C2PA" + struct.pack("<I", 8) + b"metadata"
    return marked[:4] + struct.pack("<I", len(marked) - 8) + marked[8:]


class LoopbackProvider(httpx.AsyncHTTPTransport):
    def __init__(self, port):
        super().__init__(trust_env=False)
        self.port = port

    async def handle_async_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="localhost", port=self.port)
        return await super().handle_async_request(request)


@pytest_asyncio.fixture
async def cloud_server(monkeypatch):
    seen, overrides = [], {}
    monkeypatch.setenv("ROUTER_TEST", ROUTER_KEY)
    monkeypatch.setenv("GEMINI_TEST", "fixture-gemini-key")

    async def respond(request):
        payload = await request.json() if request.method == "POST" else None
        seen.append((request.path, payload))
        if request.path in overrides:
            return await overrides[request.path](request)
        if request.path == "/api/v1/audio/speech":
            assert request.headers["authorization"] == f"Bearer {ROUTER_KEY}"
            return web.Response(body=PCM, content_type="audio/pcm", headers={"X-Generation-Id": "gen-fixture"})
        if request.path == "/api/v1/generation":
            assert request.query["id"] == "gen-fixture"
            return web.json_response({"data": {"total_cost": 0.02}})
        if request.path == "/api/v1/images":
            assert request.headers["authorization"] == f"Bearer {ROUTER_KEY}"
            return web.json_response({"data": [{"media_type": "image/png", "b64_json": base64.b64encode(PORTRAIT).decode()}],
                                      "usage": {"cost": 0.03}})
        assert request.path.endswith(":generateContent")
        assert request.headers["x-goog-api-key"] == "fixture-gemini-key"
        return web.json_response({"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/wav", "data": base64.b64encode(wav_with_c2pa()).decode(),
        }}]}}], "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 12}})

    server = web.Application()
    server.router.add_route("*", "/{path:.*}", respond)
    runner = web.AppRunner(server)
    await runner.setup()
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        await web.SockSite(runner, listener).start()
        async with httpx.AsyncClient(transport=LoopbackProvider(listener.getsockname()[1]), follow_redirects=True) as http:
            yield http, seen, overrides
        await runner.cleanup()


def configure(app, cloud_server, voice=(), images=(), **updates):
    app.cloud_studio = CloudStudio(cloud_server[0], app.dispatch.usage)
    studio = app.config.studio.model_copy(update={
        "enabled": True, "voice": True, "images": True, "special": True, "video_notes": True,
        "voice_fallbacks": voice, "image_fallbacks": images, **updates,
    })
    app.config = app.config.model_copy(update={"studio": studio})


async def allowed():
    return True


def check_voice(app):
    async def send(chat_id, file, **kwargs):
        assert file.data.startswith(b"OggS") and b"OpusHead" in file.data
        return await app.bot.send_message(chat_id, "fixture voice", **kwargs)
    app.bot.send_voice = send


@pytest.mark.parametrize("local_status", [200, 503, 500])
async def test_local_first_then_openrouter_speech(make_app, studio_server, cloud_server, local_status):
    app = make_app(contents=('{"bubbles":["hey"],"voice":"A quiet hello"}',))
    await connect(app, studio_server)
    configure(app, cloud_server, voice=(VOICE,))
    if local_status != 200:
        if not shutil.which("ffmpeg"):
            pytest.skip("requires ffmpeg")
        studio_server[3]["/v1/voice"] = local_status
        check_voice(app)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert studio_server[2][-1][0] == "/v1/voice"
    if local_status == 200:
        assert cloud_server[1] == []
        assert app.bot.of("voice")
    else:
        assert cloud_server[1] == [
            ("/api/v1/audio/speech", {"model": VOICE.model, "input": "A quiet hello",
             "voice": "Achird", "response_format": "pcm", "provider": {"zdr": True}}),
            ("/api/v1/generation", None),
        ]
        assert ("studio_voice", VOICE.model, "openrouter.ai", OWNER, 0, 0, 0.02, 1) in usage_rows(app)


async def test_gemini_paid_only_and_second_in_chain(make_app, cloud_server):
    app = make_app()
    configure(app, cloud_server, voice=(GEMINI,))
    assert await app.cloud_studio.generate(app.config, "voice", "Hello", None, OWNER, allowed) is None
    assert not cloud_server[1]

    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg")

    async def refused(_):
        return web.Response(status=503)

    cloud_server[2]["/api/v1/audio/speech"] = refused
    configure(app, cloud_server, voice=(VOICE, GEMINI.model_copy(update={"paid": True})))
    data = await app.cloud_studio.generate(app.config, "voice", "Hello", None, OWNER, allowed)
    assert data.startswith(b"OggS") and b"OpusHead" in data
    speech, gemini = cloud_server[1]
    assert speech[0] == "/api/v1/audio/speech"
    assert gemini[0] == f"/v1beta/models/{GEMINI.model}:generateContent"
    assert gemini[1]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Achird"
    assert usage_rows(app)[-1] == ("studio_voice", GEMINI.model, "generativelanguage.googleapis.com", OWNER, 9, 12, 0, 1)


def test_wav_chunk_stripping_preserves_pcm_markers():
    pcm = b"\x00\x00C2PA\x00\x00"
    assert speech_pcm(wav_with_c2pa(pcm)) == pcm
    assert speech_pcm(pcm) == pcm
    with pytest.raises(ValueError):
        speech_pcm(wav_with_c2pa(pcm)[:45])


@pytest.mark.parametrize(("supports", "permit", "has_portrait", "called"), [
    (True, False, True, True), (True, False, False, False), (False, False, True, False), (False, True, True, True),
])
async def test_photo_reference_policy(make_app, cloud_server, supports, permit, has_portrait, called):
    app = make_app()
    image = IMAGE.model_copy(update={"supports_reference": supports, "allow_without_reference": permit})
    configure(app, cloud_server, images=(image,))
    path = app.config.home / "studio-portraits"
    path.mkdir()
    if has_portrait:
        (path / "casual.png").write_bytes(PORTRAIT)
    data = await app.cloud_studio.generate(app.config, "photo", "A sunny park", "casual", OWNER, allowed)
    if not called:
        assert data is None and not cloud_server[1]
        return
    assert data == PORTRAIT
    [(route, payload)] = cloud_server[1]
    assert route == "/api/v1/images" and payload["provider"] == {"zdr": True}
    if supports:
        url = payload["input_references"][0]["image_url"]["url"]
        assert url == "data:image/png;base64," + base64.b64encode(PORTRAIT).decode()
    else:
        assert "input_references" not in payload
    assert usage_rows(app)[-1] == ("studio_image", IMAGE.model, "openrouter.ai", OWNER, 0, 0, 0.03, 1)


@pytest.mark.parametrize("blocked", ["leak", "quota", "spend", "special_group"])
async def test_cloud_uses_existing_turn_gates(make_app, cloud_server, blocked):
    action = '{"video_note":{"text":"Happy birthday","special":true}}' if blocked == "special_group" else (
        '{"bubbles":["hey"],"voice":"The fictional private passphrase is lemon waffles"}'
    )
    app = make_app(contents=(action,))
    configure(app, cloud_server, voice=(VOICE,))
    if blocked == "leak":
        put(app.home, "private/USER.md", "The fictional private passphrase is lemon waffles")
    if blocked == "quota":
        configure(app, cloud_server, voice=(VOICE,), limits=app.config.studio.limits.model_copy(update={"daily": 0}))
    if blocked == "spend":
        app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.5})
        await app.dispatch.usage.record("chat", "fixture", "example.org", {"cost": 0.5})
    chat = GROUP if blocked == "special_group" else OWNER
    await online(app, chat)
    await deliver(app, tg(chat_id=chat, sender=OWNER))
    assert not cloud_server[1]


@pytest.mark.parametrize("failure", ["timeout", "oversize", "mime", "redirect"])
async def test_cloud_http_limits(make_app, cloud_server, failure):
    app = make_app()
    timeouts = app.config.studio.timeouts.model_copy(update={"voice": 0.1})
    configure(app, cloud_server, voice=(VOICE,), timeouts=timeouts)

    async def refuse(request):
        if failure == "timeout":
            await asyncio.sleep(0.2)
        elif failure == "oversize":
            return web.Response(body=b"x" * (MAX_AUDIO_BYTES + 1), content_type="audio/pcm")
        elif failure == "redirect":
            return web.Response(status=307, headers={"location": "/unexpected"})
        return web.Response(body=PCM, content_type="application/octet-stream")

    cloud_server[2]["/api/v1/audio/speech"] = refuse
    assert await app.cloud_studio.generate(app.config, "voice", "Hello", None, OWNER, allowed) is None
    assert [path for path, _ in cloud_server[1]] == ["/api/v1/audio/speech"]


def test_studio_keys_are_paired_with_one_https_origin(tmp_path):
    studio = StudioConfig(voice_fallbacks=(VOICE,), image_fallbacks=(IMAGE,))
    valid = {"base_url": "https://openrouter.ai/api/v1", "model": "test", "api_key_env": VOICE.api_key_env}
    Config(home=tmp_path, studio=studio, models={"chat": valid})
    with pytest.raises(ValidationError, match="HTTPS origin"):
        Config(home=tmp_path, studio=studio, models={"chat": {**valid, "base_url": "https://elsewhere.example/v1"}})
    with pytest.raises(ValidationError, match="HTTPS origin"):
        Config(home=tmp_path, studio=studio.model_copy(update={"voice_fallbacks": (
            GEMINI.model_copy(update={"api_key_env": VOICE.api_key_env}),
        )}))


async def test_away_studio_advertises_photo_and_sends_approved_outfit(make_app, cloud_server):
    app = make_app(contents=('{"bubbles":["hey"],"image":"A sunny park"}',))
    configure(app, cloud_server, images=(IMAGE,), default_outfit="casual")
    path = app.config.home / "studio-portraits"
    path.mkdir()
    (path / "casual.png").write_bytes(PORTRAIT)
    sent = []

    async def send(chat_id, file, **kwargs):
        sent.append(file.data)
        return await app.bot.send_message(chat_id, "fixture photo", **kwargs)

    app.bot.send_photo = send
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert sent == [PORTRAIT]
    assert cloud_server[1][0][1]["input_references"]
    _, usage = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert [kind for kind, _ in usage] == ["photo"]


async def test_failed_special_video_uses_cloud_voice(make_app, cloud_server, studio_server):
    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg")
    app = make_app(contents=('{"video_note":{"text":"Happy birthday","special":true}}',))
    await connect(app, studio_server)
    configure(app, cloud_server, voice=(VOICE,))
    check_voice(app)
    studio_server[3]["/v1/video_note"] = 503
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert studio_server[2][-1][0] == "/v1/video_note"
    assert cloud_server[1][0][0] == "/api/v1/audio/speech"
    _, usage = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert [kind for kind, _ in usage] == ["voice"]
