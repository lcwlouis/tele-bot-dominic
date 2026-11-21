import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import EphemeralMessageParameters
from pydantic import ValidationError

from dom.actions import Actions
from dom.config import Config, load_config
from dom.telegram import executor
from test_owner_events import events
from test_traits import put
from test_usage import deliver, online
from tghelpers import FRIEND, GROUP, OWNER, tg, turn_requests

PRIVATE = '{"private": true, "bubbles": [{"text": "Owner-only answer."}], "react":"👍", "sticker":"📚"}'


def test_ephemeral_replies_default_to_disabled_and_can_be_enabled_via_toml(tmp_path):
    assert Config(home=tmp_path).ephemeral_replies is False
    config = tmp_path / "config.toml"
    config.write_text("")
    assert load_config(tmp_path).ephemeral_replies is False
    config.write_text("ephemeral_replies = true\n")
    assert load_config(tmp_path).ephemeral_replies is True


def test_private_action_is_a_boolean():
    assert Actions().private is False
    assert Actions(private=True).private is True
    with pytest.raises(ValidationError):
        Actions(private="yes")


def test_private_written_inside_the_bubbles_makes_the_whole_answer_private():
    # Live test 13: the model wrote {"bubbles": [{"text": ..., "private": true}, ...]} three times in a row and the
    # owner got nothing, because Bubble forbids extra keys.
    from dom.agent import parse_actions

    actions = parse_actions('{"bubbles": [{"text": "one", "private": true}, {"text": "two", "private": false}]}')
    assert actions.private is True and [b.text for b in actions.bubbles] == ["one", "two"]
    assert parse_actions('{"bubbles": [{"text": "one", "private": false}]}').private is False
    assert parse_actions('{"bubbles": [{"text": "one", "private": "yes"}]}').private is False


@pytest.mark.parametrize("chat_id,sender,enabled,offered", [
    (GROUP, OWNER, True, True),
    (GROUP, FRIEND, True, False),
    (GROUP, OWNER, False, False),
    (OWNER, OWNER, True, False),
    (FRIEND, FRIEND, True, False),
])
async def test_private_option_is_only_offered_on_enabled_owner_group_turns(make_app, chat_id, sender, enabled, offered):
    app = make_app()
    app.config = app.config.model_copy(update={"ephemeral_replies": enabled})
    await online(app, chat_id)
    await deliver(app, tg(chat_id, sender))
    prefix, context = [item["content"] for item in turn_requests(app)[0]["messages"]]
    assert '"private": true' not in prefix
    assert ('"private": true' in context) == offered
    if offered:
        assert "assistant" in context


async def test_owner_group_private_action_uses_ephemeral_bubbles_and_stays_out_of_group_history(make_app):
    app = make_app(contents=(PRIVATE,))
    app.config = app.config.model_copy(update={"ephemeral_replies": True})
    put(app.home, "private/USER.md", "PRIVATE OWNER NOTE")
    app.stickers.by_emoji = {"📚": ["file-a"]}
    await online(app, GROUP)
    await deliver(app, tg(GROUP, OWNER))
    (sent,) = app.bot.of("send", GROUP)
    assert sent[2] == "Owner-only answer."
    assert sent[3]["ephemeral_message_parameters"] == EphemeralMessageParameters(receiver_user_id=OWNER)
    assert not app.bot.of("react") and not app.bot.of("sticker")
    assert all(row.role != "assistant" for row in await app.store.get_recent_chat_window(GROUP))
    assert "PRIVATE OWNER NOTE" not in str(turn_requests(app))


@pytest.mark.parametrize("chat_id,sender,enabled", [
    (GROUP, OWNER, False), (GROUP, FRIEND, True), (OWNER, OWNER, True), (FRIEND, FRIEND, True),
])
async def test_ineligible_private_action_never_sends_ephemerally_or_falls_back_to_a_public_group_send(
    make_app, chat_id, sender, enabled,
):
    app = make_app(contents=('{"private":true,"bubbles":[{"text":"A reply."}]}',))
    if enabled:
        app.config = app.config.model_copy(update={"ephemeral_replies": True})
    await online(app, chat_id)
    await deliver(app, tg(chat_id, sender))
    sent = app.bot.of("send", chat_id)
    if chat_id < 0:
        assert sent == []
    else:
        assert [call[2] for call in sent] == ["A reply."]
        assert sent[0][3].get("ephemeral_message_parameters") is None


@pytest.mark.parametrize("sender,enabled", [(OWNER, False), (FRIEND, True)])
async def test_dropping_an_ineligible_private_group_reply_tells_the_owner(make_app, sender, enabled):
    app = make_app(contents=('{"private":true,"bubbles":[{"text":"A reply."}]}',))
    app.config = app.config.model_copy(update={"ephemeral_replies": enabled})
    await online(app, GROUP)
    await deliver(app, tg(GROUP, sender))
    assert app.bot.of("send", GROUP) == []
    assert [event["text"] for event in await events(app)] == [
        "Held back a private reply in The Group: it wasn't eligible",
    ]


async def test_a_refused_ephemeral_send_goes_to_the_owners_dm_with_why(make_app, caplog):
    # Live test 13: Dom is not an admin in the test group, Telegram answered BOT_NOT_ADMIN, and the owner got nothing.
    app = make_app()
    send = app.bot.send_message

    async def refuse_in_group(chat_id, text, **kw):
        if chat_id == GROUP:
            raise TelegramBadRequest(method=None, message="Bad Request: BOT_NOT_ADMIN")
        return await send(chat_id, text, **kw)

    app.bot.send_message = refuse_in_group
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(private=True, bubbles=[{"text": "One"}, {"text": "Two"}]),
        ephemeral_recipient=OWNER, sleep=app.sleep,
    )
    assert len(sent) == 1
    [dm] = app.bot.of("send", OWNER)
    assert "only when I'm a group admin" in dm[2] and dm[2].endswith("One\n\nTwo")
    assert app.bot.of("send", GROUP) == []
    assert "BOT_NOT_ADMIN" in caplog.text
    assert any("to the owner's DM" in event["text"] for event in await events(app))
