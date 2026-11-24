import json

from dom.studio.policy import UNAVAILABLE_NOTE
import socket
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from pydantic import ValidationError

from dom.actions import Actions
from dom.config import StudioConfig
from dom.studio.client import AWAY_SECONDS, StudioClient
from dom.studio.profile import sync_profile
from dom.telegram.handlers import handle_message
from dom.telegram.owner import set_global_preset
from test_guard import FakeJev
from test_traits import put
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg, turn_requests

TOKEN = "fictional-studio-token"


@pytest_asyncio.fixture
async def studio_server():
    seen = []
    failures = {}

    async def respond(request):
        assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
        seen.append((request.path, await request.json() if request.method == "POST" else None))
        if request.path in failures:
            return web.Response(status=failures[request.path])
        if request.path == "/v1/health":
            return web.json_response({"workers_loaded": ["voice"], "available_bytes": 24 * 1024**3})
        mime = {
            "/v1/voice": "audio/ogg", "/v1/video_note": "video/mp4", "/v1/image": "image/png",
        }.get(request.path, "image/png")
        return web.Response(body=b"fake media", content_type=mime)

    server = web.Application()
    server.router.add_route("*", "/v1/{path:.*}", respond)
    runner = web.AppRunner(server)
    await runner.setup()
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        site = web.SockSite(runner, listener)
        await site.start()
        port = listener.getsockname()[1]
        async with httpx.AsyncClient(trust_env=False) as http:
            yield StudioConfig(enabled=True, voice=True, video_notes=True, images=True, special=True,
                               base_url=f"http://localhost:{port}"), http, seen, failures
        await runner.cleanup()


async def connect(app, server):
    config, http, _, _ = server
    app.config = app.config.model_copy(update={"studio": config})
    app.studio = StudioClient(config, TOKEN, http)
    await app.studio.refresh()

    def sender(kind):
        async def send(chat_id, file, **options):
            assert file.data == b"fake media"
            app.bot._next_id += 1
            app.bot.calls.append((kind, chat_id, file.filename, options))
            return SimpleNamespace(message_id=app.bot._next_id)
        return send

    app.bot.send_voice = sender("voice")
    app.bot.send_video_note = sender("video_note")
    app.bot.send_photo = sender("photo")


@pytest.mark.parametrize(("action", "route", "kind", "payload"), [
    ({"voice": "Good morning"}, "/v1/voice", "voice", {"text": "Good morning"}),
    ({"video_note": {"text": "Good morning"}, "outfit": "unknown"}, "/v1/video_note", "video_note",
     {"text": "Good morning", "special": False, "outfit": "casual"}),
    ({"video_note": {"text": "Happy birthday", "special": True}}, "/v1/video_note", "video_note",
     {"text": "Happy birthday", "special": True, "outfit": "casual"}),
    ({"image": "A sunny park", "outfit": "formal"}, "/v1/image", "photo",
     {"scene": "A sunny park", "outfit": "formal"}),
])
async def test_owner_turn_sends_after_bubbles_and_records_media(make_app, studio_server, action, route, kind, payload):
    app = make_app(contents=(json.dumps({"bubbles": ["hey"], **action}),))
    await connect(app, studio_server)
    put(app.home, "presets/portrait.md", "---\noutfits: [casual, formal]\n---\nHello.")
    await set_global_preset(app, "portrait", "Owner")
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert studio_server[2] == [("/v1/health", None), (route, payload)]
    assert [call[0] for call in app.bot.calls if call[0] != "action"] == ["send", kind]
    request = turn_requests(app)[0]
    assert 'Optional action "voice"' in request["messages"][1]["content"]
    remaining = await app.studio_quota.remaining(app.config.studio, "owner_dm", OWNER, app.dispatch.now())
    assert remaining == {}
    _, rows = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert rows[0][0] == ("special" if payload.get("special") else kind)
    latest = (await app.store.get_recent_chat_window(OWNER, 1))[0]
    content = payload.get("text") or payload["scene"]
    assert content in latest.text
    assert json.loads(latest.media_json)["kind"] == kind


