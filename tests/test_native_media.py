"""v0.8.4 W1B: Dom sees the real media. Capabilities, prompt placement, lazy descriptions, kept files, the look before
sending, and /fresh."""

import asyncio
import base64
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from aiogram.types import PhotoSize

from dom import model_lists, modalities, turn_log
from dom import window as chat_window
from dom.config import Config, ModelEndpoint, ProviderEndpoint
from dom.llm import ChatClient
from dom.media import MediaDescriber
from dom.media_files import MediaFiles, extension, file_id_of
from dom.models import StoredMessage
from dom.modalities import Attachment, Modalities, prepare, probe_image
from dom.presence import parse_habits
from dom.prompt import build_messages
from dom.telegram.handlers import handle_message
from dom.telegram.owner import fresh_start
from test_media import FakeDownload, FakeMediaClient, photo_message
from test_studio import connect, studio_server  # noqa: F401  (the studio fixture)
from test_studio_cloud import cloud_server  # noqa: F401  (the cloud fixture)
from test_usage import deliver, online
from tghelpers import FINAL, FRIEND, GROUP, OWNER, ModelScript, drain_dispatch, no_sleep, tg, turn_requests

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
SEEING = ("text", "image", "audio")


def endpoint(**kw):
    return ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env=None, **kw)


def see(app, inputs=SEEING):
    """Give the app a chat route whose model takes `inputs`, over the same scripted transport."""
    app.client = ChatClient(endpoint(input=inputs), None, app.client.http, sleep=no_sleep)
    return app.client


# --- capabilities ---------------------------------------------------------------------------------------------------

def test_openrouter_input_modalities_are_read_from_the_list():
    payload = {"data": [
        {"id": "a/vision", "architecture": {"input_modalities": ["text", "image", "file", "audio"]}},
        {"id": "a/plain", "architecture": {"input_modalities": ["text"]}},
        {"id": "a/unsaid"},
    ]}
    listed = {item.id: item.input for item in model_lists.parse(payload)}
    assert listed == {"a/vision": ("text", "image", "audio"), "a/plain": ("text",), "a/unsaid": None}


async def test_a_cached_list_answers_and_the_config_override_beats_it(make_app):
    app = make_app()
    listed = model_lists.parse({"data": [
        {"id": "m", "architecture": {"input_modalities": ["text", "image"]}},
    ]})
    await model_lists.remember(app.store, "https://llm.test/v1", None, listed)
    caps = Modalities(app.store)
    assert await caps.accepts(ProviderEndpoint(base_url="https://llm.test/v1", model="m")) == {"image"}
    # a model the list doesn't name, or names without input types, is text-only
    assert await caps.accepts(ProviderEndpoint(base_url="https://llm.test/v1", model="other")) == frozenset()
    override = ProviderEndpoint(base_url="https://llm.test/v1", model="m", input=("text", "audio"))
    assert await caps.accepts(override) == {"audio"}
    assert await caps.lookup(ProviderEndpoint(base_url="https://llm.test/v1", model="other")) is None


async def test_unknown_means_text_only_and_a_probe_result_is_cached_per_address_and_model(make_app):
    app = make_app()
    caps = Modalities(app.store)
    here = ProviderEndpoint(base_url="https://llm.test/v1", model="m")
    assert await caps.accepts(here) == frozenset() and await caps.lookup(here) is None
    await caps.remember_probe(here, True)
    assert await caps.accepts(here) == {"image"}
    assert await caps.lookup(ProviderEndpoint(base_url="https://llm.test/v1", model="m2")) is None
    assert await caps.lookup(ProviderEndpoint(base_url="https://other.test/v1", model="m")) is None
    await caps.reset(here)
    assert await caps.lookup(here) is None


class Answering:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.sent = content, error, []

    async def complete(self, messages, tools, **kwargs):
        self.sent.append(messages)
        if self.error:
            raise self.error
        return {"content": self.content}


def refusal(status):
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    return httpx.HTTPStatusError("no", request=request, response=httpx.Response(status, request=request))


async def test_probe_sends_a_generated_image_and_reads_the_answer():
    client = Answering("Red.")
    assert await probe_image(client) is True
    part = client.sent[0][0]["content"][1]["image_url"]["url"]
    assert part.startswith("data:image/png;base64,") and base64.b64decode(part.split(",")[1]).startswith(b"\x89PNG")
    assert await probe_image(Answering("I see a landscape.")) is False
    assert await probe_image(Answering(error=refusal(400))) is False  # the server turned the image down
    assert await probe_image(Answering(error=refusal(503))) is None  # nothing learned: nothing cached
    assert await probe_image(Answering(error=httpx.ConnectError("down"))) is None


async def test_a_photo_arriving_for_an_unknown_model_probes_in_the_background_not_on_the_reply(make_app):
    app = make_app(contents=("Red",))
    app.client = ChatClient(endpoint(), None, app.client.http, sleep=no_sleep)  # no input info at all
    assert await app.dispatch.native.accepted() == frozenset()
    assert not app.script.requests  # asking never calls the model
    assert not await app.dispatch.native.takes({"kind": "photo", "file_id": "f", "file_size": 10})
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1  # the one probe, with the generated image
    assert await app.dispatch.native.accepted() == {"image"}
    # and it is not run again for a model it has already learned
    assert await app.dispatch.native.takes({"kind": "photo", "file_id": "f", "file_size": 10})
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1


