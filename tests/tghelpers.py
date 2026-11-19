"""Shared helpers for the Telegram-facing tests: a fake bot, a scripted model, aiogram message builders."""

import json
from datetime import datetime, timezone
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
        self._next_id = 5000

    async def send_chat_action(self, chat_id, action, **kw):
        self.calls.append(("action", chat_id, action, kw))

    async def send_message(self, chat_id, text, **kw):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, kw))
        return SimpleNamespace(message_id=self._next_id)

    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kw):
        self.calls.append(("react", chat_id, message_id, reaction))

    async def delete_message(self, chat_id, message_id, **kw):
        self.calls.append(("delete", chat_id, message_id))

    async def answer_callback_query(self, callback_query_id, text=None, **kw):
        self.calls.append(("answer", callback_query_id, text))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit", chat_id, message_id, text))

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