async def test_away_studio_does_not_advertise_media(make_app, studio_server):
    app = make_app(contents=('{"bubbles":["hey"],"voice":"A quiet hello","image":"A sunny park"}',))
    studio_server[3]["/v1/health"] = 503
    await connect(app, studio_server)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert studio_server[2] == [("/v1/health", None)]
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert 'Optional action "voice"' not in content
    assert UNAVAILABLE_NOTE in content  # else Dom imitates his past media from the window as text
    assert [call[2] for call in app.bot.of("send")] == ["hey", "A quiet hello"]


async def test_a_spent_media_limit_says_not_now_and_fake_media_bubbles_are_dropped(make_app, studio_server):
    app = make_app(contents=(
        json.dumps({"bubbles": ["hey"], "voice": "A quiet hello"}),
        json.dumps({"bubbles": ["one pic coming up", '[photo] the view\n\n<media kind="photo"></media>', "landed?"]}),
    ))
    await connect(app, studio_server)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    await deliver(app, tg(chat_id=OWNER, sender=OWNER, msg_id=2, text="send a pic"))
    second = turn_requests(app)[1]["messages"][1]["content"]
    assert 'Optional action' not in second and UNAVAILABLE_NOTE in second
    assert [call[2] for call in app.bot.of("send")][-2:] == ["one pic coming up", "landed?"]


async def test_failed_video_uses_voice_when_available(make_app, studio_server):
    app = make_app(contents=('{"video_note":{"text":"A quiet hello"}}',))
    await connect(app, studio_server)
    studio_server[3]["/v1/video_note"] = 500
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert studio_server[2][-1] == ("/v1/voice", {"text": "A quiet hello"})
    assert app.bot.of("voice") and not app.bot.of("send")


@pytest.mark.parametrize(("chat", "action", "jev_score"), [
    (OWNER, {"voice": "A fictional phrase from the owner's private notes"}, None),
    (GROUP, {"video_note": {"text": "It is 7319"}, "image": "A photo with the same code"}, 3.0),
    (OWNER, {"private": True, "voice": "Hello"}, None),
    (GROUP, {"video_note": {"text": "Happy birthday", "special": True}}, None),
])
async def test_private_media_stays_off_the_wire(make_app, studio_server, chat, action, jev_score):
    app = make_app(contents=(json.dumps({"bubbles": ["hey"], **action}),))
    await connect(app, studio_server)
    put(app.home, "private/USER.md", "A fictional phrase from the owner's private notes\nThe key is 7319.\n")
    if jev_score is not None:
        app.config = app.config.model_copy(update={"leak_check": True})
        app.dispatch.jev = FakeJev(jev_score)
        original = app.dispatch.jev.decide

        async def decide(state, questions):
            if "7319" in state.partition("<outgoing_message>")[2]:
                return await original(state, questions)
            return {"leak": {"type": "score", "score": 0}}
        app.dispatch.jev.decide = decide
    await online(app, chat)
    await deliver(app, tg(chat_id=chat, sender=OWNER if chat == OWNER else FRIEND))
    assert studio_server[2] == [("/v1/health", None)]
    assert [call[2] for call in app.bot.of("send", chat)] == ["hey"]


async def test_owner_dm_media_skips_the_model_leak_check(make_app, studio_server):
    app = make_app(contents=(json.dumps({"bubbles": ["morning pic"], "image": "Dom at his desk with kopi"}),))
    await connect(app, studio_server)
    app.config = app.config.model_copy(update={"leak_check": True})
    app.dispatch.jev = FakeJev(3.0)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert not app.dispatch.jev.scored
    assert studio_server[2][-1][0] == "/v1/image"