async def test_the_owner_can_probe_again_and_a_configured_input_is_left_alone(make_app):
    app = make_app(contents=("blue", "Red"))
    app.client = ChatClient(endpoint(), None, app.client.http, sleep=no_sleep)
    first = await app.dispatch.native.reprobe()
    assert first[0]["accepts"] == [] and first[0]["source"] == "probe"
    second = await app.dispatch.native.reprobe()
    assert second[0]["accepts"] == ["image"]
    pinned = see(app, ("text",))
    again = await app.dispatch.native.reprobe()
    assert again[0]["source"] == "config" and len(app.script.requests) == 2 and pinned.inputs == frozenset()


# --- attachments on the wire ----------------------------------------------------------------------------------------

def user_message(**kw):
    return {"role": "user", "content": "the window", **kw}


async def test_attachments_follow_the_unchanged_text_and_never_enter_a_prompt_hash(make_app):
    app = make_app()
    plain = build_messages(app.home, "owner_dm", OWNER, [OWNER], "<chat_window></chat_window>", "note")
    withmedia = build_messages(app.home, "owner_dm", OWNER, [OWNER], "<chat_window></chat_window>", "note")
    withmedia[-1]["attachments"] = [Attachment("image", "image/png", PNG, "message 4, photo from Fran")]
    wire, native = await prepare(withmedia, frozenset({"image"}))
    assert native and wire[0] == plain[0]  # the system prefix is untouched
    parts = wire[1]["content"]
    assert parts[0] == {"type": "text", "text": plain[1]["content"]}  # so is the text the host's cache keyed on
    assert parts[1] == {"type": "text", "text": "[message 4, photo from Fran]"}
    assert parts[2]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    # hashing or logging the prompt sees a small stub, not the bytes
    assert base64.b64encode(PNG).decode() not in json.dumps(withmedia)
    assert withmedia[-1]["attachments"][0]["bytes"] == len(PNG)


async def test_audio_and_video_parts_and_the_modality_the_model_lacks():
    messages = [user_message(attachments=[
        Attachment("audio", "audio/ogg", b"ogg", "voice"),
        Attachment("video", "video/mp4", b"mp4", "clip", describe=lambda: asyncio.sleep(0, "A cat waves.")),
    ])]
    wire, native = await prepare(messages, frozenset({"audio"}))
    parts = wire[0]["content"]
    assert native
    assert parts[2] == {"type": "input_audio", "input_audio": {"data": base64.b64encode(b"ogg").decode(), "format": "ogg"}}
    assert "video_url" not in json.dumps(parts)
    assert parts[0]["text"].endswith("<attached_media>\n[clip] A cat waves.\n</attached_media>")  # described instead


async def test_a_model_that_takes_nothing_gets_descriptions_and_the_turn_log_says_so():
    trace = turn_log.TurnTrace()
    messages = [user_message(attachments=[
        Attachment("image", "image/png", PNG, "message 4, photo", describe=lambda: asyncio.sleep(0, "A fox.")),
        Attachment("image", "image/png", PNG, "message 5, photo"),
    ])]
    with turn_log.track(trace):
        wire, native = await prepare(messages, frozenset())
    assert not native
    assert wire[0]["content"] == (
        "the window\n\n<attached_media>\n[message 4, photo] A fox.\n[message 5, photo] could not be shown to you\n</attached_media>"
    )
    assert "attachments" not in wire[0] and "media described" in trace.steps


