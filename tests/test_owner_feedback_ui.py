"""Owner feedback from the phone, 2026-11-22: sticker share links, the Own work layout and clearer approval cards."""

from pathlib import Path

import pytest

from dom.stickers import set_name
from test_stickers import SET, sticker_bot
from test_usage import deliver
from test_web_pages import browser  # noqa: F401  (fixture)
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
        html = (await client.get("/chats")).text
    assert f'href="{SHARE}{SET}"' in html and 'rel="noopener noreferrer"' in html
    assert "Set name or link" in html


def test_own_work_tasks_stack_instead_of_using_the_chat_grid():
    css = (Path(__file__).parents[1] / "dom/web/static/dashboard.css").read_text()
    assert "#own-work .chat { display: block; }" in css


async def test_a_stranger_dm_card_shows_the_telegram_id_and_first_message(make_app, browser):  # noqa: F811
    app = make_app()
    await deliver(app, tg(chat_id=STRANGER, sender=STRANGER, text="hey, Fran here from the climbing gym"))
    await deliver(app, tg(chat_id=STRANGER, sender=STRANGER, text="second message", msg_id=2))
    [prompt] = [call[2] for call in app.bot.calls if call[0] == "send" and call[1] == OWNER]
    assert f"Telegram ID {STRANGER}" in prompt and '"hey, Fran here from the climbing gym"' in prompt
    async with browser(app) as client:
        html = (await client.get("/proposals")).text
    assert f"Telegram ID {STRANGER}" in html
    assert "First message: hey, Fran here from the climbing gym" in html and "second message" not in html
