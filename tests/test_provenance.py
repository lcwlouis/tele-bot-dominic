"""v0.8.4: where each kept media item came from and what it cost, read back from the usage rows it links."""

import asyncio
import json
import shutil
import struct

import pytest

from dom import provenance
from dom.models import StoredMessage
from test_studio import connect, studio_server  # noqa: F401  (the studio fixture)
from test_studio_cloud import IMAGE, VOICE, check_voice, cloud_server, configure  # noqa: F401
from test_usage import deliver, online
from test_w7_studio import posts, raw_stills, scene_app  # noqa: F401
from tghelpers import OWNER, tg

SAME_SITE = {"Sec-Fetch-Site": "same-origin"}


async def made_of(app):
    [stored] = [m for m in await app.store.get_recent_chat_window(OWNER, 8) if m.media_json]
    return json.loads(stored.media_json)["made"]


async def costs_of(app, made):
    return await app.store.usage_costs(provenance.usage_ids(made))


async def test_a_local_note_records_the_studios_models_the_still_and_its_redo_and_is_free(
    make_app, studio_server, raw_stills, monkeypatch,
):
    from test_native_media import see

    app = await scene_app(
        make_app, studio_server, None, '{"match": false, "fix": "on a rooftop, facing the camera"}', '{"better": "B"}',
    )
    app.studio._health = {**app.studio._health, "models": {"image": "FLUX.2 klein", "video": "FlashHead"}}
    see(app, ("text", "image"))
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    made = await made_of(app)
    assert made["kept_redo"] == ["still"] and made["seconds"] >= 0
    assert [(s["part"], s["model"], s["host"], bool(s.get("redo"))) for s in made["steps"]] == [
        ("still", "FLUX.2 klein", "local", False), ("still", "FLUX.2 klein", "local", True),
        ("item", "FlashHead", "local", False),
    ]
    shown = provenance.describe(made, await costs_of(app, made))
    assert shown["source"] == "local" and shown["model"] == "FlashHead" and shown["free"] is True
    assert shown["cost"] == 0 and shown["pending"] is False
    assert shown["still"]["model"] == "FLUX.2 klein" and shown["still"]["redo"]["kept"] == "second"
    assert shown["redo"] is None  # the note itself was not redone


async def test_an_unnamed_studio_model_reads_as_local_studio(make_app, studio_server, raw_stills):
    app = await scene_app(make_app, studio_server)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    shown = provenance.describe(await made_of(app), {})
    assert shown["model"] == "local-studio" and shown["still"]["model"] == "local-studio"
    assert shown["duration"] is None  # the fixture's bytes are not a real video


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
async def test_a_cloud_voice_links_its_usage_row_so_a_late_cost_appears_when_it_lands(
    make_app, studio_server, cloud_server,
):
    app = make_app(contents=('{"bubbles":["hey"],"voice":"A quiet hello"}',))
    await connect(app, studio_server)
    configure(app, cloud_server, voice=(VOICE,))
    studio_server[3]["/v1/voice"] = 503  # the local studio is busy: the backup speaks
    check_voice(app)
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    await asyncio.wait_for(asyncio.gather(*app.cloud_studio._reconciling), 5)
    made = await made_of(app)
    [row] = provenance.usage_ids(made)
    assert made["steps"][0]["model"] == VOICE.model and made["steps"][0]["host"] == "openrouter.ai"
    assert made["duration"] > 0
    shown = provenance.describe(made, await costs_of(app, made))
    assert shown["source"] == "cloud" and shown["cost"] == 0.02 and shown["pending"] is False and shown["free"] is False
    await app.store._run(app.store._conn.execute, "UPDATE usage SET cost_usd = 0, settled = 0 WHERE id = ?", (row,))
    # as if OpenRouter had not reported yet
    waiting = provenance.describe(made, await costs_of(app, made))
    assert waiting["cost"] == 0 and waiting["pending"] is True
    await app.store.set_usage_cost(row, 0.031)  # the background fetch lands
    landed = provenance.describe(made, await costs_of(app, made))
    assert landed["cost"] == 0.031 and landed["pending"] is False


