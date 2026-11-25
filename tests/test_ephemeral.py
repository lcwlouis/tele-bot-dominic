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
    window = await app.store.get_recent_chat_window(GROUP)
    assert all(row.role != "assistant" for row in window)
    # Live test 13: without a note, Dom thought the owner was still unanswered and gave the same take publicly.
    [event] = [row for row in window if row.role == "event"]
    assert "answered the owner" in event.text and "Owner-only answer." not in event.text
    assert event.visibility_scope == f"chat:{GROUP}"
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


@pytest.mark.parametrize("sender,enabled,reason", [
    (OWNER, False, "ephemeral replies are off"),
    (FRIEND, True, "it was a friend's turn"),
])
async def test_dropping_an_ineligible_private_group_reply_tells_the_owner(make_app, sender, enabled, reason):
    app = make_app(contents=('{"private":true,"bubbles":[{"text":"A reply."}]}',))
    app.config = app.config.model_copy(update={"ephemeral_replies": enabled})
    await online(app, GROUP)
    await deliver(app, tg(GROUP, sender))
    assert app.bot.of("send", GROUP) == []
    assert [event["text"] for event in await events(app)] == [
        f"Held back a private reply in The Group: {reason}",
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


async def test_where_dom_is_not_admin_a_private_reply_goes_straight_to_the_dm(make_app):
    # The owner's ask: know whether Dom is an admin instead of finding out from a refused send.
    from types import SimpleNamespace

    app = make_app(contents=(PRIVATE,))
    app.config = app.config.model_copy(update={"ephemeral_replies": True})
    asked = []

    async def get_chat_member(chat_id, user_id):
        asked.append(chat_id)
        return SimpleNamespace(status="member")

    app.bot.get_chat_member = get_chat_member
    await online(app, GROUP)
    await deliver(app, tg(GROUP, OWNER))
    assert app.bot.of("send", GROUP) == []  # no doomed in-group attempt
    [dm] = app.bot.of("send", OWNER)
    assert "only when I'm a group admin" in dm[2] and dm[2].endswith("Owner-only answer.")
    assert "where only the owner can see it" in str(turn_requests(app))
    assert asked == [GROUP]  # asked once, then kept


async def test_becoming_a_group_admin_is_noted_from_the_membership_update(make_app):
    from types import SimpleNamespace

    from dom.telegram.handlers import handle_my_chat_member

    app = make_app()
    update = SimpleNamespace(
        chat=SimpleNamespace(id=GROUP, type="supergroup", title="The Group"),
        new_chat_member=SimpleNamespace(status="administrator"),
        from_user=SimpleNamespace(id=OWNER, username="owner", full_name="Owner"),
    )
    await handle_my_chat_member(app, update)
    assert await app.dispatch._is_admin(GROUP) is True
    update.new_chat_member.status = "member"
    await handle_my_chat_member(app, update)
    assert await app.dispatch._is_admin(GROUP) is False


async def test_a_long_private_reply_moved_to_the_dm_is_split_to_fit_telegram(make_app):
    # Six bubbles of up to 4000 characters joined into one DM would pass Telegram's 4096 limit and be lost.
    app = make_app()
    sent = []

    async def send(chat_id, text, **kw):
        if chat_id == GROUP:
            raise TelegramBadRequest(method=None, message="Bad Request: BOT_NOT_ADMIN")
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        sent.append(text)
        return type("Sent", (), {"message_id": len(sent)})()

    app.bot.send_message = send
    bubbles = [{"text": "a" * 3000}, {"text": "😀" * 1500}, {"text": "short"}]
    ids = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(private=True, bubbles=bubbles), ephemeral_recipient=OWNER, sleep=app.sleep,
    )
    assert ids == [1, 2]  # the emoji count double toward the limit, and "short" still fits after them
    assert sent[0].startswith("My private reply in") and sent[0].endswith("a" * 3000)
    assert sent[1] == "😀" * 1500 + "\n\nshort"


async def test_a_private_group_reply_of_only_fake_media_sends_no_header_only_dm(make_app):
    app = make_app()
    sent = await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(private=True, bubbles=[{"text": "[photo] the view"}]),
        ephemeral_recipient=OWNER, ephemeral_in_chat=False, sleep=app.sleep,
    )
    assert not sent and app.bot.of("send") == []


async def test_a_private_reply_refused_everywhere_leaves_no_trace_in_the_group(make_app):
    # Review of #77: a note in the group's own history would reveal that a private reply for the owner existed.
    app = make_app()

    async def refuse(chat_id, text, **kw):
        raise TelegramBadRequest(method=None, message="Bad Request: chat not found")

    app.bot.send_message = refuse
    await executor.perform(
        app.bot, app.store, GROUP, 1, Actions(private=True, bubbles=[{"text": "Owner-only"}]),
        ephemeral_recipient=OWNER, sleep=app.sleep,
    )
    assert await events(app, GROUP) == []
    assert any("Held back a private reply" in event["text"] for event in await events(app))


async def test_media_in_a_private_group_reply_is_noted_for_the_owner_only(make_app):
    app = make_app()
    actions = Actions(private=True, bubbles=[{"text": "Owner-only"}], image="a desk at night")
    screened = await app.dispatch._screen_studio(GROUP, "group", actions, "The Group")
    assert screened.image is None
    assert await events(app, GROUP) == []
    assert any("private reply from The Group" in event["text"] for event in await events(app))
