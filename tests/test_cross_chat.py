import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from aiogram.types import MessageEntity, User

from dom.agent import execute_tool_call
from dom.presence import parse_habits
from dom.telegram import owner
from dom.telegram.handlers import handle_callback, handle_message
from dom.tools import TurnCtx, enabled_tools
from test_owner_controls import callback
from test_web_auth import POST_HEADERS
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg


async def approve_chat(app, chat, title=None):
    await app.store.set_chat_auth_status(chat, "approved", title=title)


async def call_tool(app, scope, chat_id, name, **args):
    offered = {tool.name: tool for tool in await enabled_tools(app, scope)}
    ctx = TurnCtx(scope, chat_id, OWNER if chat_id == OWNER else FRIEND, app.home, app.store, OWNER, app=app)
    return await execute_tool_call(
        {"function": {"name": name, "arguments": json.dumps(args)}}, offered, ctx,
    )


async def draft(app, *, chat=GROUP, text="see you\n  there!", reason="Confirm the plan", voice=False):
    args = {"chat": chat, "text": text, "reason": reason}
    if voice:
        args["voice"] = True
    return await call_tool(app, "owner_dm", OWNER, "message_chat", **args)


async def test_cross_chat_voice_note_requires_approval_and_delivers_via_studio(make_app):
    from types import SimpleNamespace

    app = make_app()
    await approve_chat(app, GROUP, "Book club")
    app.config = app.config.model_copy(update={
        "studio": app.config.studio.model_copy(update={"enabled": True, "voice": True, "base_url": "http://127.0.0.1:8080"}),
    })

    class FakeStudio:
        ready = True

        def model_for(self, worker):
            return "fake-tts"

        async def voice(self, text):
            return b"OggS-fake-voice-bytes"

    sent_voices = []

    async def fake_send_voice(chat_id, voice, **kwargs):
        sent_voices.append((chat_id, voice.filename, voice.data))
        return SimpleNamespace(message_id=777)

    async def fake_chat_action(chat_id, action, **kwargs):
        return True

    app.studio = FakeStudio()
    app.bot.send_voice = fake_send_voice
    app.bot.send_chat_action = fake_chat_action
    reply = await draft(app, chat=GROUP, text="hey everyone, lunch at 2", reason="Send a voice update", voice=True)
    assert "Waiting for the owner's OK" in reply

    [proposal] = await app.proposals.pending()
    assert 'Send a voice note to "Book club"' in proposal["title"]
    assert json.loads(proposal["body_json"])["voice"] is True

    # Approve and deliver through CrossChat worker.
    assert await app.proposals.decide(proposal["id"], True) == "Approved"
    await process_ready(app)
    assert len(sent_voices) == 1 and sent_voices[0][0] == GROUP and sent_voices[0][2] == b"OggS-fake-voice-bytes"
    [item] = await app.store.cross_chat_items()
    assert item["status"] == "sent" and item["sent_msg_id"] == 777


async def test_cross_chat_video_note_requires_approval_and_delivers_via_studio(make_app):
    from types import SimpleNamespace

    app = make_app()
    await approve_chat(app, GROUP, "Book club")
    app.config = app.config.model_copy(update={
        "studio": app.config.studio.model_copy(update={
            "enabled": True, "video_notes": True, "video_scenes": False, "base_url": "http://127.0.0.1:8080",
        }),
    })

    class FakeStudio:
        ready = True

        def model_for(self, worker):
            return "fake-video"

        async def video(self, text, special=False, outfit="", still=None):
            return b"fake-mp4-video-note"

    sent_videos = []

    async def fake_send_video_note(chat_id, video, **kwargs):
        sent_videos.append((chat_id, video.filename, video.data))
        return SimpleNamespace(message_id=888)

    async def fake_chat_action(chat_id, action, **kwargs):
        return True

    app.studio = FakeStudio()
    app.bot.send_video_note = fake_send_video_note
    app.bot.send_chat_action = fake_chat_action
    reply = await call_tool(
        app, "owner_dm", OWNER, "message_chat",
        chat=GROUP, text="quick telebubble hello!", reason="Send a video note", video_note=True, scene="coffee shop",
    )
    assert "Waiting for the owner's OK" in reply

    [proposal] = await app.proposals.pending()
    assert 'Send a video note to "Book club"' in proposal["title"]
    body = json.loads(proposal["body_json"])
    assert body["video_note"] is True and body["scene"] == "coffee shop"

    assert await app.proposals.decide(proposal["id"], True) == "Approved"
    await process_ready(app)
    assert len(sent_videos) == 1 and sent_videos[0][0] == GROUP and sent_videos[0][2] == b"fake-mp4-video-note"
    [item] = await app.store.cross_chat_items()
    assert item["status"] == "sent" and item["sent_msg_id"] == 888


