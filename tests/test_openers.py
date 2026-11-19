from datetime import timedelta

import httpx
import pytest

from dom.models import StoredMessage
from dom.presence import parse_habits
from test_usage import chat, completion, online, usage_rows
from tghelpers import FINAL, GROUP, OWNER, turn_requests

NO_RESPONSE = '{"no_response": true}'


class Draw:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


async def record(app, msg_id, *, chat_id=OWNER, role="user", minutes_ago=60):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), msg_id=msg_id, role=role, is_bot=role == "assistant",
        sender_user_id=chat_id if role == "user" else None,
        visibility_scope=app.store.visibility_for(chat_id),
        text="book chat", created_at=(app.dispatch.now() - timedelta(minutes=minutes_ago)).isoformat(),
    ))


def always_try(app):
    openers = app.dispatch.openers
    openers.rng = Draw(0)
    return openers, app.dispatch.now


@pytest.mark.parametrize("reason", ["disabled", "blocked", "sleep", "spacing", "activity", "random", "unanswered"])
async def test_each_opener_condition_blocks_on_its_own(make_app, reason):
    from dom.config import OpenersConfig

    app = make_app()
    await online(app, OWNER)
    openers, now = always_try(app)
    if reason == "disabled":
        app.config = app.config.model_copy(update={"openers": OpenersConfig(enabled=False)})
    elif reason == "blocked":
        await app.store.set_chat_auth_status(OWNER, "blocked")
    elif reason == "sleep":
        await app.dispatch.presence.sleep(OWNER, parse_habits(""))
    elif reason == "spacing":
        await app.store.kv_set("opener:last_attempt", (now() - timedelta(hours=2, minutes=59)).isoformat())
    elif reason == "activity":
        await record(app, 100, minutes_ago=44)
    elif reason == "random":
        openers.rng = Draw(0.9)
    elif reason == "unanswered":
        await record(app, 100, role="assistant", minutes_ago=240)
        await app.store.kv_set("opener:last_msg_id", "100")

    await openers.maybe(now())
    assert app.script.requests == []
    assert app.bot.of("send") == []
    if reason != "spacing":
        assert await app.store.kv_get("opener:last_attempt") is None


async def test_owner_only_normal_turn_with_daily_cap_and_persisted_unanswered_check(make_app):
    app = make_app()
    await online(app, GROUP)
    await record(app, 100, chat_id=GROUP)
    openers, now = always_try(app)
    await openers.maybe(now())
    first = await app.store.kv_get("opener:last_msg_id")
    assert first is not None
    messages = await app.store.get_recent_chat_window(OWNER)
    assert [(m.role, m.msg_id) for m in messages] == [("assistant", int(first))]
    assert "Nobody has written for a while." in turn_requests(app)[0]["messages"][1]["content"]
    assert app.bot.of("send", GROUP) == []

    now.advance(hours=3)
    await openers.maybe(now())  # last owner DM message is still the opener
    assert len(app.script.requests) == 1
    await record(app, 6000, minutes_ago=45)  # answer the opener
    app.bot._next_id = 6000
    await openers.maybe(now())
    second = await app.store.kv_get("opener:last_msg_id")
    assert second != first
    await record(app, 6002, minutes_ago=45)
    now.advance(hours=3)
    await openers.maybe(now())
    assert len(app.script.requests) == len(app.bot.of("send", OWNER)) == 2

    now.advance(days=1)
    await openers.maybe(now())
    assert len(app.bot.of("send", OWNER)) == 3


async def test_opener_no_response_counts_as_attempt_only(make_app):
    app = make_app(contents=(NO_RESPONSE, NO_RESPONSE, FINAL))
    openers, now = always_try(app)
    await openers.maybe(now())
    assert not app.bot.of("send")
    assert await app.store.kv_get("opener:last_msg_id") is None
    assert await app.store.kv_get("opener:last_attempt") == now().isoformat()
    await openers.maybe(now())
    assert len(app.script.requests) == 1
    now.advance(hours=3)
    await openers.maybe(now())
    now.advance(hours=3)
    await openers.maybe(now())
    assert len(app.script.requests) == 3
    assert len(app.bot.of("send", OWNER)) == 1


async def test_openers_never_use_other_chat_if_owner_missing(make_app):
    app = make_app(owner_id=None)
    await online(app, GROUP)
    openers, now = always_try(app)
    await openers.maybe(now())
    assert not app.script.requests and not app.bot.of("send")


async def test_opener_model_runs_without_turn_lock_and_discards_when_owner_writes(make_app):
    app = make_app()
    openers, now = always_try(app)

    async def respond(request):
        assert not app.locks[OWNER].locked()
        await record(app, 100, minutes_ago=0)
        return completion(FINAL, {"cost": 0.01})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        await openers.maybe(now())

    assert not app.bot.of("send", OWNER)
    assert usage_rows(app)[-1][0] == "chat"
    assert usage_rows(app)[-1][3] == OWNER


async def test_opener_records_partial_delivery(make_app):
    app = make_app(contents=('{"bubbles": [{"text": "one"}, {"text": "two"}]}',))
    openers, now = always_try(app)
    original = app.bot.send_message

    async def send(chat_id, text, **kwargs):
        if text == "two":
            raise RuntimeError("delivery failed")
        return await original(chat_id, text, **kwargs)

    app.bot.send_message = send
    await openers.maybe(now())
    [first] = await app.store.get_recent_chat_window(OWNER)
    assert first.text == "one"
    assert await app.store.kv_get("opener:last_msg_id") == str(first.msg_id)