async def test_a_refused_request_is_sent_again_with_everything_described():
    posts = []

    def serve(request):
        body = json.loads(request.content)
        posts.append(body)
        if "image_url" in json.dumps(body["messages"]):
            return httpx.Response(400, json={"error": "images unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    trace = turn_log.TurnTrace()
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        client = ChatClient(endpoint(), None, http, sleep=no_sleep)
        client.inputs = frozenset({"image"})
        messages = [user_message(attachments=[
            Attachment("image", "image/png", PNG, "photo", describe=lambda: asyncio.sleep(0, "A fox.")),
        ])]
        with turn_log.track(trace):
            assert (await client.complete(messages, []))["content"] == "ok"
    assert len(posts) == 2
    assert posts[1]["messages"][0]["content"].endswith("[photo] A fox.\n</attached_media>")
    assert "media refused" in trace.steps


async def test_other_errors_are_not_mistaken_for_a_refused_modality():
    def serve(request):
        return httpx.Response(401, json={"error": "bad key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        client = ChatClient(endpoint(), None, http, sleep=no_sleep)
        client.inputs = frozenset({"image"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete([user_message(attachments=[Attachment("image", "image/png", PNG, "p")])], [])


# --- the turn --------------------------------------------------------------------------------------------------------

async def seeing_app(make_app, inputs=SEEING, *, contents=(), chat=GROUP):
    app = make_app(contents=contents)
    await app.store.set_chat_auth_status(chat, "approved")
    await app.dispatch.presence.go_online(chat, parse_habits(""))
    see(app, inputs)
    bot, describer = FakeDownload(), FakeMediaClient()
    app.bot.download = bot.download  # the one bot both the describer and the prefetch download through
    app.media_describer = MediaDescriber(describer, bot)
    return app, bot, describer


async def test_a_photo_goes_to_the_model_itself_with_no_description_call(make_app):
    app, bot, describer = await seeing_app(make_app)
    await deliver(app, photo_message(caption="look"))
    assert not describer.calls  # nothing was described
    stored = (await app.store.get_recent_chat_window(GROUP))[0]
    assert json.loads(stored.media_json)["description"] is None
    assert bot.downloads == ["large"]  # downloaded once, on arrival
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text" and "<media kind=\"photo\"></media>" in content[0]["text"]
    assert content[-1]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(b"fake attachment").decode()


async def test_a_text_only_route_still_gets_the_media_models_description(make_app):
    app, bot, describer = await seeing_app(make_app, inputs=("text",))
    await deliver(app, photo_message(caption="look"))
    assert len(describer.calls) == 1
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert isinstance(content, str) and "A fox &amp; a crescent moon." in content


async def test_a_route_that_takes_images_but_not_audio_describes_the_voice_note(make_app):
    app, bot, describer = await seeing_app(make_app, inputs=("text", "image"))
    from aiogram.types import Voice

    voice = tg(msg_id=2, text=None, voice=Voice(file_id="v", file_unique_id="vu", duration=2, mime_type="audio/ogg", file_size=10))
    await deliver(app, voice)
    assert len(describer.calls) == 1 and isinstance(turn_requests(app)[0]["messages"][1]["content"], str)


async def test_only_the_newest_media_up_to_the_limits_is_attached(make_app):
    app, bot, describer = await seeing_app(make_app)
    for msg_id in (1, 2, 3):
        await handle_message(app, photo_message(msg_id=msg_id))
    await drain_dispatch(app.dispatch)
    content = turn_requests(app)[-1]["messages"][1]["content"]
    shown = [part for part in content if part.get("type") == "text" and part["text"].startswith("[message")]
    assert [part["text"][:11] for part in shown] == ["[message 2,", "[message 3,"]  # media_input_images = 2, newest
    assert sum(part.get("type") == "image_url" for part in content) == 2


async def test_media_over_the_size_limit_is_described_instead(make_app):
    app, bot, describer = await seeing_app(make_app)
    app.config = app.config.model_copy(update={"media_input_max_mb": 0.00005})  # 50 bytes; the photo says 100
    await deliver(app, photo_message())
    assert len(describer.calls) == 1 and isinstance(turn_requests(app)[0]["messages"][1]["content"], str)


async def test_triage_gets_a_description_even_when_the_chat_model_sees(make_app):
    app, bot, describer = await seeing_app(make_app)
    message = tg(msg_id=1, text=None, photo=[PhotoSize(file_id="large", file_unique_id="u", width=2, height=2, file_size=10)])
    await handle_message(app, message)
    await drain_dispatch(app.dispatch)
    assert not describer.calls
    msg = SimpleNamespace(chat_id=GROUP, msg_id=1, media={"kind": "photo", "file_id": "large", "file_size": 10})
    await app.dispatch._describe_for_triage(SimpleNamespace(trace=turn_log.TurnTrace()), msg)
    assert len(describer.calls) == 1
    assert json.loads((await app.store.get_message(GROUP, 1)).media_json)["description"]
    await app.dispatch._describe_for_triage(SimpleNamespace(trace=turn_log.TurnTrace()), msg)
    assert len(describer.calls) == 1  # once


async def test_catch_up_describes_only_what_the_model_cant_see(make_app):
    app, bot, describer = await seeing_app(make_app, inputs=("text", "image"))
    app.dispatch._media_budget = lambda chat_id: True
    for msg_id, media in ((1, {"kind": "photo", "file_id": "p", "file_size": 5}),
                          (2, {"kind": "voice", "file_id": "v", "file_size": 5, "mime": "audio/ogg"})):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(GROUP), msg_id=msg_id, sender_user_id=FRIEND, sender_name="Fran", is_bot=False,
            visibility_scope=app.store.visibility_for(GROUP), text="", created_at="2026-01-01T12:00:00+00:00",
            media_json=json.dumps({**media, "description": None}),
        ))
    await app.dispatch._describe_unread_media(GROUP)
    assert len(describer.calls) == 1  # the voice note
    assert json.loads((await app.store.get_message(GROUP, 2)).media_json)["description"]
    assert json.loads((await app.store.get_message(GROUP, 1)).media_json)["description"] is None


async def unread_photos(app, ids):
    for msg_id in ids:
        await app.store.record_new_message(StoredMessage(
            chat_id=str(GROUP), msg_id=msg_id, sender_user_id=FRIEND, sender_name="Fran", is_bot=False,
            visibility_scope=app.store.visibility_for(GROUP), text="", created_at="2026-01-01T12:00:00+00:00",
            media_json=json.dumps({"kind": "photo", "file_id": f"p{msg_id}", "file_size": 5, "description": None}),
        ))


async def test_catch_up_describes_the_photos_that_wont_fit_the_native_limits(make_app):
    app, bot, describer = await seeing_app(make_app)
    app.dispatch._media_budget = lambda chat_id: True
    await unread_photos(app, (1, 2, 3))  # the model takes the newest two photos; the oldest would reach catch-up empty
    await app.dispatch._describe_unread_media(GROUP)
    assert len(describer.calls) == 1
    assert json.loads((await app.store.get_message(GROUP, 1)).media_json)["description"]
    for msg_id in (2, 3):
        assert json.loads((await app.store.get_message(GROUP, msg_id)).media_json)["description"] is None


async def test_the_memory_pass_gets_descriptions_for_media_in_its_batch_only(make_app):
    app, bot, describer = await seeing_app(make_app)
    app.dispatch._media_budget = lambda chat_id: True
    await unread_photos(app, (1,))
    await app.store.advance_memory_cursor(GROUP, 1)  # the first photo is already past the cursor
    await unread_photos(app, (2,))
    await app.dispatch._memory_pass(GROUP)
    assert len(describer.calls) == 1  # photo 2 only: it is the one the pass reads
    assert json.loads((await app.store.get_message(GROUP, 2)).media_json)["description"]
    assert json.loads((await app.store.get_message(GROUP, 1)).media_json)["description"] is None
    asked = [r for r in app.script.requests if "tools" not in r][-1]
    assert "A fox &amp; a crescent moon." in json.dumps(asked["messages"])


async def test_the_memory_pass_describes_nothing_over_the_spend_cap(make_app):
    app, bot, describer = await seeing_app(make_app)
    app.dispatch._media_budget = lambda chat_id: True
    app.dispatch.model_allowed = lambda chat_id: asyncio.sleep(0, False)
    await unread_photos(app, (1,))
    await app.dispatch._memory_pass(GROUP)
    assert not describer.calls


async def test_a_photo_that_cannot_be_loaded_is_described_instead(make_app):
    app, bot, describer = await seeing_app(make_app)

    async def broken(file_id):
        raise OSError("down")

    app.bot.download = broken
    describing = MediaDescriber(describer, SimpleNamespace(download=FakeDownload().download))  # the media route still can
    app.media_describer = describing
    await deliver(app, photo_message(caption="look"))
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert isinstance(content, str) and "A fox &amp; a crescent moon." in content and "couldn't be loaded" not in content


async def test_when_nothing_can_load_or_describe_a_photo_the_window_says_so(make_app):
    app, bot, describer = await seeing_app(make_app)

    async def broken(file_id):
        raise OSError("down")

    app.bot.download = broken
    app.media_describer = MediaDescriber(FakeMediaClient(error=RuntimeError("no")), SimpleNamespace(download=broken))
    await deliver(app, photo_message(caption="look"))
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert isinstance(content, str) and '<media kind="photo">(photo couldn\'t be loaded)</media>' in content


# --- kept media ---------------------------------------------------------------------------------------------------------

def sent(kind, file_id="F1"):
    field = {"photo": [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=file_id)]}.get(kind, SimpleNamespace(file_id=file_id))
    return SimpleNamespace(message_id=1, **{kind: field})


def test_file_ids_and_extensions():
    assert file_id_of(sent("photo"), "photo") == "F1" and file_id_of(sent("voice"), "voice") == "F1"
    assert file_id_of(SimpleNamespace(message_id=1), "photo") is None
    assert extension("photo", b"\xff\xd8\xff") == "jpg" and extension("photo", PNG) == "png"
    assert extension("voice", b"OggS") == "ogg" and extension("video_note", b"") == "mp4"


async def assistant_media(app, chat_id, msg_id, media, at="2026-01-01T12:00:00+00:00"):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), msg_id=msg_id, is_bot=True, role="assistant",
        visibility_scope=app.store.visibility_for(chat_id), text="[photo] x", media_json=json.dumps(media), created_at=at,
    ))