async def process_ready(app):
    while await app.dispatch.cross_chat.process_one():
        pass


@pytest.mark.parametrize("trigger", ["mention", "text_mention", "reply", "bot", "unapproved", "recent_owner", "near_match"])
async def test_alerts_from_telegram_ingress(make_app, monkeypatch, trigger):
    app = make_app()
    clock = app.dispatch.now
    clock.advance(hours=12, minutes=1)  # inside the default sleep hours
    username = "captain"
    handle = chr(64) + username
    await app.store.resolve_person(OWNER, handle, "Captain")
    if trigger != "unapproved":
        await approve_chat(app, GROUP)
    owner_user = User(id=OWNER, is_bot=False, first_name="Captain", username=username)
    reply = tg(sender=OWNER, msg_id=8, text="Earlier").model_copy(update={
        "from_user": owner_user, "date": clock() - timedelta(hours=1),
    })
    message = tg(
        chat_type="supergroup", text=f"🙂 {handle.title()} private question", msg_id=9,
        entities=[MessageEntity(type="mention", offset=3, length=8)],
    )
    if trigger == "text_mention":
        message = message.model_copy(update={
            "text": "Captain private question",
            "entities": [MessageEntity(type="text_mention", offset=0, length=7, user=owner_user)],
        })
    elif trigger == "reply":
        message = message.model_copy(update={"reply_to_message": reply, "entities": []})
    elif trigger == "bot":
        message = message.model_copy(update={"from_user": User(id=FRIEND, is_bot=True, first_name="Robot")})
    elif trigger == "recent_owner":
        await handle_message(app, reply.model_copy(update={"date": clock() - timedelta(minutes=2)}))
    elif trigger == "near_match":
        message = message.model_copy(update={
            "text": f"{handle}s private question", "entities": [MessageEntity(type="mention", offset=0, length=9)],
        })
    message = message.model_copy(update={
        "date": clock(),
        "chat": message.chat.model_copy(update={"id": GROUP, "username": "example_group"}),
    })

    async def paused(_):
        return False

    monkeypatch.setattr(app.dispatch, "model_allowed", paused)
    await handle_message(app, message)
    await drain_dispatch(app.dispatch)
    calls_before_delivery = len(app.bot.of("send", OWNER))
    await process_ready(app)
    alerts = await app.store.cross_chat_items()
    if trigger in ("mention", "text_mention", "reply"):
        [alert] = alerts
        assert alert["kind"] == "alert" and alert["status"] == "sent"
        [last] = app.bot.of("send", OWNER)[calls_before_delivery:]
        base = "https://" + "t.me/"
        assert last[2] == f'👋 Fran is asking for you in "The Group" · {base}{message.chat.username}/9'
        assert last[3]["disable_notification"] is True
        assert not await app.proposals.pending()
        [event] = await app.store.get_recent_chat_window(OWNER, 5)
        assert event.role == "event" and event.text == last[2]
    else:
        assert not alerts
        assert len(app.bot.of("send", OWNER)) == calls_before_delivery


async def test_alert_limits_by_group_asker_and_shared_hour(make_app):
    app = make_app()
    clock = app.dispatch.now
    for chat in range(GROUP - 6, GROUP + 1):
        await approve_chat(app, chat)

    async def alert(chat, sender, msg):
        from dom.telegram.convert import to_incoming

        request = to_incoming(tg(chat_id=chat, sender=sender, msg_id=msg)).model_copy(update={"mentions_owner": True})
        await app.dispatch.cross_chat.alert(request)
        return len(await app.store.cross_chat_items())

    assert await alert(GROUP, OWNER, 9) == 0  # the owner naming himself is no call for his attention
    assert await alert(GROUP, FRIEND, 1) == 1
    assert await alert(GROUP, FRIEND + 1, 2) == 1  # group cooldown
    clock.advance(minutes=15)
    assert await alert(GROUP, FRIEND, 3) == 1  # same asker for one hour
    assert await alert(GROUP, FRIEND + 1, 4) == 2
    for n in range(1, 5):
        assert await alert(GROUP - n, FRIEND, n) == 2 + n
    assert await alert(GROUP - 5, FRIEND, 1) == 6
    clock.advance(minutes=45)
    assert await alert(GROUP, FRIEND, 1) == 6  # duplicate update even past its cooldown
    assert await alert(GROUP, FRIEND, 5) == 7


