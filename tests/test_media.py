import asyncio
import base64
import io
import json
import xml.etree.ElementTree as ET

import pytest
from aiogram.types import PhotoSize, Sticker, Voice

import dom.dispatch
from dom.media import MediaDescriber
from dom.models import StoredMessage
from dom.presence import parse_habits
from dom.store.store import format_chat_window_xml
from dom.telegram.convert import to_incoming
from dom.telegram.handlers import handle_message
from tghelpers import GROUP, drain_dispatch, tg


class FakeMediaClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        if self.error:
            raise self.error
        return {"content": "A fox & a crescent moon."}


class FakeDownload:
    def __init__(self, wait=None):
        self.downloads = []
        self.wait = wait
        self.started = asyncio.Event()

    async def download(self, file_id):
        self.downloads.append(file_id)
        self.started.set()
        if self.wait:
            await self.wait.wait()
        return io.BytesIO(b"fake attachment")


def photo_message(msg_id=1, **kwargs):
    large = PhotoSize(file_id="large", file_unique_id="large-unique", width=20, height=20, file_size=100)
    small = PhotoSize(file_id="small", file_unique_id="small-unique", width=10, height=10, file_size=50)
    return tg(msg_id=msg_id, text=None, photo=[large, small], **kwargs)


@pytest.fixture
def media_app(make_app):
    async def setup(error=None, wait=None):
        app = make_app()
        await app.store.set_chat_auth_status(GROUP, "approved")
        await app.dispatch.presence.go_online(GROUP, parse_habits(""))
        bot, client = FakeDownload(wait), FakeMediaClient(error)
        app.media_describer = MediaDescriber(client, bot)
        return app, bot, client

    return setup


async def test_photo_described_and_stored(media_app):
    ready = asyncio.Event()
    app, bot, client = await media_app(wait=ready)
    message = photo_message(caption="look")
    await handle_message(app, message)
    await bot.started.wait()
    stored = (await app.store.get_recent_chat_window(GROUP))[0]
    assert json.loads(stored.media_json) == {
        "kind": "photo", "file_id": "large", "file_size": 100, "mime": "image/jpeg", "description": None,
    }
    assert not client.calls and not app.script.requests
    ready.set()
    await drain_dispatch(app.dispatch)
    stored = (await app.store.get_recent_chat_window(GROUP))[0]
    assert json.loads(stored.media_json)["description"] == "A fox & a crescent moon."
    assert bot.downloads == ["large"]
    part = client.calls[0][0][0]["content"][1]
    assert part == {"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64," + base64.b64encode(b"fake attachment").decode(),
    }}
    assert "<media kind=\"photo\">A fox &amp; a crescent moon.</media>" in (
        app.script.requests[0]["messages"][-1]["content"]
    )
    await handle_message(app, message)
    await drain_dispatch(app.dispatch)
    assert bot.downloads == ["large"]


async def test_media_failure_records_without_description(media_app, caplog):
    app, _, client = await media_app(RuntimeError("fake provider failure"))
    await handle_message(app, photo_message())
    await drain_dispatch(app.dispatch)
    stored = (await app.store.get_recent_chat_window(GROUP))[0]
    media = json.loads(stored.media_json)
    assert media["kind"] == "photo" and media["description"] is None
    assert len(client.calls) == len(app.script.requests) == 1
    assert "media description failed" in caplog.text


async def test_oversize_skipped():
    bot, client = FakeDownload(), FakeMediaClient()
    describer = MediaDescriber(client, bot)
    media = {"kind": "photo", "file_id": "fake-photo", "file_size": 20_000_001, "mime": "image/jpeg"}
    assert await describer.describe(media) is None
    assert not bot.downloads and not client.calls


def test_window_renders_media():
    def message(msg_id, media):
        return StoredMessage(
            chat_id=str(GROUP), msg_id=msg_id, text="caption", visibility_scope=f"chat:{GROUP}",
            media_json=json.dumps(media), created_at="2026-01-01T12:00:00Z",
        )

    rendered = format_chat_window_xml(str(GROUP), [
        message(1, {"kind": "photo", "file_id": "hidden-file-id", "description": "Fox < moon & stars"}),
        message(2, {"type": "voice"}),
    ])
    photo, voice = ET.fromstring(rendered).findall("./msg/media")
    assert photo.attrib == {"kind": "photo"}
    assert photo.text == "Fox < moon & stars"
    assert voice.attrib == {"kind": "voice"} and voice.text is None
    assert "hidden-file-id" not in rendered


