"""Owner feedback from the phone, 2026-11-22: sticker share links, the Own work layout and clearer approval cards."""

import json

import pytest

from dom.stickers import set_name
from test_stickers import SET, sticker_bot
from test_usage import deliver
from tghelpers import OWNER, tg

SHARE = "https://t" + ".me/addstickers/"
STRANGER = 4243


@pytest.mark.parametrize("value", [
    SET, f"  {SET} ", SHARE + SET, SHARE + SET + "/", SHARE + SET + "?ref=x",
    "http://telegram" + ".me/addstickers/" + SET, "t" + ".me/addstickers/" + SET,
])
def test_a_set_is_named_or_linked(value):
    assert set_name(value) == SET


@pytest.mark.parametrize("value", ["", "https://example.org/addstickers/" + SET, SHARE, SHARE + "bad name", "9" + SET])
def test_anything_else_is_refused(value):
    with pytest.raises(ValueError):
        set_name(value)


async def test_adding_by_share_link_lists_the_set_with_a_preview_link(make_app, browser):  # noqa: F811
    app = make_app()
    sticker_bot(app, {SET: [("📚", "file-a")]})
    assert await app.stickers.change("add", SHARE + SET) == f"Added sticker set {SET}"
    assert app.stickers.names() == [SET]
    async with browser(app) as client:
        assert (await client.get("/api/chats")).json()["stickers"] == [SET]


async def test_a_stranger_dm_card_shows_the_telegram_id_and_first_message(make_app, browser):  # noqa: F811
    app = make_app()
    await deliver(app, tg(chat_id=STRANGER, sender=STRANGER, text="hey, Fran here from the climbing gym"))
    await deliver(app, tg(chat_id=STRANGER, sender=STRANGER, text="second message", msg_id=2))
    [prompt] = [call[2] for call in app.bot.calls if call[0] == "send" and call[1] == OWNER]
    assert f"Telegram ID {STRANGER}" in prompt and '"hey, Fran here from the climbing gym"' in prompt
    async with browser(app) as client:
        waiting = (await client.get("/api/waiting")).json()
    chat, = waiting["chats"]
    assert f"Telegram ID {STRANGER}" in chat["detail"]
    assert chat["first"] == "hey, Fran here from the climbing gym" and "second message" not in json.dumps(chat)


async def test_a_group_card_shows_its_telegram_id_too(make_app, browser):  # noqa: F811
    from tghelpers import GROUP

    app = make_app()
    await deliver(app, tg(chat_id=GROUP, sender=STRANGER, text="hi all"))
    async with browser(app) as client:
        waiting = (await client.get("/api/waiting")).json()
    assert f"Telegram ID {GROUP}" in waiting["chats"][0]["detail"]