@pytest.mark.parametrize("scope, chat, last", [
    ("group", GROUP, "tell_owner"), ("friend_dm", FRIEND, "tell_owner"), ("owner_dm", OWNER, "message_chat"),
])
async def test_delivery_tools_are_scope_specific_and_last(make_app, scope, chat, last):
    app = make_app()
    names = [tool.name for tool in await enabled_tools(app, scope)]
    assert names[-1] == last
    other = "tell_owner" if last == "message_chat" else "message_chat"
    assert other not in names
    assert await call_tool(app, scope, chat, other) == "tool not available here"


async def test_owner_note_is_an_ordinary_silent_bubble_with_no_card_and_shared_limits(make_app):
    app = make_app()
    clock = app.dispatch.now
    clock.advance(hours=12, minutes=1)
    for chat in range(GROUP - 4, GROUP + 1):
        await approve_chat(app, chat)
    await approve_chat(app, FRIEND)

    async def note(chat):
        return await call_tool(
            app, "group" if chat < 0 else "friend_dm", chat, "tell_owner",
            text="Could use your help with this.", reason="The group asked a question",
        )

    assert await note(FRIEND) == "Sent to the owner."
    assert (await note(FRIEND)).startswith("error:")
    before = len(await app.store.cross_chat_items())
    clock.advance(minutes=10)
    assert await note(FRIEND) == "Sent to the owner."
    assert len(await app.store.cross_chat_items()) == before + 1
    await app.store.add_owner_delivery("alert", GROUP, "Ping", "", clock(), sender=FRIEND, msg_id=1)
    for n in range(1, 4):
        assert await note(GROUP - n) == "Sent to the owner."
    assert (await note(GROUP - 4)).startswith("error:")
    assert len(await app.store.cross_chat_items()) == 6
    await process_ready(app)
    assert not await app.proposals.pending()
    replies = app.bot.of("send", OWNER)
    assert len(replies) == 6 and all(reply[3]["disable_notification"] for reply in replies)
    messages = await app.store.get_recent_chat_window(OWNER, 20)
    assert sum(m.role == "assistant" and m.text == "Could use your help with this." for m in messages) == 5
    assert not any(reply[3].get("reply_parameters") for reply in replies)


async def test_card_has_exact_text_telegram_approval_sends_once_and_the_api_rejects(make_app, browser):
    app = make_app()
    await approve_chat(app, GROUP, "Book Club")
    await approve_chat(app, FRIEND, "Reader")
    text = "I'll be there\n  once we're ready. <hello>"
    assert await draft(app, chat="book club", text=text) == "Waiting for the owner's OK on a card; it goes out once approved."
    [card] = await app.proposals.pending()
    assert not app.bot.of("send")
    await process_ready(app)
    [notice] = app.bot.of("send", OWNER)
    assert text in notice[2] and "Book Club" in notice[2] and "Expires in 2 h" in notice[2]
    assert "Leak check unavailable" in notice[2]
    assert not app.bot.of("send", GROUP)
    query = callback(app, OWNER, f"prop:yes:{card['id']}")
    await handle_callback(app, query)
    await handle_callback(app, query)
    await process_ready(app)
    await process_ready(app)
    [sent] = app.bot.of("send", GROUP)
    assert sent[2] == text and sent[3]["reply_parameters"] is None
    [message] = await app.store.get_recent_chat_window(GROUP, 20)
    assert message.role == "assistant" and message.text == text
    assert (await app.store.cross_chat_items())[0]["status"] == "sent"

    assert (await draft(app, chat=FRIEND, text="coordinating the plan")).startswith("Waiting")
    [pending] = await app.proposals.pending()
    async with browser(app) as client:
        waiting = (await client.get("/api/waiting")).json()
        card, = waiting["cards"]
        assert card["kind"] == "cross_chat" and "coordinating the plan" in card["text"] and "Confirm the plan" in card["reason"]
        denied = await client.post("/api/waiting/decide", json={"id": pending["id"], "decision": "reject"}, headers=POST_HEADERS)
        assert denied.status_code == 200 and denied.json()["ok"] is True
        log = (await client.get("/api/system/turns")).json()
        assert any("coordinating the plan" in item["text"] for item in log["cross_chat"])
    await process_ready(app)
    assert not app.bot.of("send", FRIEND)
    assert (await app.store.cross_chat_items())[0]["drop_reason"] == "refused"


