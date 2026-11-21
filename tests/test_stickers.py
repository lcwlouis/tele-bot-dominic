import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from pydantic import ValidationError

from dom.actions import Actions, Bubble
from dom.telegram import executor, handlers
from test_owner_events import events
from test_traits import put
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, tg, turn_requests

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
    # typing while Dom thinks, then typing for the bubble
    assert [call[0] for call in app.bot.calls] == ["action", "action", "send", "sticker"]
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
