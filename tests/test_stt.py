import io
import tomllib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from dom.config import ProviderEndpoint, load_config
from dom.llm import ChatClient
from dom.media import MediaDescriber
from dom.modalities import Modalities
from dom.stt import SttClient, transcriptions_url


class FakeDownload:
    def __init__(self, payload: bytes = b"OggS-fake-audio"):
        self.payload = payload
        self.calls: list[str] = []

    async def download(self, file_id: str):
        self.calls.append(file_id)
        return io.BytesIO(self.payload)


class FakeMediaClient:
    available = True

    def __init__(self, reply: str = "fallback transcript"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=()):
        self.calls.append(messages)
        return {"content": self.reply}


def test_transcriptions_url_handles_base_and_full_path():
    assert transcriptions_url("http://192.168.68.24:8000/v1") == "http://192.168.68.24:8000/v1/audio/transcriptions"
    assert transcriptions_url("http://192.168.68.24:8000/v1/") == "http://192.168.68.24:8000/v1/audio/transcriptions"
    assert transcriptions_url("http://192.168.68.24:8000/v1/audio/transcriptions") == (
        "http://192.168.68.24:8000/v1/audio/transcriptions"
    )


async def test_stt_client_transcribe_and_probe():
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.headers.get("authorization"), request.content))
        return httpx.Response(200, json={"text": "  hello lah  "})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = SttClient(http, "http://127.0.0.1:8000/v1", "whisper-large-v3-turbo", "tok-123", "en")
        assert await client.transcribe(b"audio-bytes", mime="audio/ogg") == "hello lah"
        assert "Answered" in await client.probe()
    assert seen[0][0] == "http://127.0.0.1:8000/v1/audio/transcriptions"
    assert seen[0][1] == "Bearer tok-123"
    assert b"whisper-large-v3-turbo" in seen[0][2] and b'name="language"' in seen[0][2]


async def test_media_describer_prefers_local_stt_and_falls_back_on_error():
    def ok_handler(request):
        return httpx.Response(200, json={"text": "local voice transcript"})

    def fail_handler(request):
        return httpx.Response(503, text="offline")

    voice = {"kind": "voice", "file_id": "v1", "file_size": 100, "mime": "audio/ogg"}
    photo = {"kind": "photo", "file_id": "p1", "file_size": 100, "mime": "image/jpeg"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as http:
        fallback = FakeMediaClient("cloud transcript")
        describer = MediaDescriber(fallback, FakeDownload(), stt=SttClient(http, "http://127.0.0.1:8000/v1"))
        assert await describer.describe(voice) == "local voice transcript"
        assert not fallback.calls
        assert await describer.describe(photo) == "cloud transcript"
        assert len(fallback.calls) == 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as http:
        fallback = FakeMediaClient("cloud fallback")
        describer = MediaDescriber(fallback, FakeDownload(), stt=SttClient(http, "http://127.0.0.1:8000/v1"))
        assert await describer.describe(voice) == "cloud fallback"
        assert len(fallback.calls) == 1


async def test_video_note_hear_uses_local_stt_and_falls_back_to_media(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(-1001, "approved")
    cloud_calls = []

    def cloud_handler(request):
        cloud_calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "cloud clip transcript"}}]})

    media_ep = ProviderEndpoint(base_url="https://openrouter.ai/api/v1", model="media-audio", input=["audio"])

    def ok_handler(request):
        assert b"clip.m4a" in request.content
        return httpx.Response(200, json={"text": "video note speech"})

    def fail_handler(request):
        raise httpx.ConnectError("mac asleep", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(cloud_handler)) as cloud_http:
        app.media_describer = MediaDescriber(ChatClient(media_ep, "fake-key", cloud_http), FakeDownload())
        async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as http:
            app.stt = SttClient(http, "http://127.0.0.1:8000/v1")
            sound, text = await app.dispatch.native._hear(-1001, b"m4a-bytes", background=False)
            assert (sound, text) == ("heard", "video note speech")
            assert not cloud_calls

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as http:
            app.stt = SttClient(http, "http://127.0.0.1:8000/v1")
            sound, text = await app.dispatch.native._hear(-1001, b"m4a-bytes", background=False)
            assert (sound, text) == ("heard", "cloud clip transcript")
            assert len(cloud_calls) == 1


