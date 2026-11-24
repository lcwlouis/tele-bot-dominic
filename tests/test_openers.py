import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from dom.config import OpenersConfig
from dom.models import StoredMessage
from dom.presence import parse_habits
from test_usage import chat, completion, online, usage_rows
from tghelpers import FINAL, FRIEND, GROUP, OWNER, turn_requests

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


def always_try(app, **settings):
    app.config = app.config.model_copy(update={"openers": OpenersConfig(**(settings or {"enabled": True}))})
    openers = app.dispatch.openers
    openers.rng = Draw(0)
    return openers, app.dispatch.now


def test_openers_are_off_by_default_and_in_the_starter_config():
    from dom.config import starter_config

    for switch in ("enabled", "groups", "friends"):
        assert getattr(OpenersConfig(), switch) is False
        assert starter_config()["openers"][switch] is False


@pytest.mark.parametrize("reason", ["disabled", "blocked", "sleep", "spacing", "activity", "random", "unanswered"])
async def test_each_opener_condition_blocks_on_its_own(make_app, reason):
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
    await ready_chat(app, GROUP)
    await ready_chat(app, FRIEND)
    openers, now = always_try(app)
    await openers.maybe(now())
    first = await app.store.kv_get("opener:last_msg_id")
    assert first is not None
    messages = await app.store.get_recent_chat_window(OWNER)
    assert [(m.role, m.msg_id) for m in messages] == [("assistant", int(first))]
    assert "Nobody has written for a while." in turn_requests(app)[0]["messages"][1]["content"]
    assert app.bot.of("send", GROUP) == app.bot.of("send", FRIEND) == []

    now.advance(hours=3)
    await openers.maybe(now())  # last owner DM message is still the opener
    assert len(app.script.requests) == 1
    await record(app, 6000, minutes_ago=45)  # answer the opener
    await app.store.mark_chat_read(OWNER, 6000)
    app.bot._next_id = 6000
    await openers.maybe(now())
    second = await app.store.kv_get("opener:last_msg_id")
    assert second != first
    await record(app, 6002, minutes_ago=45)
    await app.store.mark_chat_read(OWNER, 6002)
    now.advance(hours=3)
    await openers.maybe(now())
    assert len(app.script.requests) == len(app.bot.of("send", OWNER)) == 2

    now.advance(days=1)
    await openers.maybe(now())
    assert len(app.bot.of("send", OWNER)) == 3


async def test_opener_waits_while_the_owner_has_unread_messages(make_app):
    app = make_app()
    await online(app, OWNER)
    await record(app, 100, minutes_ago=50)  # quiet long enough, but Dom has not read it yet
    openers, now = always_try(app)
    await openers.maybe(now())
    assert app.script.requests == [] and app.bot.of("send") == []
    assert await app.store.kv_get("opener:last_attempt") is None

    await app.store.mark_chat_read(OWNER, 100)
    await openers.maybe(now())
    assert len(app.script.requests) == 1


async def test_shutdown_finishes_an_opener_that_started_sending(make_app, monkeypatch):
    app = make_app()
    openers, now = always_try(app)
    sent, release = asyncio.Event(), asyncio.Event()
    send = app.bot.send_message

    async def send_then_pause(*args, **kwargs):
        result = await send(*args, **kwargs)
        sent.set()
        await release.wait()
        return result

    monkeypatch.setattr(app.bot, "send_message", send_then_pause)
    opener = app.dispatch._spawn(openers.maybe(now()))
    await asyncio.wait_for(sent.wait(), timeout=2)
    closing = asyncio.create_task(app.dispatch.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, timeout=2)

    assert not opener.cancelled()
    assert len(app.bot.of("send", OWNER)) == 1
    [stored] = await app.store.get_recent_chat_window(OWNER)
    assert await app.store.kv_get("opener:last_msg_id") == str(stored.msg_id)
    assert await app.store.kv_get("opener:sent") == f"{openers._day(now())}/1"


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


async def test_opener_only_goes_online_once_it_actually_texts(make_app):
    app = make_app(contents=(NO_RESPONSE, FINAL))
    openers, now = always_try(app)
    before = await app.store.get_presence(OWNER)
    await openers.maybe(now())
    assert len(app.script.requests) == 1 and not app.bot.of("send")
    assert await app.store.get_presence(OWNER) == before

    now.advance(hours=3)
    await openers.maybe(now())
    assert len(app.bot.of("send", OWNER)) == 1
    habits = parse_habits("")
    state, until = app.dispatch.presence.peek(await app.store.get_presence(OWNER), habits)
    assert state == "online" and until > now()


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


async def ready_chat(app, chat_id, *, minutes_ago=180, msg_id=100):
    await online(app, chat_id)
    await record(app, msg_id, chat_id=chat_id, minutes_ago=minutes_ago)
    await app.store.mark_chat_read(chat_id, msg_id)


def group_permissions(app, monkeypatch, *, status="member", may_send=True):
    async def get_chat_member(chat_id, user_id, **options):
        assert not app.locks[chat_id].locked()
        return SimpleNamespace(status=status, is_member=True, can_send_messages=may_send)

    async def get_chat(chat_id, **options):
        assert not app.locks[chat_id].locked()
        return SimpleNamespace(type="supergroup", permissions=SimpleNamespace(can_send_messages=may_send))

    monkeypatch.setattr(app.bot, "get_chat_member", get_chat_member, raising=False)
    monkeypatch.setattr(app.bot, "get_chat", get_chat, raising=False)