async def test_a_kept_file_is_atomic_private_and_read_only_through_its_own_chat(make_app, tmp_path):
    app = make_app()
    files = app.media_files
    fields = await files.keep(OWNER, 7, "photo", PNG, datetime(2026, 3, 9, tzinfo=timezone.utc))
    assert fields == {"file": f"media/{OWNER}/2026-03/7.png", "bytes": len(PNG)}
    path = tmp_path / fields["file"]
    assert path.read_bytes() == PNG and path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700 and (tmp_path / "media").stat().st_mode & 0o777 == 0o700
    assert not list(path.parent.glob(".part-*"))
    assert await files.read(OWNER, fields["file"]) == PNG
    assert await files.read(FRIEND, fields["file"]) is None  # another chat can't read it
    assert await files.read(OWNER, f"media/{OWNER}/../../config.toml") is None
    assert await files.read(OWNER, "memory/MEMORY.md") is None


async def test_files_no_message_records_are_removed_at_startup(make_app, tmp_path):
    app = make_app()
    at = datetime(2026, 3, 9, tzinfo=timezone.utc)
    kept = await app.media_files.keep(OWNER, 1, "photo", PNG, at)
    await assistant_media(app, OWNER, 1, {"kind": "photo", "scene": "x", **kept})
    orphan = await app.media_files.keep(OWNER, 2, "photo", PNG, at)  # saved, then the process died before the row
    stray = tmp_path / "media" / str(OWNER) / "2026-03" / ".part-abc"
    stray.write_bytes(b"half")
    assert await app.media_files.reconcile() == 2
    assert (tmp_path / kept["file"]).exists()
    assert not (tmp_path / orphan["file"]).exists() and not stray.exists()
    assert (await app.media_files.usage())["files"] == 1
    assert await app.media_files.reconcile() == 0