async def test_approve_all_skips_cross_chat_and_card_expires(make_app):
    app = make_app()
    await approve_chat(app, GROUP, "Book Club")
    await draft(app)
    [card] = await app.proposals.pending()
    assert "own approval" in await owner.approve_proposal_page(app, card["id"], card["id"])
    await owner.show_proposals(app)
    [listing] = app.bot.of("send", OWNER)
    controls = listing[3]["reply_markup"].inline_keyboard
    assert not any(b.callback_data == f"prop:yes:{card['id']}" for row in controls for b in row)
    await process_ready(app)
    app.dispatch.now.advance(hours=2)
    await process_ready(app)
    assert (await app.store.get_proposal(card["id"]))["status"] == "expired"
    assert (await app.store.cross_chat_items())[0]["drop_reason"] == "expired"
    assert not app.bot.of("send", GROUP)


async def test_pending_and_daily_limits_and_approved_chat_resolution(make_app):
    app = make_app()
    await approve_chat(app, GROUP, "Same")
    await approve_chat(app, FRIEND, "Same")
    await app.store.set_chat_auth_status(GROUP - 1, "ignored", title="Hidden")
    assert "Same" in (await draft(app, chat="same")) and not await app.proposals.pending()
    assert "Hidden" not in await draft(app, chat="Hidden")
    assert (await draft(app, chat=OWNER)).startswith("error:")
    for _ in range(3):
        assert (await draft(app)).startswith("Waiting")
    assert "already three" in await draft(app)
    for card in await app.proposals.pending():
        await app.proposals.decide(card["id"], False)
    for _ in range(10):
        assert (await draft(app)).startswith("Waiting")
        [card] = await app.proposals.pending()
        await app.proposals.decide(card["id"], True)
        await process_ready(app)
    assert len(app.bot.of("send", GROUP)) == 10
    assert "daily limit" in await draft(app)


async def test_verbatim_private_text_and_credentials_refused_before_creating_card(make_app, monkeypatch):
    app = make_app()
    await approve_chat(app, GROUP)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-credential-value")
    app.home.scoped(allow_private=True).memory_write("private/NOTES.md", "add", "Keep the yellow notebook in the attic.")
    for text in ("Keep the yellow notebook in the attic.", "here is test-credential-value"):
        assert (await draft(app, text=text)).startswith("error:")
    assert not await app.proposals.pending()
    assert not await app.store.cross_chat_items()


async def test_worker_waits_for_target_and_source_turn_takes_no_other_slot(make_app, monkeypatch):
    app = make_app()
    dispatch = app.dispatch
    await approve_chat(app, GROUP)
    slots = {}
    real = dispatch._turn_slot
    waiting = asyncio.Event()
    caller = asyncio.current_task()

    @asynccontextmanager
    async def tracked(*chats):
        task = asyncio.current_task()
        assert task not in slots and len(chats) == 1
        if task != caller:
            assert chats == (GROUP,)
            waiting.set()
        async with real(*chats):
            slots[task] = chats[0]
            try:
                yield
            finally:
                del slots[task]

    monkeypatch.setattr(dispatch, "_turn_slot", tracked)
    async with dispatch.chat_slot(OWNER):
        assert (await draft(app)).startswith("Waiting")
        assert set(slots.values()) == {OWNER}
    [card] = await app.proposals.pending()
    await app.proposals.decide(card["id"], True)

    async with dispatch.chat_slot(GROUP):
        task = asyncio.create_task(dispatch.cross_chat.process_one())
        await asyncio.wait_for(waiting.wait(), 2)
        assert not task.done() and not app.bot.of("send", GROUP)
    await asyncio.wait_for(task, 2)
    assert len(app.bot.of("send", GROUP)) == 1
    assert not slots