async def test_modality_probe_expires_after_probe_ttl(make_app):
    app = make_app()
    wall = [datetime(2026, 11, 28, 0, 0, tzinfo=timezone.utc)]
    mono = [100.0]
    modalities = Modalities(app.store, now=lambda: mono[0], wall=lambda: wall[0])
    ep = ProviderEndpoint(base_url="http://192.168.68.24:8080/v1", model="default")

    await modalities.remember_probe(ep, False)
    assert await modalities.lookup(ep) == frozenset()

    # After PROBE_TTL + in-memory TTL, the cached probe expires so the home server gets re-probed.
    wall[0] += timedelta(seconds=Modalities.PROBE_TTL + 1)
    mono[0] += Modalities.TTL + 1
    assert await modalities.lookup(ep) is None


async def test_modality_in_memory_cache_never_outlives_probe_ttl(make_app):
    app = make_app()
    wall = [datetime(2026, 11, 28, 0, 0, tzinfo=timezone.utc)]
    mono = [100.0]
    modalities = Modalities(app.store, now=lambda: mono[0], wall=lambda: wall[0])
    ep = ProviderEndpoint(base_url="http://192.168.68.24:8080/v1", model="default")

    await modalities.remember_probe(ep, False)
    # Advance to 10s before PROBE_TTL expires and warm the in-memory cache.
    wall[0] += timedelta(seconds=Modalities.PROBE_TTL - 10)
    mono[0] += Modalities.PROBE_TTL - 10
    assert await modalities.lookup(ep) == frozenset()
    # Advancing 11s (far less than Modalities.TTL=600) must expire the in-memory cache too.
    wall[0] += timedelta(seconds=11)
    mono[0] += 11
    assert await modalities.lookup(ep) is None


async def test_voice_fallback_works_with_real_single_use_media_request_context(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(-1001, "approved")

    def fail_handler(request):
        return httpx.Response(503, text="offline")

    voice = {"kind": "voice", "file_id": "v1", "file_size": 100, "mime": "audio/ogg"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as http:
        fallback = FakeMediaClient("cloud fallback ok")
        describer = MediaDescriber(fallback, FakeDownload(), stt=SttClient(http, "http://127.0.0.1:8000/v1"))
        # Pass the real single-use @asynccontextmanager from Dispatcher._media_request
        result = await describer.describe(voice, request=app.dispatch._media_request(-1001))
        assert result == "cloud fallback ok"
        assert len(fallback.calls) == 1


async def test_stt_only_video_summary_preserves_transcript_without_media_client(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(-1001, "approved")
    app.media_describer = MediaDescriber(None, FakeDownload())
    app.dispatch.native._strips[(-1001, 42)] = [b"fake-strip-jpeg"]
    media = {
        "kind": "video_note",
        "watched": {
            "strips": 1, "spans": [[0.0, 5.0]], "seconds": 5.0, "total": 5.0,
            "cut": False, "missing": [], "sound": "heard", "transcript": "meet at seven",
        },
    }
    summary = await app.dispatch.native._summarise(-1001, 42, media, app.dispatch._media_request(-1001))
    assert summary is not None
    assert "meet at seven" in summary


async def test_unread_media_catch_up_respects_bot_media_and_excludes_bot_documents(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(-1001, "approved")
    from dom.models import StoredMessage

    await app.store.record_new_message(StoredMessage(
        chat_id="-1001", msg_id=10, sender_user_id=333, sender_name="Bot", is_bot=True,
        role="user", visibility_scope="chat:-1001", text="[voice]",
        media_json='{"kind":"voice","file_id":"v1"}', created_at="2026-11-28T00:00:00+00:00",
    ))
    await app.store.record_new_message(StoredMessage(
        chat_id="-1001", msg_id=11, sender_user_id=333, sender_name="Bot", is_bot=True,
        role="user", visibility_scope="chat:-1001", text="[document]",
        media_json='{"kind":"document","file_id":"d1"}', created_at="2026-11-28T00:00:01+00:00",
    ))
    assert await app.store.get_unread_media_to_describe(-1001, 10, include_bots=False) == []
    got = await app.store.get_unread_media_to_describe(-1001, 10, include_bots=True)
    assert [msg_id for msg_id, _ in got] == [10]
