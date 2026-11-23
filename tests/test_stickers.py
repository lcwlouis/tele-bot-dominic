import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from pydantic import ValidationError

from dom.actions import Actions, Bubble
from dom.media import MAX_BYTES, MediaDescriber, STICKER_PREVIEW_PROMPT
from dom.stickers import DESCRIPTIONS_FILE, MAX_DESCRIPTIONS_PER_PASS, MAX_EMOJI, PERMANENT_SKIP, Stickers
from dom.telegram import executor, handlers
from test_owner_events import events
from test_traits import put
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg, turn_requests

SET = "example_set_by_examplebot"


def sticker_bot(app, sets):
    requested = []

    async def get_sticker_set(name):
        requested.append(name)
        return SimpleNamespace(stickers=[
            SimpleNamespace(emoji=emoji, file_id=file_id) for emoji, file_id in sets[name]
        ])

    async def send_sticker(chat_id, sticker, **kwargs):
        app.bot._next_id += 1
        app.bot.calls.append(("sticker", chat_id, sticker, kwargs))
        return SimpleNamespace(message_id=app.bot._next_id)

    app.bot.get_sticker_set = get_sticker_set
    app.bot.send_sticker = send_sticker
    return requested


async def test_load_sets_caches_each_emoji_and_refreshes_changes(make_app):
    app = make_app()
    other = "example_other_by_examplebot"
    requested = sticker_bot(app, {SET: [("📚", "file-a"), (None, "file-b")], other: [("📚", "file-c")]})
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    await app.stickers.refresh()
    await app.stickers.refresh()
    assert app.stickers.by_emoji == {"📚": ["file-a"]}
    assert requested == [SET]

    put(app.home, "stickers.toml", f"sets = [{json.dumps(other)}]")
    await app.stickers.refresh()
    assert requested == [SET, other]
    assert app.stickers.by_emoji == {"📚": ["file-c"]}


async def test_caps_sets_stickers_per_set_and_unique_emoji_in_turn_note(make_app):
    app = make_app()
    names = [f"example_{index}_by_examplebot" for index in range(11)]
    emoji = [chr(0x1F680 + i) for i in range(41)]  # none of them is an allowed reaction, so none is in the prompt prefix
    sets = {name: [(emoji[i % 41], f"file-{name}-{i}") for i in range(121)] for name in names}
    requested = sticker_bot(app, sets)
    put(app.home, "stickers.toml", f"sets = {json.dumps(names)}")
    await app.stickers.refresh()

    assert requested == names[:10]
    assert sum(map(len, app.stickers.by_emoji.values())) == 1200
    assert all(not file.endswith("-120") for ids in app.stickers.by_emoji.values() for file in ids)
    await online(app)
    await deliver(app, tg())
    prefix, context = [row["content"] for row in turn_requests(app)[0]["messages"]]
    assert all(key in context and key not in prefix for key in emoji[:40])
    assert emoji[-1] not in context


@pytest.mark.parametrize("value", [5, ["📚"], ""])
def test_action_rejects_invalid_sticker_values(value):
    with pytest.raises(ValidationError):
        Actions(sticker=value)
    assert Actions(sticker="📚").sticker == "📚"
    assert Actions().sticker is None


async def test_executor_sends_a_matching_sticker_after_text(make_app):
    app = make_app(contents=('{"bubbles":[{"text":"A good book!"}],"sticker":"📚"}',))
    sticker_bot(app, {SET: [("📚", "file-a"), ("🍵", "file-b")]})
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    await app.stickers.refresh()
    await online(app)
    await deliver(app, tg())
    # The same indicator stays on through thinking and the typing pause.
    assert [call[0] for call in app.bot.calls] == ["action", "send", "sticker"]
    assert app.bot.of("sticker")[0][:3] == ("sticker", GROUP, "file-a")
    latest = await app.store.get_recent_chat_window(GROUP, 1)
    assert json.loads(latest[0].media_json)["emoji"] == "📚"


