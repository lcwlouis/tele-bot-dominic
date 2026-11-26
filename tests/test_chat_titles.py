import json

import pytest

from dom.store.store import Store


async def test_set_chat_title_updates_known_chat_only(tmp_path):
    store = Store(tmp_path, 111)
    try:
        await store.get_chat_auth_status("-1009")  # known chat with no title, like an imported one
        await store.set_chat_title(-1009, "Book Club")
        await store.set_chat_title(-1777, "Stranger Group")  # unknown chat: no row is created
        await store.set_chat_title(-1009, "")  # empty titles never overwrite
        rows = dict(store._conn.execute("SELECT chat_id, title FROM chats").fetchall())
    finally:
        store.close()
    assert rows.get("-1009") == "Book Club"
    assert "-1777" not in rows


# --- titles for chats the bot has never seen ---

from datetime import timedelta
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetChat

from dom.chat_titles import GAP_SECONDS, KEY, PER_PASS, RETRY
from tghelpers import GROUP, OWNER

NEVER_SEEN = "-100777"


class AskedBot:
    """A bot whose get_chat answers from a table: a title, or an exception to raise. It records every ask."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    async def get_chat(self, chat_id):
        self.asked.append(chat_id)
        answer = self.answers[str(chat_id)]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(title=answer)


def refused():
    return TelegramForbiddenError(method=GetChat(chat_id=1), message="Forbidden: bot was kicked from the supergroup chat")


async def _app_with_notes(make_app, *ids):
    app = make_app()
    for chat_id in ids:
        app.home._write_regular(f"memory/chats/{chat_id}.md", "- A note {any}")
    return app


async def test_a_title_asked_of_telegram_is_kept_for_a_chat_with_no_row(make_app):
    app = await _app_with_notes(make_app, NEVER_SEEN)
    app.bot = AskedBot({NEVER_SEEN: "  Hiking   crew\n"})
    await app.chat_titles.refresh()
    assert app.bot.asked == [int(NEVER_SEEN)]
    assert await app.chat_titles.known() == {NEVER_SEEN: "Hiking crew"}
    assert await app.store.list_chats() == []  # no row is made for it
    await app.chat_titles.refresh()
    assert app.bot.asked == [int(NEVER_SEEN)]  # a known title is not asked for again


async def test_a_title_for_a_chat_with_a_row_but_no_title_goes_in_the_chats_table(make_app):
    app = await _app_with_notes(make_app, str(GROUP))
    await app.store.get_chat_auth_status(str(GROUP))  # a row with no title, like an imported one
    app.bot = AskedBot({str(GROUP): "Book Club"})
    await app.chat_titles.refresh()
    assert [chat["title"] for chat in await app.store.list_chats()] == ["Book Club"]
    assert await app.chat_titles.known() == {}  # the row holds it, not the kv table
    await app.chat_titles.refresh()
    assert len(app.bot.asked) == 1


async def test_a_refused_chat_is_marked_unknown_and_not_asked_again_for_a_day(make_app):
    app = await _app_with_notes(make_app, NEVER_SEEN)
    app.bot = AskedBot({NEVER_SEEN: refused()})
    await app.chat_titles.refresh()
    assert await app.chat_titles.known() == {}
    assert json.loads(await app.store.kv_get(KEY + NEVER_SEEN))["unknown"] is True
    await app.chat_titles.refresh()
    assert len(app.bot.asked) == 1  # too soon
    app.dispatch.now = lambda start=app.dispatch.now(): start + RETRY + timedelta(minutes=1)
    app.bot.answers[NEVER_SEEN] = "Now a member"
    await app.chat_titles.refresh()
    assert len(app.bot.asked) == 2 and await app.chat_titles.known() == {NEVER_SEEN: "Now a member"}


@pytest.mark.parametrize("error", [
    TelegramRetryAfter(method=GetChat(chat_id=1), message="Too Many Requests", retry_after=30),
    TelegramNetworkError(method=GetChat(chat_id=1), message="network down"),
    RuntimeError("boom"),
])
async def test_a_rate_limit_or_a_network_failure_marks_nothing_and_stops_the_pass(make_app, error):
    app = await _app_with_notes(make_app, NEVER_SEEN, "-100778")
    app.bot = AskedBot({NEVER_SEEN: error, "-100778": "Never reached"})
    await app.chat_titles.refresh()
    assert app.bot.asked == [int(NEVER_SEEN)]
    assert await app.store.kv_prefix(KEY) == []


async def test_a_pass_asks_a_few_chats_at_a_time_with_a_gap(make_app):
    ids = [f"-1008{n:02d}" for n in range(PER_PASS + 2)]
    app = await _app_with_notes(make_app, *ids)
    app.bot = AskedBot({chat_id: f"Chat {chat_id}" for chat_id in ids})
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    app.sleep = sleep
    await app.chat_titles.refresh()
    assert len(app.bot.asked) == PER_PASS and sleeps == [GAP_SECONDS] * (PER_PASS - 1)
    await app.chat_titles.refresh()
    assert len(app.bot.asked) == len(ids)  # the rest on the next pass


async def test_only_group_notes_are_looked_up(make_app):
    app = await _app_with_notes(make_app, "222", str(OWNER))
    app.bot = AskedBot({})
    await app.chat_titles.refresh()
    assert app.bot.asked == []


async def test_the_dashboard_shows_the_asked_title_instead_of_group_and_the_id(make_app, browser):
    app = await _app_with_notes(make_app, NEVER_SEEN, "-100778")
    app.bot = AskedBot({NEVER_SEEN: "Hiking crew", "-100778": refused()})
    await app.chat_titles.refresh()
    async with browser(app) as client:
        memory = (await client.get("/api/memory")).json()
        labels = {f["path"]: f["label"] for section in memory["sections"] for f in section["files"]}
        assert labels[f"memory/chats/{NEVER_SEEN}.md"] == "Hiking crew"
        assert labels["memory/chats/-100778.md"] == "Group -100778"  # refused: still just the id
        assert memory["chat_names"][NEVER_SEEN] == "Hiking crew" and "-100778" not in memory["chat_names"]