async def test_a_failed_save_leaves_no_half_written_file(make_app, tmp_path, monkeypatch):
    app = make_app()
    monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        await app.media_files.keep(OWNER, 1, "photo", PNG, datetime(2026, 3, 9, tzinfo=timezone.utc))
    assert not [p for p in (tmp_path / "media").rglob("*") if p.is_file()]


async def test_past_the_cap_the_oldest_files_go_and_their_file_ids_stay(make_app, tmp_path):
    app = make_app()
    app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update={"keep_cap_mb": 1})})
    big = b"x" * 450_000
    for index, day in enumerate(("2026-01-01", "2026-01-02", "2026-01-03")):
        at = datetime.fromisoformat(f"{day}T12:00:00+00:00")
        fields = await app.media_files.keep(OWNER, index + 1, "voice", big, at)
        await assistant_media(app, OWNER, index + 1, {"kind": "voice", "text": f"hi {index}", "file_id": f"F{index}", **fields},
                              at=at.isoformat())
    assert (await app.media_files.usage())["files"] == 3
    assert await app.media_files.evict() == 1  # 1.35 MB over a 1 MB cap: the oldest goes
    kept = await app.store.kept_media()
    assert sorted(row["msg_id"] for row in kept) == [2, 3]
    first = json.loads((await app.store.get_message(OWNER, 1)).media_json)
    assert first["file_id"] == "F0" and first["text"] == "hi 0" and first["removed"] is True and "file" not in first
    assert not (tmp_path / "media" / str(OWNER) / "2026-01" / "1.ogg").exists()
    assert await app.media_files.evict() == 0
    usage = await app.media_files.usage()
    assert usage == {"files": 2, "bytes": 900_000, "cap_bytes": 1_000_000}


async def test_nothing_is_kept_when_the_cap_is_zero_or_the_file_is_over_it(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update={"keep_cap_mb": 0})})
    assert await app.media_files.keep(OWNER, 1, "photo", PNG, datetime.now(timezone.utc)) == {}


async def test_sent_media_is_kept_with_its_text_kind_time_and_file_id(make_app, studio_server, tmp_path):
    app = make_app(contents=(json.dumps({"bubbles": ["hey"], "image": "A sunny park"}),))
    await connect(app, studio_server)

    async def send_photo(chat_id, file, **options):
        app.bot._next_id += 1
        return SimpleNamespace(message_id=app.bot._next_id, photo=[SimpleNamespace(file_id="TG-FILE")])

    app.bot.send_photo = send_photo
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    row = (await app.store.kept_media())[0]
    media = row["media"]
    assert media["kind"] == "photo" and media["scene"] == "A sunny park" and media["file_id"] == "TG-FILE"
    assert media["file"].startswith(f"media/{OWNER}/") and media["file"].endswith(f"/{row['msg_id']}.png")
    assert (tmp_path / media["file"]).read_bytes() == b"fake media" and media["bytes"] == 10 and media["sent_at"]


async def test_his_own_latest_photo_is_shown_to_him_from_the_kept_file(make_app, tmp_path):
    app, bot, describer = await seeing_app(make_app, chat=OWNER)
    fields = await app.media_files.keep(OWNER, 20, "photo", PNG, datetime.now(timezone.utc))
    await assistant_media(app, OWNER, 20, {"kind": "photo", "scene": "my desk", **fields, "file_id": "F"},
                          at=datetime.now(timezone.utc).isoformat())
    await deliver(app, tg(chat_id=OWNER, sender=OWNER, text="what did you send?", msg_id=21))
    content = turn_requests(app)[0]["messages"][1]["content"]
    assert content[-2]["text"] == "[message 20, the photo you sent]"
    assert content[-1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_a_file_belonging_to_another_chat_never_reaches_this_chats_prompt(make_app):
    app, bot, describer = await seeing_app(make_app, chat=FRIEND)
    await app.store.set_chat_auth_status(OWNER, "approved")
    fields = await app.media_files.keep(OWNER, 20, "photo", PNG, datetime.now(timezone.utc))
    # a row in the friend's chat that points at the owner DM's file
    await assistant_media(app, FRIEND, 30, {"kind": "photo", "scene": "x", **fields, "file_id": "F"},
                          at=datetime.now(timezone.utc).isoformat())
    await deliver(app, tg(chat_id=FRIEND, sender=FRIEND, text="hi", msg_id=31))
    assert isinstance(turn_requests(app)[0]["messages"][1]["content"], str)


# --- looking before sending --------------------------------------------------------------------------------------------

def image_posts(studio_server):
    return [body for path, body in studio_server[2] if path == "/v1/image"]


async def looking_app(make_app, studio_server, *replies, **studio):
    app = make_app(contents=(json.dumps({"bubbles": ["hey"], "image": "A red door"}), *replies))
    await connect(app, studio_server)
    if studio:
        app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update=studio)})
    see(app, ("text", "image"))
    await online(app, OWNER)
    return app