async def test_a_redone_cloud_photo_counts_the_redo_and_a_missing_row_is_left_out(make_app):
    app = make_app()
    usage = app.dispatch.usage
    with provenance.tracing() as trace:
        first = await usage.record("studio_image", "vendor/image-a", "openrouter.ai", {"cost": 0.04}, OWNER, fallback=True)
        with provenance.redoing():
            again = await usage.record("studio_image", "vendor/image-a", "openrouter.ai", {"cost": 0.04}, OWNER, fallback=True)
            gone = await usage.record("studio_image", "vendor/image-b", "openrouter.ai", {"cost": 0.9}, OWNER, fallback=True)
            await app.store.delete_usage(gone)
            provenance.discard(gone)
        provenance.kept_redo()
        await usage.record("chat", "m", "llm.test", {"cost": 5}, OWNER)  # not a studio call: never linked
    made = trace.made(outfit="casual", preset="Cosy", seconds=3.26, duration=None)
    assert provenance.usage_ids(made) == [first, again] and made["seconds"] == 3.3
    shown = provenance.describe(made, await costs_of(app, made))
    assert shown["cost"] == 0.08 and shown["redo"] == {"cost": 0.04, "pending": False, "kept": "second", "free": False}
    assert (shown["outfit"], shown["preset"], shown["still"], shown["voice"]) == ("casual", "Cosy", None, None)
    assert shown["model"] == "vendor/image-a" and shown["host"] == "openrouter.ai"
    # a trace made outside a job records nothing
    await usage.record("studio_image", "x", "openrouter.ai", {}, OWNER)
    assert provenance.current() is None


def test_describe_ignores_what_is_not_a_record_and_prices_only_cloud_steps():
    assert provenance.describe(None, {}) is None and provenance.describe({"steps": "x"}, {}) is None
    assert provenance.describe({"steps": []}, {}) is None
    made = {"steps": [
        {"part": "item", "kind": "studio_video", "model": "avatar", "host": "openrouter.ai", "usage_id": 4},
        {"part": "voice", "kind": "studio_voice", "model": "tts", "host": "local", "usage_id": None},
    ]}
    shown = provenance.describe(made, {4: None})
    assert shown["pending"] is True and shown["free"] is False
    assert shown["voice"]["free"] is True and shown["voice"]["pending"] is False


def test_the_length_of_a_voice_note_and_a_video_is_read_from_the_file_itself():
    assert provenance.seconds_of("voice", b"OggS\0\2" + struct.pack("<q", 0) + b"\0" * 12) is None
    last = b"OggS\0\4" + struct.pack("<q", 48312)
    head = b"OggS\0\2" + struct.pack("<q", 0) + b"\0" * 8 + b"OpusHead\1\1" + struct.pack("<H", 312)
    assert provenance.seconds_of("voice", head + last + b"\0" * 8) == 1.0
    assert provenance.seconds_of("voice", b"OggS") is None
    mvhd = b"mvhd" + b"\0\0\0\0" + b"\0" * 8 + struct.pack(">II", 600, 4500)
    assert provenance.seconds_of("video_note", b"\0\0\0\x6cmoov\0\0\0\x6c" + mvhd) == 7.5
    assert provenance.seconds_of("video_note", b"not a video") is None
    assert provenance.seconds_of("photo", b"anything") is None