async def test_card_guard_uses_owner_attribution_and_the_spend_gate(make_app, monkeypatch):
    import httpx

    from dom import turn_log
    from dom.jev import JevClient
    from dom.usage import UsageRecorder

    app = make_app()
    await approve_chat(app, GROUP)
    requests, looked_up = [], []
    names = app.store.chat_participant_names

    async def record_lookup(chat_id):
        looked_up.append(chat_id)
        return await names(chat_id)

    monkeypatch.setattr(app.store, "chat_participant_names", record_lookup)

    def jev_response(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "answers": {"leak": {"type": "score", "score": 2.5}}, "usage": {"input_tokens": 7},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev_response)) as client:
        app.dispatch.jev = JevClient(client, "fake", "guard", usage=UsageRecorder(app.store, app.dispatch.now))
        trace = turn_log.TurnTrace()
        with turn_log.track(trace):
            assert (await draft(app)).startswith("Waiting")
        [row] = await app.proposals.pending()
        assert json.loads(row["body_json"])["leak_score"] == 2.5
        assert looked_up == [GROUP]  # the names of the chat the message goes to, not the owner's DM
        assert requests[0]["provider"] == {"zdr": True}
        assert [call.kind for call in trace.model_calls] == ["guard"]
        usage = await app.store._run(lambda: list(app.store._conn.execute("SELECT kind, chat_id FROM usage")))
        assert [tuple(row) for row in usage] == [("guard", OWNER)]

        async def paused(chat):
            assert chat == OWNER
            return False

        monkeypatch.setattr(app.dispatch, "model_allowed", paused)
        assert (await draft(app)).startswith("Waiting")
        second = (await app.proposals.pending())[-1]
        assert json.loads(second["body_json"])["leak_score"] is None
        assert len(requests) == 1


def test_plain_forum_topic_messages_are_not_replies_to_the_owner():
    from aiogram.types import Chat, ForumTopicCreated, Message

    from dom.telegram.convert import mentions_owner
    from tghelpers import NOW

    forum = Chat(id=GROUP, type="supergroup", title="The Group", is_forum=True)
    owner_user = User(id=OWNER, is_bot=False, first_name="Owner")
    created = Message(
        message_id=50, date=NOW, chat=forum, from_user=owner_user,
        forum_topic_created=ForumTopicCreated(name="Trips", icon_color=0x6FB9F0),
    )
    plain = tg(chat_type="supergroup", reply_to=created, is_topic_message=True, message_thread_id=50)
    assert not mentions_owner(plain, OWNER, "owner")

    owners_post = Message(message_id=77, date=NOW, chat=forum, from_user=owner_user, text="who's in?",
                          is_topic_message=True, message_thread_id=50)
    reply = tg(msg_id=78, chat_type="supergroup", reply_to=owners_post, is_topic_message=True, message_thread_id=50)
    assert mentions_owner(reply, OWNER, "owner")


@pytest.mark.parametrize("value", [["a"], 5, {"x": 1}])
def test_a_wrongly_typed_elsewhere_line_is_dropped_not_fatal(value):
    from dom.agent import parse_actions

    actions = parse_actions(json.dumps({"bubbles": [{"text": "hi", "reply_to": None}], "elsewhere": value}))
    assert actions is not None and actions.elsewhere is None and actions.bubbles[0].text == "hi"


async def test_friend_dm_card_says_no_leak_check_runs_there(make_app):
    app = make_app()
    await approve_chat(app, FRIEND, "Reader")
    await draft(app, chat="reader", text="running late")
    await process_ready(app)
    [notice] = app.bot.of("send", OWNER)
    assert "No leak check for a private chat" in notice[2] and "Leak check unavailable" not in notice[2]


async def test_tell_owner_allows_private_dm_even_when_owner_is_talking_in_group(make_app):
    app = make_app()
    await approve_chat(app, GROUP)
    offered = {tool.name: tool for tool in await enabled_tools(app, "group")}
    ctx = TurnCtx("group", GROUP, OWNER, app.home, app.store, OWNER, app=app)
    result = await execute_tool_call(
        {"function": {"name": "tell_owner", "arguments": json.dumps({"text": "private aside for you", "reason": "x"})}},
        offered, ctx,
    )
    assert result == "Sent to the owner."
    assert len(await app.store.cross_chat_items()) == 1