async def test_a_picture_that_matches_is_sent_after_one_check(make_app, studio_server):
    app = await looking_app(make_app, studio_server, '{"match": true, "fix": ""}')
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(image_posts(studio_server)) == 1
    assert len(app.script.requests) == 2  # the turn, and one look
    assert "image_url" in json.dumps(app.script.requests[1])
    assert len(app.bot.of("photo")) == 1


async def test_a_mismatch_regenerates_once_and_sends_the_better_one(make_app, studio_server):
    app = await looking_app(make_app, studio_server, '{"match": false, "fix": "A red door with a brass knob"}',
                            '{"better": "B"}')
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    posts = image_posts(studio_server)
    assert [post["scene"] for post in posts] == ["A red door", "A red door with a brass knob"]
    assert len(app.script.requests) == 3 and len(app.bot.of("photo")) == 1  # turn, look, compare; one photo sent
    stored = json.loads((await app.store.get_recent_chat_window(OWNER, 1))[0].media_json)
    assert stored["scene"] == "A red door"  # what he asked for stays the record


async def test_it_never_loops_however_the_checks_answer(make_app, studio_server):
    replies = ('{"match": false, "fix": "again"}', '{"better": "A"}', '{"match": false, "fix": "and again"}')
    app = await looking_app(make_app, studio_server, *replies)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(image_posts(studio_server)) == 2  # one regeneration, no more
    assert len(app.script.requests) == 3  # turn, look, compare: the third reply is never asked for
    assert len(app.bot.of("photo")) == 1


async def test_a_local_video_notes_first_frame_is_checked_and_redone_once(make_app, studio_server, monkeypatch):
    from dom.studio import look

    async def frame(video):
        return b"\xff\xd8\xffframe"

    monkeypatch.setattr(look, "first_frame", frame)
    app = make_app(contents=(
        json.dumps({"bubbles": ["hey"], "video_note": {"text": "waving hello"}}),
        '{"match": false, "fix": "waving hello, facing the camera"}', '{"better": "B"}',
    ))
    await connect(app, studio_server)
    see(app, ("text", "image"))
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    posts = [body for path, body in studio_server[2] if path == "/v1/video_note"]
    assert [post["text"] for post in posts] == ["waving hello", "waving hello, facing the camera"]
    assert len(app.bot.of("video_note")) == 1


async def cloud_video_app(make_app, cloud_server, *replies):
    from test_studio_cloud import configure
    from test_studio_video import VIDEO, VOICE

    app = make_app(contents=(json.dumps({"bubbles": ["hey"], "video_note": {"text": "waving hello"}}), *replies))
    configure(app, cloud_server, voice=(VOICE,), video_fallbacks=(VIDEO,), enabled=False)
    made = []

    async def generate_video(config, kind, outfit, chat_id, allowed, make_voice, still=None):
        made.append(kind)
        return b"cloud video %d" % len(made)

    app.cloud_studio.generate_video = generate_video

    async def send_video_note(chat_id, file, **kwargs):
        app.bot.calls.append(("video_note", chat_id))
        return await app.bot.send_message(chat_id, "fixture video note", **kwargs)

    app.bot.send_video_note = send_video_note
    see(app, ("text", "image"))
    await online(app, OWNER)
    return app, made


@pytest.fixture
def frames(monkeypatch):
    from dom.studio import look

    async def frame(video):
        return b"\xff\xd8\xffframe"

    monkeypatch.setattr(look, "first_frame", frame)


@pytest.mark.parametrize("verdict", ['{"clearly_wrong": false}', '{"clearly_wrong": false, "why": "the hair is a little different"}'])
async def test_a_cloud_video_note_that_passes_or_is_only_slightly_off_is_not_redone(make_app, cloud_server, frames, verdict):
    app, made = await cloud_video_app(make_app, cloud_server, verdict)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert made == ["video_note"]  # one video, one check, no second try
    assert len(app.script.requests) == 2 and len(app.bot.of("video_note")) == 1
    assert "clearly" in json.dumps(app.script.requests[1])  # the strict question, not the photo's


async def test_a_clearly_wrong_cloud_video_note_is_redone_once(make_app, cloud_server, frames):
    app, made = await cloud_video_app(
        make_app, cloud_server, '{"clearly_wrong": true}', '{"better": "B"}', '{"clearly_wrong": true}',
    )
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert made == ["video_note", "video_note"]  # one redo, however the checks answer
    assert len(app.script.requests) == 3 and len(app.bot.of("video_note")) == 1  # turn, check, compare
    _, usage = await app.store.studio_usage(OWNER, app.dispatch.now())
    assert [kind for kind, _ in usage] == ["video_note"]  # still one item against the media limits


async def test_a_cloud_video_note_is_not_checked_where_the_setting_is_off(make_app, cloud_server, frames):
    app, made = await cloud_video_app(make_app, cloud_server)
    app.config = app.config.model_copy(update={"studio": app.config.studio.model_copy(update={"look_owner_dm": False})})
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert made == ["video_note"] and len(app.script.requests) == 1


async def test_without_ffmpeg_a_video_note_is_sent_unchecked(monkeypatch):
    from dom.studio import look

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert await look.first_frame(b"mp4") is None


async def test_a_failing_check_sends_the_original(make_app, studio_server):
    app = await looking_app(make_app, studio_server, "not json at all")
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(image_posts(studio_server)) == 1 and len(app.bot.of("photo")) == 1