async def test_unknown_sticker_emoji_ignored_and_logged(make_app, caplog):
    app = make_app()
    await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(sticker="🍵", bubbles=[Bubble(text="Tea?")]),
        stickers={"📚": ["file-a"]}, sleep=app.sleep,
    )
    assert app.bot.of("sticker") == []
    assert app.bot.of("send")[0][2] == "Tea?"
    assert "unknown sticker emoji" in caplog.text


async def test_only_owner_dm_sticker_commands_change_sets(make_app):
    app = make_app()
    requested = sticker_bot(app, {SET: [("📚", "file-a")]})
    for chat_id, sender in ((GROUP, OWNER), (FRIEND, FRIEND)):
        await handlers.handle_message(app, tg(chat_id, sender, f"/stickers add {SET}"))
    assert requested == []
    assert not app.home.read("stickers.toml")

    await handlers.handle_message(app, tg(OWNER, OWNER, f"/stickers add {SET}"))
    assert requested == [SET] and app.stickers.by_emoji == {"📚": ["file-a"]}
    assert SET in app.home.read("stickers.toml")
    await handlers.handle_message(app, tg(OWNER, OWNER, f"/stickers remove {SET}"))
    assert app.home.read("stickers.toml") == "sets = []\n"
    assert not app.stickers.by_emoji
    texts = [row["text"] for row in await events(app)]
    assert all(any(action in text for text in texts) for action in ("Added sticker set", "Removed sticker set"))


async def test_a_crashing_refresh_is_logged_and_never_raised(make_app, caplog):
    app = make_app()

    async def explode(name):
        raise RuntimeError("boom")

    app.bot.get_sticker_set = explode
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    await app.stickers.refresh()
    assert app.stickers.by_emoji == {}
    assert "sticker" in caplog.text


async def test_a_failed_set_is_retried_only_after_an_hour(make_app):
    app = make_app()
    requested, failing = [], {"on": True}

    async def get_sticker_set(name):
        requested.append(name)
        if failing["on"]:
            raise TelegramNetworkError(method=None, message="down")
        return SimpleNamespace(stickers=[SimpleNamespace(emoji="📚", file_id="file-a")])

    app.bot.get_sticker_set = get_sticker_set
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    await app.stickers.refresh()
    app.dispatch.now.advance(minutes=59)
    await app.stickers.refresh()
    assert requested == [SET]
    failing["on"] = False
    app.dispatch.now.advance(minutes=1)
    await app.stickers.refresh()
    assert requested == [SET, SET] and app.stickers.by_emoji == {"📚": ["file-a"]}
    await app.stickers.refresh()
    assert requested == [SET, SET]


async def test_a_failing_sticker_refresh_does_not_stop_jobs_or_openers_in_the_same_pass(make_app, monkeypatch):
    app = make_app()
    ran = []

    async def explode(name):
        raise RuntimeError("boom")

    async def job():
        ran.append("job")

    async def maybe(now):
        ran.append("opener")

    app.bot.get_sticker_set = explode
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    app.dispatch.jobs.register("probe", lambda local, habits: True, job)
    monkeypatch.setattr(app.dispatch.openers, "maybe", maybe)
    await app.dispatch._background_jobs()
    assert ran == ["job", "opener"]


def make_describer(replies, *, error=None, on_download=None):
    downloads = []
    calls = []

    class FakeDownload:
        async def download(self, file_id):
            downloads.append(file_id)
            if on_download is not None:
                await on_download(file_id)
            return io.BytesIO(f"bytes:{file_id}".encode())

    class FakeClient:
        async def complete(self, messages, tools):
            calls.append(messages)
            if error is not None and error.get("on", True):
                raise RuntimeError("media model down")
            file_id = downloads[-1]
            text = replies(file_id) if callable(replies) else replies.get(file_id, "cozy sticker")
            return {"content": text}

    return MediaDescriber(FakeClient(), FakeDownload()), downloads, calls