async def test_voice_uses_input_audio_part():
    bot, client = FakeDownload(), FakeMediaClient()
    voice = Voice(file_id="fake-voice", file_unique_id="voice-unique", duration=2, mime_type="audio/ogg")
    media = to_incoming(tg(text=None, voice=voice)).media
    result = await MediaDescriber(client, bot).describe(media)
    assert result == "A fox & a crescent moon."
    text, audio = client.calls[0][0][0]["content"]
    assert text == {"type": "text", "text": (
        "Describe this for a friend reading a group chat: what it shows/says, any text, the vibe. "
        "Voice/audio: transcribe, then one line on tone. Max 80 words."
    )}
    assert audio == {"type": "input_audio", "input_audio": {
        "data": base64.b64encode(b"fake attachment").decode(), "format": "ogg",
    }}


async def test_media_does_not_reach_model_if_approval_changes_during_download(media_app):
    ready = asyncio.Event()
    app, bot, client = await media_app(wait=ready)
    await handle_message(app, photo_message())
    await bot.started.wait()
    await app.store.set_chat_auth_status(GROUP, "ignored")
    ready.set()
    await drain_dispatch(app.dispatch)
    assert not client.calls and not app.script.requests


async def test_turn_does_not_cancel_a_slow_media_description(media_app, monkeypatch):
    monkeypatch.setattr(dom.dispatch, "MEDIA_WAIT_SECONDS", 0)
    ready, sent = asyncio.Event(), asyncio.Event()
    app, bot, client = await media_app(wait=ready)
    send_message = app.bot.send_message

    async def send(*args, **kwargs):
        sent.set()
        return await send_message(*args, **kwargs)

    app.bot.send_message = send
    await handle_message(app, photo_message())
    await bot.started.wait()
    await asyncio.wait_for(sent.wait(), timeout=1)
    assert not client.calls
    ready.set()
    await drain_dispatch(app.dispatch)
    assert len(client.calls) == 1


def test_sticker_emoji_is_preserved():
    sticker = Sticker(
        file_id="fake-sticker", file_unique_id="sticker-unique", type="regular",
        width=10, height=10, is_animated=False, is_video=False, emoji="🦊",
    )
    assert to_incoming(tg(text=None, sticker=sticker)).media == {
        "kind": "sticker", "file_id": "fake-sticker", "file_size": None, "mime": "image/webp", "emoji": "🦊",
    }


async def test_animated_sticker_uses_image_preview():
    preview = PhotoSize(file_id="fake-preview", file_unique_id="preview-unique", width=10, height=10)
    sticker = Sticker(
        file_id="fake-animation", file_unique_id="animation-unique", type="regular",
        width=20, height=20, is_animated=True, is_video=False, emoji="🦊", thumbnail=preview,
    )
    media = to_incoming(tg(text=None, sticker=sticker)).media
    assert media["file_id"] == "fake-animation"
    assert media["emoji"] == "🦊"
    bot, client = FakeDownload(), FakeMediaClient()
    assert await MediaDescriber(client, bot).describe(media) == "A fox & a crescent moon."
    assert bot.downloads == ["fake-preview"]
    part = client.calls[0][0][0]["content"][1]
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_new_message_restarts_debounce_while_dispatch_waits_for_media(media_app):
    allow_media = asyncio.Event()
    app, bot, _ = await media_app(wait=allow_media)
    delays = []

    async def sleep(_):
        ready = asyncio.Event()
        delays.append(ready)
        await ready.wait()

    app.dispatch.sleep = sleep
    await handle_message(app, photo_message())
    await bot.started.wait()
    delays[0].set()
    await asyncio.sleep(0)
    first_dispatch = next(task for task in app.dispatch._tasks if task not in app.dispatch._media_tasks[GROUP])
    await handle_message(app, tg(msg_id=2, text="here's another message"))
    allow_media.set()
    await first_dispatch
    assert not app.script.requests
    delays[1].set()
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1
    assert "here's another message" in app.script.requests[0]["messages"][-1]["content"]