@pytest.mark.parametrize("setup", ["setting off", "model can't see"])
async def test_no_look_without_the_setting_or_a_model_that_sees(make_app, studio_server, setup):
    if setup == "setting off":
        app = await looking_app(make_app, studio_server, look_owner_dm=False)
    else:
        app = await looking_app(make_app, studio_server)
        see(app, ("text",))
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    assert len(app.script.requests) == 1 and len(image_posts(studio_server)) == 1 and len(app.bot.of("photo")) == 1


async def test_the_look_does_not_hold_back_the_text_bubbles(make_app, studio_server):
    app = await looking_app(make_app, studio_server, '{"match": true, "fix": ""}')
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    kinds = [call[0] for call in app.bot.calls if call[0] in ("send", "photo")]
    assert kinds == ["send", "photo"]  # the text first, the picture after its check


# --- /fresh --------------------------------------------------------------------------------------------------------------

async def say(app, chat_id, text, msg_id):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), msg_id=msg_id, sender_user_id=FRIEND, sender_name="Fran", is_bot=False,
        visibility_scope=app.store.visibility_for(chat_id), text=text, created_at=datetime.now(timezone.utc).isoformat(),
    ))


async def window_texts(app, chat_id):
    rows = (await chat_window.load(app.store, chat_id, app.config)).rows
    return [row.text for row in rows]


