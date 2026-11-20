import json
import logging
import os
from random import Random
from types import SimpleNamespace

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from pydantic import ValidationError

from dom.actions import Actions, Bubble
from dom.cli import gif_search
from dom.config import load_config
from dom.gifs import MAX_BYTES, GifSearch
from dom.tools import SELF_INSPECT_TOOL, TurnCtx
from dom.telegram import executor
from test_stickers import SET, sticker_bot
from test_traits import put
from test_usage import deliver, online
from tghelpers import GROUP, OWNER, tg, turn_requests

KEY = "fake-klipy-key"


def item(slug, md=(10_000, "https://static.klipy.com/a.mp4"), sm=(5_000, "https://static.klipy.com/a-sm.mp4"), kind="gif"):
    def file(size, url):
        return {"mp4": {"url": url, "width": 320, "height": 320, "size": size}, "gif": {"url": "https://static.klipy.com/x.gif"}}

    tiers = {name: file(*value) for name, value in (("md", md), ("sm", sm)) if value is not None}
    return {"id": 1, "slug": slug, "title": slug, "type": kind, "file": tiers}


def payload(*items):
    return {"result": True, "data": {"data": list(items), "current_page": 1, "per_page": 10, "has_next": False}}


def search(respond, rng=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return GifSearch(KEY, http, rng or Random(0)), http


async def test_request_is_a_safe_search_for_short_mp4s_and_never_sends_chat_data():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=payload(item("hello")))

    gifs, http = search(respond)
    async with http:
        assert await gifs.find("happy dance") == "https://static.klipy.com/a.mp4"
    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == "api.klipy.com"
    assert request.url.path == f"/api/v1/{KEY}/gifs/search"
    params = dict(request.url.params)
    assert params["q"] == "happy dance"
    assert params["content_filter"] == "high"
    assert params["format_filter"] == "mp4"
    assert params["customer_id"] == "dom"
    assert 8 <= int(params["per_page"]) <= 50
    assert request.extensions["timeout"]["read"] <= 10


async def test_pick_is_random_among_usable_results_and_skips_the_rest():
    big = (MAX_BYTES + 1, "https://static.klipy.com/big.mp4")
    results = payload(
        item("ad", kind="ad"),
        item("big", md=big, sm=big),
        item("sm-only", md=None, sm=(900, "https://static.klipy.com/s.mp4")),
        item("elsewhere", md=(900, "https://example.org/x.mp4"), sm=None),
        item("plain-http", md=(900, "http://static.klipy.com/x.mp4"), sm=None),
        item("lookalike", md=(900, "https://static.klipy.com.example.org/x.mp4"), sm=None),
        item("ok", md=(900, "https://static1.klipy.com/ok.mp4"), sm=None),
        {"slug": "garbage", "file": "nope"},
        "not an object",
    )
    gifs, http = search(lambda _: httpx.Response(200, json=results))
    async with http:
        picks = {await gifs.find("x") for _ in range(40)}
    assert picks == {"https://static.klipy.com/s.mp4", "https://static1.klipy.com/ok.mp4"}


@pytest.mark.parametrize("response", [
    httpx.Response(200, json=payload()),
    httpx.Response(200, json={"result": False}),
    httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={"data": {"data": "no"}}),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(429, json={"result": False}),
    httpx.Response(500),
])
async def test_bad_answers_mean_no_gif(response):
    gifs, http = search(lambda _: response)
    async with http:
        assert await gifs.find("x") is None


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
async def test_network_failures_mean_no_gif_and_the_key_is_never_logged(error, caplog):
    def respond(request):
        raise type(error)(f"failed for {request.url}", request=request)

    gifs, http = search(respond)
    async with http:
        with caplog.at_level(logging.DEBUG):
            assert await gifs.find("x") is None
    assert "gif search failed" in caplog.text and KEY not in caplog.text


async def test_a_hung_search_is_cut_off(monkeypatch):
    import asyncio

    import dom.gifs

    monkeypatch.setattr(dom.gifs, "TIMEOUT_SECONDS", 0.01)

    async def hang(request):
        await asyncio.sleep(5)

    gifs, http = search(hang)
    async with http:
        assert await gifs.find("x") is None


@pytest.mark.parametrize("value", [5, ["cat"], "", "x" * 51])
def test_action_rejects_invalid_gif_queries(value):
    with pytest.raises(ValidationError):
        Actions(gif=value)
    assert Actions(gif="cat typing").gif == "cat typing"
    assert Actions().gif is None


class StubGifs:
    def __init__(self, url="https://static.klipy.com/a.mp4"):
        self.url, self.queries = url, []

    async def find(self, query):
        self.queries.append(query)
        return self.url


def animation_bot(app, error=None):
    async def send_animation(chat_id, animation, **kwargs):
        if error is not None:
            raise error
        app.bot._next_id += 1
        app.bot.calls.append(("animation", chat_id, animation, kwargs))
        return SimpleNamespace(message_id=app.bot._next_id)

    app.bot.send_animation = send_animation