async def test_adding_set_schedules_background_descriptions_and_keeps_prefix_stable(make_app):
    app = make_app(contents=(
        '{"bubbles":[{"text":"First!"}],"sticker":"📚"}',
        '{"bubbles":[{"text":"Second!"}]}',
    ))
    sticker_bot(app, {SET: [("📚", "file-a"), ("🍵", "file-b")]})
    describer, downloads, calls = make_describer({
        "file-a": "  \"Cozy cat reading a book, calm\"  \n",
        "file-b": "Steaming green tea cup, relaxed",
    })
    app.media_describer = describer

    await handlers.handle_message(app, tg(OWNER, OWNER, f"/stickers add {SET}"))
    assert any("describing 2 stickers in the background" in text for _, _, text, _ in app.bot.of("send"))
    await drain_dispatch(app.dispatch)

    assert downloads == ["file-a", "file-b"]
    assert calls[0][0]["content"][0]["text"] == STICKER_PREVIEW_PROMPT
    cached = json.loads(app.home.read(DESCRIPTIONS_FILE))
    assert cached == {
        "file-a": "Cozy cat reading a book, calm",
        "file-b": "Steaming green tea cup, relaxed",
    }

    await online(app)
    await deliver(app, tg(msg_id=1, text="hi"))
    prefix1, context1 = [row["content"] for row in turn_requests(app)[0]["messages"]]
    assert "Cozy cat reading a book, calm" not in prefix1
    assert "📚" not in prefix1
    assert "📚 (Cozy cat reading a book, calm)" in context1
    assert "🍵 (Steaming green tea cup, relaxed)" in context1

    note1 = app.stickers.turn_note()
    await deliver(app, tg(msg_id=2, text="again"))
    prefix2, _ = [row["content"] for row in turn_requests(app)[1]["messages"]]
    assert prefix2 == prefix1
    assert app.stickers.turn_note() == note1
    assert downloads == ["file-a", "file-b"]  # each sticker described once


async def test_reply_turn_never_runs_or_waits_for_media_descriptions(make_app):
    app = make_app(contents=('{"bubbles":[{"text":"Hey"}]}',))
    sticker_bot(app, {SET: [("📚", "file-a")]})
    describer, downloads, calls = make_describer({"file-a": "Owl perched on stacked books, studious"})
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    # Load the set without a media describer, then attach one right before the turn.
    await app.stickers.refresh()
    app.media_describer = describer

    await online(app)
    await deliver(app, tg())
    # The turn path must never invoke the media model; it falls back to the bare alias.
    assert downloads == [] and calls == []
    _, context = [row["content"] for row in turn_requests(app)[0]["messages"]]
    assert "📚" in context and "Owl perched" not in context

    # Background refresh populates the cache; a fresh Stickers instance reuses it across restarts.
    await app.stickers.refresh()
    await drain_dispatch(app.dispatch)
    assert downloads == ["file-a"]

    restarted = Stickers(app.home, app.bot, lambda: app.dispatch.now(), describer=lambda: describer)
    await restarted.refresh()
    assert downloads == ["file-a"]
    assert "📚 (Owl perched on stacked books, studious)" in restarted.turn_note()


async def test_two_turns_complete_immediately_during_cold_background_pass(make_app):
    app = make_app(contents=(
        '{"bubbles":[{"text":"One"}]}',
        '{"bubbles":[{"text":"Two"}]}',
    ))
    sticker_bot(app, {SET: [("📚", "file-a"), ("🍵", "file-b")]})
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_download(file_id):
        started.set()
        await release.wait()

    describer, downloads, _ = make_describer(
        {"file-a": "Bookish cat", "file-b": "Warm tea"}, on_download=slow_download,
    )
    app.media_describer = describer
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")

    await app.stickers.refresh()
    await started.wait()  # cold pass is in-flight and blocked on download

    await online(app)
    await online(app, FRIEND)
    # Deliver two turns directly while the cold background pass is blocked.
    await handlers.handle_message(app, tg(GROUP, FRIEND, "first", msg_id=1))
    await handlers.handle_message(app, tg(FRIEND, FRIEND, "second", msg_id=2))
    await asyncio.wait_for(asyncio.gather(*list(app.dispatch._debounces.values())), timeout=2.0)
    # Both turns completed without waiting on the blocked sticker description pass.
    assert len(turn_requests(app)) == 2
    assert all("📚" in req["messages"][1]["content"] and "Bookish cat" not in req["messages"][1]["content"] for req in turn_requests(app))

    # Release the background pass and verify descriptions land.
    release.set()
    await drain_dispatch(app.dispatch)
    assert downloads == ["file-a", "file-b"]
    assert "📚 (Bookish cat)" in app.stickers.turn_note()