@pytest.mark.parametrize(("chat_id", "setting", "minutes"), [(FRIEND, "friends", 45), (GROUP, "groups", 180)])
@pytest.mark.parametrize("reason", ["eligible", "recent", "offline", "sleep", "left", "pending", "unread"])
async def test_non_owner_opener_eligibility(make_app, monkeypatch, chat_id, setting, minutes, reason):
    app = make_app()
    openers, now = always_try(app, **{setting: True})
    group_permissions(app, monkeypatch)
    await ready_chat(app, chat_id, minutes_ago=minutes - (reason == "recent"))
    other = GROUP if chat_id == FRIEND else FRIEND
    await ready_chat(app, other)
    if reason == "offline":  # the session ended long ago: no deadlines left, as a chat looks hours later
        await app.store.set_presence(
            chat_id, is_sleeping=False, sleep_until=None, is_offline=True, offline_until=None, online_until=None,
        )
    elif reason == "sleep":
        await app.dispatch.presence.sleep(chat_id, parse_habits(""))
    elif reason in ("left", "pending"):
        await app.store.set_chat_auth_status(chat_id, "left" if reason == "left" else "pending_approval")
    elif reason == "unread":
        await record(app, 101, chat_id=chat_id, minutes_ago=minutes)

    await openers.maybe(now())

    # An online session lasts minutes, far shorter than the quiet stretch, so offline is the usual case: the opener
    # brings Dom online. Asleep never opens.
    expected = int(reason in ("eligible", "offline"))
    assert len(app.script.requests) == len(app.bot.of("send", chat_id)) == expected
    assert app.bot.of("send", other) == app.bot.of("send", OWNER) == []
    if reason == "offline":
        state, _ = app.dispatch.presence.peek(await app.store.get_presence(chat_id), parse_habits(""))
        assert state == "online"


@pytest.mark.parametrize("status", ["member", "restricted", "left"])
async def test_group_opener_checks_telegram_send_permissions(make_app, monkeypatch, status):
    app = make_app()
    openers, now = always_try(app, groups=True)
    await ready_chat(app, GROUP)
    group_permissions(app, monkeypatch, status=status, may_send=False)
    await openers.maybe(now())
    assert not app.script.requests and not app.bot.of("send")


async def test_opener_caps_and_spacing_are_per_chat(make_app, monkeypatch):
    app = make_app(contents=(FINAL, NO_RESPONSE, FINAL, FINAL))
    group_permissions(app, monkeypatch)
    openers, now = always_try(app, groups=True, friends=True, max_per_day=1)
    await ready_chat(app, GROUP)
    await ready_chat(app, FRIEND)
    await openers.maybe(now())  # group sends
    await openers.maybe(now())  # friend passes
    assert await app.store.kv_get(f"opener:sent:{GROUP}") == f"{openers._day(now())}/1"
    assert await app.store.kv_get(f"opener:sent:{FRIEND}") is None

    await openers.maybe(now())  # wrap the cursor
    await openers.maybe(now())  # friend's attempt spacing still applies
    assert len(app.script.requests) == 2

    now.advance(hours=3)
    app.bot._next_id = 6000
    await ready_chat(app, GROUP, msg_id=6000)
    await ready_chat(app, FRIEND)
    await openers.maybe(now())
    assert len(app.bot.of("send", GROUP)) == len(app.bot.of("send", FRIEND)) == 1
    assert len(app.script.requests) == 3

    now.advance(days=1)
    await ready_chat(app, GROUP, msg_id=6000)
    await openers.maybe(now())  # wrap at the end of the page
    await openers.maybe(now())
    assert len(app.bot.of("send", GROUP)) == 2


async def test_friend_opener_uses_friend_scope_and_attributes_spend(make_app):
    app = make_app()
    openers, now = always_try(app, enabled=True, friends=True)
    await ready_chat(app, FRIEND)
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "Confidential owner preference")
    prompts = []

    def respond(request):
        assert not app.locks[FRIEND].locked()
        prompts.append(request.content.decode())
        return completion(FINAL, {"cost": 0.01})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        await openers.maybe(now())

    owner_prompt, friend_prompt = prompts
    assert "Confidential owner preference" in owner_prompt
    assert "Confidential owner preference" not in friend_prompt
    assert "book chat" in friend_prompt and "(friend_dm)" in friend_prompt
    assert usage_rows(app)[-1][3] == FRIEND
    assert len(app.bot.of("send", FRIEND)) == 1


async def test_non_owner_opener_obeys_spend_cap(make_app):
    app = make_app()
    openers, now = always_try(app, friends=True)
    await ready_chat(app, FRIEND)
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.01})
    await app.store.record_usage(now().isoformat(), "chat", "model", "example.org", FRIEND, 0, 0, 0.01, 0)
    await openers.maybe(now())
    assert not app.script.requests and not app.bot.of("send", FRIEND)


async def test_group_opener_cancels_if_muted_during_compose(make_app, monkeypatch):
    app = make_app()
    openers, now = always_try(app, groups=True)
    await ready_chat(app, GROUP)
    group_permissions(app, monkeypatch)

    def respond(request):
        group_permissions(app, monkeypatch, status="restricted", may_send=False)
        return completion(FINAL, {"cost": 0.01})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app.client = chat(http, app)
        await openers.maybe(now())
    assert not app.bot.of("send", GROUP)
    assert await app.store.kv_get(f"opener:sent:{GROUP}") is None