async def test_executor_sends_the_gif_by_url_after_bubbles_and_records_it(make_app):
    app = make_app()
    animation_bot(app)
    gifs = StubGifs()
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(gif="celebration", bubbles=[Bubble(text="We did it")]),
        gifs=gifs, sleep=app.sleep, thread_id=7,
    )
    assert [call[0] for call in app.bot.calls] == ["action", "send", "animation"]
    assert app.bot.of("animation")[0][1:3] == (GROUP, "https://static.klipy.com/a.mp4")
    assert app.bot.of("animation")[0][3] == {"message_thread_id": 7}
    assert gifs.queries == ["celebration"] and len(sent) == 2
    latest = await app.store.get_recent_chat_window(GROUP, 1)
    assert latest[0].text == "[gif]" and latest[0].role == "assistant"
    assert json.loads(latest[0].media_json) == {"kind": "gif", "query": "celebration"}


async def test_gif_goes_after_a_sticker(make_app):
    app = make_app()
    animation_bot(app)
    sticker_bot(app, {SET: [("📚", "file-a")]})
    await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(gif="books", sticker="📚"), gifs=StubGifs(),
        stickers={"📚": ["file-a"]}, sleep=app.sleep,
    )
    assert [call[0] for call in app.bot.calls] == ["sticker", "animation"]


@pytest.mark.parametrize("gifs,error", [
    (None, None),
    (StubGifs(url=None), None),
    (StubGifs(), TelegramNetworkError(method=None, message="down")),
])
async def test_any_gif_failure_leaves_the_turn_intact(make_app, gifs, error):
    app = make_app()
    animation_bot(app, error)
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(gif="cat", bubbles=[Bubble(text="Hi")]), gifs=gifs, sleep=app.sleep,
    )
    assert app.bot.of("animation") == [] and len(sent) == 1
    assert app.bot.of("send")[0][2] == "Hi"


async def test_a_crashing_provider_is_not_fatal_to_the_turn(make_app):
    app = make_app()
    animation_bot(app)

    class Broken:
        async def find(self, query):
            raise RuntimeError("boom")

    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(gif="cat", bubbles=[Bubble(text="Hi")]), gifs=Broken(), sleep=app.sleep,
    )
    assert len(sent) == 1 and app.bot.of("animation") == []


async def test_private_replies_never_send_a_gif(make_app):
    app = make_app()
    animation_bot(app)
    gifs = StubGifs()
    await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(gif="cat", private=True), gifs=gifs, sleep=app.sleep,
    )
    assert gifs.queries == [] and app.bot.calls == []


async def test_turn_offers_the_action_only_with_a_provider_and_sends_it(make_app):
    app = make_app(contents=('{"bubbles":[{"text":"nice"}],"gif":"thumbs up"}',))
    animation_bot(app)
    app.gifs = StubGifs()
    await online(app)
    await deliver(app, tg())
    _, context = [row["content"] for row in turn_requests(app)[0]["messages"]]
    assert 'Optional action "gif"' in context
    assert app.gifs.queries == ["thumbs up"] and app.bot.of("animation")
    assert [call[0] for call in app.bot.calls][-1] == "animation"



async def test_turn_does_not_offer_the_action_without_a_provider(make_app):
    app = make_app()
    await online(app)
    await deliver(app, tg())
    assert 'action "gif"' not in turn_requests(app)[0]["messages"][1]["content"]


async def test_a_gif_alone_is_a_complete_reply(make_app):
    app = make_app(contents=('{"bubbles":[],"gif":"wave"}',))
    animation_bot(app)
    app.gifs = StubGifs()
    await online(app)
    await deliver(app, tg())
    assert app.bot.of("animation") and app.bot.of("send") == []


async def test_self_inspect_says_whether_gifs_are_on(make_app):
    app = make_app()
    ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, app.store, app.owner_id, app=app)
    off = json.loads(await SELF_INSPECT_TOOL.handler({}, ctx))["gifs"]
    assert off.startswith("off") and "KLIPY_API_KEY" in off
    app.gifs = StubGifs()
    assert json.loads(await SELF_INSPECT_TOOL.handler({}, ctx))["gifs"] == "on"


async def test_gifs_are_on_only_with_a_key(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("")
    monkeypatch.delenv("KLIPY_API_KEY", raising=False)
    async with httpx.AsyncClient() as http:
        assert gif_search(load_config(tmp_path), http) is None
        (tmp_path / ".env").write_text(f"KLIPY_API_KEY={KEY}\n")
        assert isinstance(gif_search(load_config(tmp_path), http), GifSearch)


@pytest.mark.skipif(not os.environ.get("KLIPY_API_KEY"), reason="set KLIPY_API_KEY to search KLIPY for real")
async def test_live_search_returns_a_klipy_mp4():
    async with httpx.AsyncClient() as http:
        url = await GifSearch(os.environ["KLIPY_API_KEY"], http).find("thumbs up")
    assert url and url.startswith("https://") and url.endswith(".mp4")