async def test_fresh_hides_earlier_messages_and_undo_brings_them_back(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(OWNER, "approved")
    for index in range(3):
        await say(app, OWNER, f"old {index}", index + 1)
    assert await window_texts(app, OWNER) == ["old 0", "old 1", "old 2"]
    assert "Started" in await fresh_start(app, OWNER, "You")
    await say(app, OWNER, "new", 10)
    texts = await window_texts(app, OWNER)
    assert texts[0].startswith("This chat was started fresh") and texts[1:] == ["new"]
    # nothing was deleted: the earlier messages are still there for search
    assert len(await app.store.get_recent_chat_window(OWNER, 20)) == 6  # 3 old, the owner's note, Dom's note, new
    assert "Put the earlier" in await fresh_start(app, OWNER, "You", undo=True)
    after = await window_texts(app, OWNER)
    assert [text for text in after if text.startswith("old") or text == "new"] == ["old 0", "old 1", "old 2", "new"]
    assert any(text.startswith("The fresh start was undone") for text in after)
    assert "Nothing to undo" in await fresh_start(app, OWNER, "You", undo=True)


async def test_a_second_fresh_start_undoes_to_the_first(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(OWNER, "approved")
    await say(app, OWNER, "a", 1)
    await fresh_start(app, OWNER, "You")
    await say(app, OWNER, "b", 2)
    await fresh_start(app, OWNER, "You")
    await say(app, OWNER, "c", 3)
    assert (await window_texts(app, OWNER))[1:] == ["c"]
    await fresh_start(app, OWNER, "You", undo=True)  # back to where the first fresh start left it
    now = await window_texts(app, OWNER)
    assert "b" in now and "a" not in now


async def test_fresh_tells_dom_in_that_chat_and_the_owner_in_the_record(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(FRIEND, "approved")
    await say(app, FRIEND, "old", 1)
    assert await fresh_start(app, FRIEND, "You") is not None
    events = [row.text for row in await app.store.get_recent_chat_window(FRIEND, 10) if row.role == "event"]
    assert events and events[0].startswith("This chat was started fresh")
    assert await fresh_start(app, 424242, "You") is None  # not a chat Dom knows


async def test_the_fresh_command_works_in_the_owner_dm_only(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(OWNER, "approved")
    await say(app, OWNER, "old", 1)
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/fresh", msg_id=2))
    assert any("Started your DM fresh" in call[2] for call in app.bot.of("send", OWNER))
    await handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/fresh undo", msg_id=3))
    assert any("Put the earlier" in call[2] for call in app.bot.of("send", OWNER))
    await app.store.set_chat_auth_status(FRIEND, "approved")
    await handle_message(app, tg(chat_id=FRIEND, sender=FRIEND, text="/fresh", msg_id=1))
    assert not app.bot.of("send", FRIEND) or all("fresh" not in call[2].lower() for call in app.bot.of("send", FRIEND))


async def test_a_fresh_start_applies_to_a_forum_topics_window_too(make_app):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved")
    await chat_window.start_fresh(app.store, GROUP, 100, "2026-01-01T00:00:00+00:00")
    await say(app, GROUP, "before", 1)  # row id below the floor
    await say(app, GROUP, "x", 2)
    assert (await chat_window.load(app.store, GROUP, app.config, thread_id=5)).rows == []


# --- the owner's API ---------------------------------------------------------------------------------------------------------

JSON = {"Origin": "https://dom.test", "Content-Type": "application/json"}


async def test_the_dashboard_can_start_any_chat_fresh_and_undo(make_app, browser):
    from test_web_auth import ORIGIN

    app = make_app()
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Fran")
    await say(app, FRIEND, "old", 1)
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    async with browser(app) as client:
        done = await client.post("/api/chats/fresh", json={"chat_id": FRIEND}, headers=headers)
        assert done.status_code == 200 and "fresh" in done.json()["message"]
        assert (await window_texts(app, FRIEND))[0].startswith("This chat was started fresh")
        undone = await client.post("/api/chats/fresh", json={"chat_id": FRIEND, "undo": True}, headers=headers)
        assert undone.status_code == 200 and "old" in await window_texts(app, FRIEND)
        assert (await client.post("/api/chats/fresh", json={"chat_id": 99}, headers=headers)).status_code == 404
        assert (await client.post("/api/chats/fresh", json={"chat_id": "x"}, headers=headers)).status_code == 422
        assert (await client.post("/api/chats/fresh", json={"chat_id": FRIEND}, headers={**headers, "Authorization": ""})).status_code == 403


async def test_the_library_api_lists_and_serves_kept_files_to_the_owner_only(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Fran")
    fields = await app.media_files.keep(FRIEND, 8, "photo", PNG, datetime.now(timezone.utc))
    await assistant_media(app, FRIEND, 8, {"kind": "photo", "scene": "a pier", "file_id": "TG", **fields},
                          at="2026-02-01T10:00:00+00:00")
    await assistant_media(app, FRIEND, 9, {"kind": "voice", "text": "gone", "file_id": "TG2", "removed": True},
                          at="2026-02-02T10:00:00+00:00")
    same_site = {"Sec-Fetch-Site": "same-origin"}
    async with browser(app) as client:
        listing = (await client.get("/api/media/files", headers=same_site)).json()
        assert [(f["msg_id"], f["kind"], f["stored"], f["text"]) for f in listing["files"]] == [
            (9, "voice", False, "gone"), (8, "photo", True, "a pier"),
        ]
        assert listing["next"] is None
        served = await client.get(listing["files"][1]["url"], headers=same_site)
        assert served.status_code == 200 and served.content == PNG and served.headers["content-type"] == "image/png"
        assert (await client.get("/api/media/files", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await client.get("/api/media/files", headers={**same_site, "Authorization": ""})).status_code == 403
        assert (await client.get("/api/media/files/%d/999" % FRIEND, headers=same_site)).status_code == 404
        usage = (await client.get("/api/media/usage", headers=same_site)).json()
        assert usage["files"] == 1 and usage["bytes"] == len(PNG)
        # a removed file is fetched from Telegram through its file_id
        app.bot.download = lambda file_id: asyncio.sleep(0, io.BytesIO(b"from telegram"))
        gone = await client.get("/api/media/files/%d/9" % FRIEND, headers=same_site)
        assert gone.status_code == 200 and gone.content == b"from telegram"


async def test_the_inputs_api_reports_and_reprobes(make_app, browser):
    from test_web_auth import ORIGIN

    app = make_app(contents=("Red",))
    app.client = ChatClient(endpoint(), None, app.client.http, sleep=no_sleep)
    async with browser(app) as client:
        before = (await client.get("/api/media/inputs", headers={"Sec-Fetch-Site": "same-origin"})).json()
        assert before["providers"][0]["source"] == "unknown" and before["route_accepts"] == []
        done = await client.post("/api/media/inputs/probe", json={}, headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert done.status_code == 200 and done.json()["route_accepts"] == ["image"]
        assert done.json()["providers"][0]["source"] == "probe"


# --- config ------------------------------------------------------------------------------------------------------------------

def test_input_override_is_validated_and_defaults_are_safe(tmp_path):
    assert ProviderEndpoint(base_url="https://x/v1", model="m").input is None
    assert ProviderEndpoint(base_url="https://x/v1", model="m", input=["text", "image"]).input == ("text", "image")
    with pytest.raises(ValueError):
        ProviderEndpoint(base_url="https://x/v1", model="m", input=["smell"])
    config = Config(home=tmp_path, models={"chat": endpoint()})
    assert (config.media_input_images, config.media_input_audio, config.media_input_video) == (2, 1, 1)
    assert config.studio.keep_cap_mb == 2048
    assert (config.studio.look_owner_dm, config.studio.look_friend_dm, config.studio.look_group) == (True, False, False)


async def test_a_saved_route_keeps_its_input_override(tmp_path):
    import tomlkit

    from dom.web.model_settings import set_role

    document = tomlkit.parse(
        '[models.chat]\nbase_url = "https://x/v1"\nmodel = "m"\ninput = ["text", "image"]\n'
    )
    set_role(document, "chat", [ProviderEndpoint(base_url="https://x/v1", model="m")])
    assert list(document["models"]["chat"]["input"]) == ["text", "image"]
    set_role(document, "chat", [ProviderEndpoint(base_url="https://x/v1", model="other")])
    assert "input" not in document["models"]["chat"]


def test_a_backup_leaves_out_the_media_folder(tmp_path):
    from dom.backup import backup

    home = tmp_path / "home"
    (home / "media" / "1" / "2026-01").mkdir(parents=True)
    (home / "media" / "1" / "2026-01" / "5.png").write_bytes(PNG)
    (home / "memory").mkdir()
    (home / "memory" / "MEMORY.md").write_text("notes")
    import sqlite3
    import tarfile

    sqlite3.connect(home / "state.db").close()
    archive = backup(home, destination=tmp_path / "out")
    names = tarfile.open(archive).getnames()
    assert any(name.endswith("memory/MEMORY.md") for name in names)
    assert not any("media" in name.split("/") for name in names)