async def test_big_set_caps_a_pass_and_describes_every_emoji_before_second_variants(make_app):
    app = make_app()
    # 55 unique emoji, 2 stickers per emoji (110 stickers total); only the first MAX_EMOJI emoji are offered.
    emoji = [chr(0x1F680 + i) for i in range(55)]
    items = [(e, f"file-{idx}-first") for idx, e in enumerate(emoji)] + [
        (e, f"file-{idx}-second") for idx, e in enumerate(emoji)
    ]
    sticker_bot(app, {SET: items})
    describer, downloads, calls = make_describer(lambda fid: f"desc {fid}")
    app.media_describer = describer

    await app.stickers.change("add", SET)
    await drain_dispatch(app.dispatch)

    assert len(downloads) == len(calls) == MAX_DESCRIPTIONS_PER_PASS == MAX_EMOJI
    assert all(fid.endswith("-first") for fid in downloads)

    await app.stickers.refresh()  # the next pass describes the rest of the offered stickers
    await drain_dispatch(app.dispatch)
    assert len(downloads) == 2 * MAX_EMOJI and len(set(downloads)) == len(downloads)
    assert not any(fid.startswith(f"file-{MAX_EMOJI}-") for fid in downloads)


async def test_every_variant_stays_sendable_and_each_one_is_described(make_app):
    app = make_app(contents=tuple('{"bubbles":[{"text":"Hi"}],"sticker":"📚2"}' for _ in range(3)))
    sticker_bot(app, {SET: [("📚", "file-rep"), ("📚", "file-alt-1"), ("📚", "file-alt-2"), ("🍵", "file-tea")]})
    describer, downloads, _ = make_describer({
        "file-rep": "Cat reading a big book, cozy", "file-alt-1": "Owl on a book stack", "file-alt-2": "Sleepy cat",
    })
    app.media_describer = describer

    await app.stickers.change("add", SET)
    await drain_dispatch(app.dispatch)
    assert sorted(downloads) == ["file-alt-1", "file-alt-2", "file-rep", "file-tea"]
    sendable = app.stickers.sendable()
    assert sendable["📚"] == ["file-rep", "file-alt-1", "file-alt-2"]  # none of them is lost once one is described
    assert (sendable["📚1"], sendable["📚2"], sendable["📚3"]) == (["file-rep"], ["file-alt-1"], ["file-alt-2"])
    note = app.stickers.turn_note()
    assert "📚 (📚1 Cat reading a big book, cozy; 📚2 Owl on a book stack; 📚3 Sleepy cat)" in note
    assert "🍵 (" in note and "with its number" in note

    await online(app)
    await deliver(app, tg(msg_id=1, text="msg 1"))  # Dom picks the described variant he wants
    assert [call[2] for call in app.bot.of("sticker")] == ["file-alt-1"]