async def test_the_library_api_shows_where_each_item_came_from_and_what_it_cost(make_app, browser):
    app = make_app()
    row = await app.store.record_usage("2026-02-01T10:00:00+00:00", "studio_image", "vendor/image-a", "openrouter.ai",
                                       OWNER, 0, 0, 0.043, 1)
    waiting = await app.store.record_usage("2026-02-01T10:00:00+00:00", "studio_voice", "vendor/tts", "openrouter.ai",
                                           OWNER, 0, 0, 0.0, 1)
    made = {
        "steps": [{"part": "item", "kind": "studio_image", "model": "vendor/image-a", "host": "openrouter.ai", "usage_id": row}],
        "outfit": "casual", "preset": "Cosy", "seconds": 4.2,
    }
    spoken = {"steps": [{"part": "item", "kind": "studio_voice", "model": "vendor/tts", "host": "openrouter.ai",
                         "usage_id": waiting}], "duration": 2.5, "seconds": 1.0}
    for msg_id, media, at in (
        (8, {"kind": "photo", "scene": "a pier", "file_id": "TG", "made": made}, "2026-02-01T10:00:00+00:00"),
        (9, {"kind": "voice", "text": "hi", "file_id": "TG2", "made": spoken}, "2026-02-02T10:00:00+00:00"),
        (10, {"kind": "photo", "scene": "old", "file_id": "TG3"}, "2026-02-03T10:00:00+00:00"),
    ):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(OWNER), msg_id=msg_id, is_bot=True, role="assistant", visibility_scope=app.store.visibility_for(OWNER),
            text="[x]", media_json=json.dumps(media), created_at=at,
        ))
    async with browser(app) as client:
        files = {f["msg_id"]: f for f in (await client.get("/api/media/files", headers=SAME_SITE)).json()["files"]}
    assert files[10]["made"] is None  # media from before provenance
    photo = files[8]["made"]
    assert photo == {
        "source": "cloud", "model": "vendor/image-a", "host": "openrouter.ai", "cost": 0.043, "pending": False,
        "redo": None, "free": False, "outfit": "casual", "preset": "Cosy", "seconds": 4.2, "duration": None,
        "voice": None, "still": None,
    }
    assert files[9]["made"]["pending"] is True and files[9]["made"]["duration"] == 2.5 and files[9]["made"]["cost"] == 0
    await app.store.set_usage_cost(waiting, 0.012)
    async with browser(app) as client:
        again = {f["msg_id"]: f for f in (await client.get("/api/media/files", headers=SAME_SITE)).json()["files"]}
    assert again[9]["made"]["pending"] is False and again[9]["made"]["cost"] == 0.012


async def test_a_failed_video_job_settled_at_zero_is_not_pending_but_an_unreported_row_is(make_app):
    app = make_app()
    usage = app.dispatch.usage
    with provenance.tracing() as trace:
        failed = await usage.record("studio_video", "vendor/video", "openrouter.ai", {"cost": 0.2}, OWNER, fallback=True)
        with provenance.redoing():
            backup = await usage.record("studio_video", "vendor/video-b", "openrouter.ai", {"cost": 0.3}, OWNER, fallback=True)
    made = trace.made(outfit=None, preset=None, seconds=1, duration=None)
    await app.store.set_usage_cost(failed, 0.0)  # the job failed: the provider billed nothing, and said so
    await app.store.set_usage_cost(backup, 0.31)
    shown = provenance.describe(made, await costs_of(app, made))
    assert shown["cost"] == 0.31 and shown["pending"] is False and shown["redo"]["pending"] is False
    # a row that has simply not been priced yet stays pending
    waiting = await app.store.record_usage("2026-01-01T00:00:00+00:00", "studio_voice", "tts", "openrouter.ai", OWNER, 0, 0, 0.0, 1)
    assert (await app.store.usage_costs([waiting])) == {waiting: None}
    made = {"steps": [{"part": "item", "kind": "studio_voice", "model": "tts", "host": "openrouter.ai", "usage_id": waiting}]}
    assert provenance.describe(made, await app.store.usage_costs([waiting]))["pending"] is True


def test_the_redo_says_for_itself_whether_it_was_free():
    made = {"steps": [
        {"part": "item", "kind": "studio_image", "model": "cloud-m", "host": "openrouter.ai", "usage_id": 1},
        {"part": "item", "kind": "studio_image", "model": "local-m", "host": "local", "usage_id": None, "redo": True},
    ], "kept_redo": ["item"]}
    shown = provenance.describe(made, {1: 0.04})
    assert shown["free"] is False and shown["redo"]["free"] is True and shown["cost"] == 0.04
