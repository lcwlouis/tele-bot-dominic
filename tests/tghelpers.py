"""Shared helpers for the Telegram-facing tests: a fake bot, a scripted model, aiogram message builders."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from aiogram.types import Chat, Message, User

OWNER, FRIEND, GROUP = 111, 222, -1009
BOT_USER = User(id=999, is_bot=True, first_name="Dom", username="dombot")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
FINAL = json.dumps({"no_response": False, "react": None, "bubbles": [{"text": "hey"}]})


class FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []
        self.edit_markups: list = []
        self._next_id = 5000
        self.leave_error: Exception | None = None
        self.rich_error: Exception | None = None  # raised by send_rich_message
        self.delete_error: Exception | None = None  # raised by delete_message
        self.member_counts: dict = {}  # chat_id -> int, or an Exception to raise

    async def send_chat_action(self, chat_id, action, **kw):
        self.calls.append(("action", chat_id, action, kw))

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, kw))
        return SimpleNamespace(message_id=self._next_id)

    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kw):
        self.calls.append(("react", chat_id, message_id, reaction))

    async def send_rich_message(self, chat_id, rich_message, **kw):
        if self.rich_error is not None:
            raise self.rich_error
        self._next_id += 1
        self.calls.append(("rich", chat_id, rich_message.markdown, kw))
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id, message_id, **kw):
        self.calls.append(("delete", chat_id, message_id))
        if self.delete_error is not None:
            raise self.delete_error

    async def answer_callback_query(self, callback_query_id, text=None, **kw):
        self.calls.append(("answer", callback_query_id, text))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit", chat_id, message_id, text))
        self.edit_markups.append(kw.get("reply_markup"))

    async def leave_chat(self, chat_id, **kw):
        self.calls.append(("leave", chat_id))
        if self.leave_error is not None:
            raise self.leave_error

    async def get_chat_member_count(self, chat_id, **kw):
        self.calls.append(("count", chat_id))
        result = self.member_counts.get(chat_id, 3)
        if isinstance(result, Exception):
            raise result
        return result

    async def set_my_commands(self, commands, scope=None, **kw):
        self.calls.append(("set_commands", commands, scope))

    async def delete_my_commands(self, scope=None, **kw):
        self.calls.append(("delete_commands", scope))

    def of(self, kind, chat_id=None):
        return [c for c in self.calls if c[0] == kind and (chat_id is None or c[1] == chat_id)]


class ModelScript:
    def __init__(self, *contents):
        self.contents = list(contents)
        self.requests: list[dict] = []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        content = self.contents.pop(0) if self.contents else FINAL
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def turn_requests(app):
    """The model requests that were reply turns (they offer tools), not background memory passes."""
    return [request for request in app.script.requests if "tools" in request]


async def no_sleep(seconds):
    return None


def tg(chat_id=GROUP, sender=FRIEND, text="hello", msg_id=1, chat_type=None, reply_to=None, **kw):
    chat_type = chat_type or ("private" if chat_id > 0 else "group")
    return Message(
        message_id=msg_id, date=NOW,
        chat=Chat(id=chat_id, type=chat_type, title=None if chat_id > 0 else "The Group"),
        from_user=User(id=sender, is_bot=False, first_name="Fran", username="fran"),
        text=text, reply_to_message=reply_to, **kw,
    )


class Clock:
    def __init__(self, at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc)):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, **delta):
        self.at += timedelta(**delta)


class FixedRandom:
    def __init__(self):
        self.draws = []

    def triangular(self, lo, hi, mode):
        self.draws.append((lo, hi, mode))
        return (lo + hi) / 2

    def uniform(self, lo, hi):
        return (lo + hi) / 2


async def drain_dispatch(dispatch):
    while dispatch._tasks:
        await asyncio.gather(*list(dispatch._tasks), return_exceptions=True)