async def test_health_cache_and_connection_backoff():
    clock = [0.0]
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if len(calls) == 2:
            raise httpx.ConnectError("fixture offline")
        return httpx.Response(200, json={})

    config = StudioConfig(enabled=True, base_url="https://studio.example.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = StudioClient(config, TOKEN, http, lambda: clock[0])
        await client.refresh()
        await client.refresh()
        assert client.ready and calls == ["/v1/health"]
        assert await client.voice("Hi") is None
        assert not client.ready and client.status.startswith("away since")
        await client.refresh()
        assert await client.voice("Hi again") is None
        assert len(calls) == 2
        clock[0] += AWAY_SECONDS
        await client.refresh()
        assert client.ready and calls[-1] == "/v1/health"


async def test_global_reservation_is_shared_and_scope_limits_stay_distinct(make_app, studio_server):
    app = make_app()
    await connect(app, studio_server)
    config = StudioConfig(enabled=True, base_url="https://studio.example.org", voice=True, video_notes=True, images=True,
                          special=True, limits={"daily": 1})
    quota = app.studio_quota
    now = app.dispatch.now()
    assert "special" not in await quota.remaining(config, "friend_dm", FRIEND, now)
    async with quota.reserve(config, "owner_dm", OWNER, "voice", now) as accepted:
        assert accepted
        assert not await quota.remaining(config, "owner_dm", OWNER, now)
        assert not await quota.remaining(config, "group", GROUP, now)
    assert (await quota.remaining(config, "group", GROUP, now))["video_note"] == 1


async def test_profile_follows_everywhere_and_throttles_updates(make_app, studio_server):
    app = make_app()
    await connect(app, studio_server)
    config = app.config.studio.model_copy(update={"profile_photo_follows_preset": True})
    app.config = app.config.model_copy(update={"studio": config})
    put(app.home, "presets/one.md", "---\noutfits: [casual]\n---\nHello.")
    put(app.home, "presets/two.md", "---\noutfits: [formal]\n---\nHello.")
    photos = []

    async def set_photo(*, photo):
        photos.append(photo.photo.data)

    app.bot.set_my_profile_photo = set_photo
    await set_global_preset(app, "one", "Owner")
    await sync_profile(app)
    await set_global_preset(app, "two", "Owner")
    await sync_profile(app)
    assert photos == [b"fake media"]
    assert studio_server[2][-1] == ("/v1/portrait/casual", None)
    # The default interval is two weeks: a day later nothing changes, two weeks later the new outfit shows.
    await app.store.kv_set("studio:profile_at", (app.dispatch.now() - timedelta(days=1)).isoformat())
    await sync_profile(app)
    assert photos == [b"fake media"]
    await app.store.kv_set("studio:profile_at", (app.dispatch.now() - timedelta(days=14)).isoformat())
    await sync_profile(app)
    assert photos == [b"fake media", b"fake media"]
    assert studio_server[2][-1] == ("/v1/portrait/formal", None)


async def test_studio_command_is_owner_only(make_app, studio_server):
    app = make_app()
    await connect(app, studio_server)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/studio"))
    await online(app, FRIEND)
    await handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/studio"))
    await drain_dispatch(app.dispatch)
    assert not app.bot.of("send", FRIEND)
    reply = app.bot.of("send", OWNER)[0][2]
    assert "Voice notes: available" in reply and "Profile photo: off" in reply
    # E46: availability only; workers, memory and outage times stay on the dashboard.
    assert "memory" not in reply.lower() and "worker" not in reply.lower() and "since" not in reply


@pytest.mark.parametrize("bad", [
    {"voice": 1}, {"voice": "x" * 601}, {"image": "x" * 301},
    {"video_note": {"text": "x", "special": "true"}}, {"video_note": {"text": "x" * 601}},
    {"video_note": {"text": "x", "extra": 1}}, {"outfit": "x" * 65},
])
def test_studio_action_fields_are_strict_and_bounded(bad):
    with pytest.raises(ValidationError):
        Actions.model_validate(bad)


async def test_a_slow_render_fails_only_that_request_and_a_missing_portrait_pauses_nothing():
    clock = [0.0]

    def respond(request):
        if request.url.path == "/v1/voice":
            raise httpx.ReadTimeout("slow render")
        if request.url.path.startswith("/v1/portrait/"):
            return httpx.Response(503, json={"error": "portrait-unavailable"})
        return httpx.Response(200, json={})

    config = StudioConfig(enabled=True, base_url="https://studio.example.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = StudioClient(config, TOKEN, http, lambda: clock[0])
        await client.refresh()
        assert await client.voice("Hi") is None
        assert client.ready and client.status == "on"
        assert await client.portrait("default") is None
        assert client.ready and client.status == "on"


def test_video_note_text_caps():
    import pydantic

    from dom.actions import SPECIAL_TEXT_MAX, VIDEO_TEXT_MAX, VideoNote

    VideoNote(text="x" * VIDEO_TEXT_MAX)
    VideoNote(text="x" * SPECIAL_TEXT_MAX, special=True)
    for bad in ({"text": "x" * (VIDEO_TEXT_MAX + 1)}, {"text": "x" * (SPECIAL_TEXT_MAX + 1), "special": True}):
        with pytest.raises(pydantic.ValidationError):
            VideoNote(**bad)


async def test_no_media_note_at_all_when_no_kind_is_switched_on(make_app, studio_server):
    app = make_app(contents=('{"bubbles":["hey"]}',))
    await connect(app, studio_server)
    off = app.config.studio.model_copy(update={"voice": False, "video_notes": False, "images": False, "special": False})
    app.config = app.config.model_copy(update={"studio": off})
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert UNAVAILABLE_NOTE not in content and "Optional action" not in content


async def test_a_photo_written_as_text_is_sent_as_the_real_photo(make_app, studio_server):
    # Live on Dominic Dev: offered "image", DeepSeek Flash wrote '[photo] a scene <media kind="photo"></media>' instead.
    app = make_app(contents=(json.dumps(
        {"bubbles": ["pic again ah", '[photo] A sunny park\n\n<media kind="photo"></media>', "landed?"]}),))
    await connect(app, studio_server)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [(route, payload)] = [call for call in studio_server[2] if call[0] != "/v1/health"]
    assert route == "/v1/image" and payload["scene"] == "A sunny park"
    assert [call[2] for call in app.bot.of("send")] == ["pic again ah", "landed?"]
    assert app.bot.of("photo")


async def test_voice_written_as_text_is_never_resent_as_text_when_the_studio_is_away(make_app, studio_server):
    app = make_app(contents=(json.dumps({"bubbles": ["hey", "[voice] a quiet hello"]}),))
    studio_server[3]["/v1/health"] = 503
    await connect(app, studio_server)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert [call[2] for call in app.bot.of("send")] == ["hey"]


async def test_a_photo_written_as_text_is_not_made_when_photos_were_not_offered(make_app, studio_server):
    app = make_app(contents=(json.dumps({"bubbles": ["ok", "[photo] A sunny park"]}),))
    await connect(app, studio_server)
    app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update={"images": False})})
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER, text="send a pic"))
    assert "Optional action \"image\"" not in turn_requests(app)[0]["messages"][1]["content"]
    assert "/v1/image" not in [route for route, _ in studio_server[2]]
    assert [call[2] for call in app.bot.of("send")] == ["ok"]


@pytest.mark.parametrize("bubbles,image,texts", [
    (["[voice]", "[photo] a park", "hi"], "a park", ["hi"]),  # an empty marker doesn't stop the scan
    ([{"text": "[photo] a park", "delete_after": 2}, "ok"], None, ["ok"]),  # a vanishing bubble stays text, dropped
])
def test_media_written_as_text_edge_cases(bubbles, image, texts):
    from dom.actions import Actions
    actions = Actions.model_validate({"bubbles": bubbles})
    assert actions.image == image and [b.text for b in actions.bubbles] == texts