async def test_animated_video_and_oversize_stickers_and_permanent_skip_cache(make_app):
    app = make_app()

    async def get_sticker_set(name):
        return SimpleNamespace(stickers=[
            SimpleNamespace(
                emoji="🎉", file_id="anim-file", file_unique_id="u-anim",
                is_animated=True, is_video=False, thumbnail=SimpleNamespace(file_id="thumb-anim"),
            ),
            SimpleNamespace(
                emoji="🎬", file_id="vid-file", file_unique_id="u-vid",
                is_animated=False, is_video=True, thumbnail=SimpleNamespace(file_id="thumb-vid"),
            ),
            SimpleNamespace(
                emoji="👻", file_id="nothumb-file", file_unique_id="u-nothumb",
                is_animated=True, is_video=False, thumbnail=None,
            ),
            SimpleNamespace(
                emoji="🐘", file_id="huge-file", file_unique_id="u-huge",
                is_animated=False, is_video=False, file_size=MAX_BYTES["sticker"] + 1,
            ),
        ])

    app.bot.get_sticker_set = get_sticker_set
    describer, downloads, calls = make_describer({
        "thumb-anim": "Confetti popping, celebratory",
        "thumb-vid": "Clapperboard snapping, playful",
    })
    app.media_describer = describer
    await app.stickers.change("add", SET)
    await drain_dispatch(app.dispatch)

    assert downloads == ["thumb-anim", "thumb-vid"]
    assert all(call[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,") for call in calls)
    cached = json.loads(app.home.read(DESCRIPTIONS_FILE))
    assert cached["u-nothumb"] == PERMANENT_SKIP
    assert cached["u-huge"] == PERMANENT_SKIP

    # Advancing clock past RETRY_AFTER must NOT retry permanently skipped stickers.
    app.dispatch.now.advance(hours=2)
    await app.stickers.refresh()
    await drain_dispatch(app.dispatch)
    assert downloads == ["thumb-anim", "thumb-vid"]

    note = app.stickers.turn_note()
    assert "🎉 (Confetti popping, celebratory)" in note
    assert "🎬 (Clapperboard snapping, playful)" in note
    assert "👻" in note and "🐘" in note


async def test_cancellation_mid_pass_keeps_earlier_descriptions_on_disk(make_app):
    app = make_app()
    sticker_bot(app, {SET: [("📚", "file-1"), ("🍵", "file-2"), ("🎉", "file-3")]})
    second_started = asyncio.Event()
    block_second = asyncio.Event()

    async def on_download(file_id):
        if file_id == "file-2":
            second_started.set()
            await block_second.wait()

    describer, downloads, _ = make_describer(
        {"file-1": "First saved", "file-2": "Second blocked", "file-3": "Third"},
        on_download=on_download,
    )
    app.media_describer = describer

    await app.stickers.change("add", SET)
    await second_started.wait()
    await app.dispatch.close()  # cancels background task mid-pass

    assert downloads == ["file-1", "file-2"]
    cached = json.loads(app.home.read(DESCRIPTIONS_FILE))
    assert cached == {"file-1": "First saved"}


async def test_corrupt_cache_file_recovers_cleanly(make_app):
    app = make_app()
    sticker_bot(app, {SET: [("📚", "file-a")]})
    describer, downloads, _ = make_describer({"file-a": "Fresh description"})
    app.media_describer = describer
    put(app.home, "stickers.toml", f"sets = [{json.dumps(SET)}]")
    put(app.home, DESCRIPTIONS_FILE, "{corrupt json")

    await app.stickers.refresh()
    await drain_dispatch(app.dispatch)
    assert downloads == ["file-a"]
    assert json.loads(app.home.read(DESCRIPTIONS_FILE)) == {"file-a": "Fresh description"}
    assert "📚 (Fresh description)" in app.stickers.turn_note()


async def test_spend_capped_describe_is_skipped_cleanly_and_retried_when_allowed(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 1.0})
    sticker_bot(app, {SET: [("📚", "file-a")]})
    describer, downloads, calls = make_describer({"file-a": "Cozy book"})
    app.media_describer = describer

    # Record usage at the daily spend cap so the global budget check refuses calls.
    await app.store.record_usage(
        app.dispatch.now().isoformat(), "chat", "example-model", "example.org", GROUP, 10, 10, 1.0, 0,
    )
    await app.stickers.change("add", SET)
    await drain_dispatch(app.dispatch)

    assert downloads == [] and calls == []
    assert app.home.read(DESCRIPTIONS_FILE) == ""
    assert "📚" in app.stickers.turn_note() and "Cozy book" not in app.stickers.turn_note()

    # Next day (under cap): refresh schedules and completes the description.
    app.dispatch.now.advance(days=1)
    await app.stickers.refresh()
    await drain_dispatch(app.dispatch)
    assert downloads == ["file-a"]
    assert "📚 (Cozy book)" in app.stickers.turn_note()


async def test_media_failure_falls_back_to_alias_and_retries_after_backoff(make_app):
    app = make_app()
    sticker_bot(app, {SET: [(chr(0x1F680 + i), f"file-{i}") for i in range(6)]})
    failing = {"on": True}
    describer, downloads, _ = make_describer(lambda fid: f"preview for {fid}", error=failing)
    app.media_describer = describer

    await app.stickers.change("add", SET)
    await drain_dispatch(app.dispatch)
    # Circuit breaker stops after 3 consecutive failures instead of hammering a down endpoint.
    assert len(downloads) == 3
    note = app.stickers.turn_note()
    assert chr(0x1F680) in note and "preview for" not in note

    # Still backed off before RETRY_AFTER elapses.
    app.dispatch.now.advance(minutes=59)
    await app.stickers.refresh()
    await drain_dispatch(app.dispatch)
    assert len(downloads) == 3

    # Media model recovers and backoff expires: all 6 get described on retry.
    failing["on"] = False
    app.dispatch.now.advance(minutes=1)
    await app.stickers.refresh()
    await drain_dispatch(app.dispatch)
    assert len(downloads) == 9
    assert f"{chr(0x1F680)} (preview for file-0)" in app.stickers.turn_note()
    assert f"{chr(0x1F685)} (preview for file-5)" in app.stickers.turn_note()


async def test_removed_set_is_excluded_from_turn_note(make_app):
    app = make_app()
    other = "example_other_by_examplebot"
    sticker_bot(app, {SET: [("📚", "file-a")], other: [("🍵", "file-b")]})
    describer, _, _ = make_describer({"file-a": "Stack of books, cozy", "file-b": "Cup of tea, warm"})
    app.media_describer = describer

    await app.stickers.change("add", SET)
    await app.stickers.change("add", other)
    await drain_dispatch(app.dispatch)
    assert "Stack of books, cozy" in app.stickers.turn_note()
    assert "Cup of tea, warm" in app.stickers.turn_note()

    await app.stickers.change("remove", other)
    note = app.stickers.turn_note()
    assert "Stack of books, cozy" in note
    assert "Cup of tea, warm" not in note
    assert "🍵" not in note


async def test_dashboard_refresh_descriptions_is_owner_only_and_runs_in_background(make_app, monkeypatch):
    import httpx
    from dom.web.app import create_app
    from dom.web.auth import Auth
    from test_web_auth import ORIGIN, POST_HEADERS, TOKEN

    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)
    app = make_app()
    sticker_bot(app, {SET: [("📚", "file-a")]})
    version = {"n": 1}
    describer, downloads, _ = make_describer(lambda fid: f"Book cat v{version['n']}")
    app.media_describer = describer
    await app.stickers.change("add", SET)
    await drain_dispatch(app.dispatch)
    assert "Book cat v1" in app.stickers.turn_note()

    config = app.config.model_copy(update={
        "web": app.config.web.model_copy(update={"dev_token_env": "DOM_TEST_TOKEN"}),
    })
    web_app = create_app(app, Auth(config))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web_app), base_url=ORIGIN) as anon:
        denied = await anon.post("/api/stickers/refresh", json={}, headers=POST_HEADERS)
        assert denied.status_code in (401, 403)

    version["n"] = 2
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url=ORIGIN,
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.get("/api/chats")).json()["stickers"]
        resp = await client.post("/api/stickers/refresh", json={}, headers=POST_HEADERS)
        assert resp.status_code == 200
        assert "Sticker descriptions: describing 1 sticker in the background." in resp.json()["message"]

    await drain_dispatch(app.dispatch)
    assert "Book cat v2" in app.stickers.turn_note()
    assert downloads == ["file-a", "file-a"]
    assert any("Sticker descriptions:" in row["text"] for row in await events(app))
