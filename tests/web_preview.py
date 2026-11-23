"""Serve the dashboard with a fictional Dom, for the new app's Playwright screenshots (web/screens).

    uv run python tests/web_preview.py --port 8765

It runs the real create_app, so the real auth middleware and CSP apply. Every name and message here is made up;
it never reads a real Dom's home. The bearer token below is a fixed local test value.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))

from dom import traits  # noqa: E402
from dom.config import load_config  # noqa: E402
from dom.dispatch import Dispatcher  # noqa: E402
from dom.home import Home  # noqa: E402
from dom.llm import ChatClient  # noqa: E402
from dom.models import StoredMessage  # noqa: E402
from dom.presence import Presence, read_habits  # noqa: E402
from dom.store import Store  # noqa: E402
from dom.telegram import handlers, owner  # noqa: E402
from dom.web.app import create_app  # noqa: E402
from dom.web.auth import Auth  # noqa: E402
from tghelpers import BOT_USER, OWNER, Clock, FakeBot, FixedRandom, ModelScript, no_sleep  # noqa: E402

TOKEN = "preview-token-" + "p" * 32
TOKEN_ENV = "DOM_PREVIEW_TOKEN"
NOW = datetime(2026, 11, 22, 20, 30, tzinfo=timezone.utc)  # a Sunday evening
CREW, ALEX, SAM = -4001, 4002, 4003


# The Settings page reads and writes this file. Fictional hosts; the one key below is a made-up value.
CONFIG = """\
timezone = "UTC"
leak_check = false
memory_self_edit = true

[models.chat]
base_url = "http://homelab:8000/v1"
model = "example/chat-model"

[[models.chat.fallbacks]]
base_url = "https://llm.example.org/v1"
model = "example/chat-large"
api_key_env = "EXAMPLE_LLM_KEY"

[models.fallback]
base_url = "https://llm.example.org/v1"
model = "example/chat-large"
api_key_env = "EXAMPLE_LLM_KEY"

[models.media]
base_url = "https://llm.example.org/v1"
model = "example/vision-model"
api_key_env = "EXAMPLE_LLM_KEY"

[models.drafting]
base_url = "https://drafts.example.net/v1"
model = "example/draft-model"
api_key_env = "EXAMPLE_DRAFTS_KEY"
reasoning_effort = "low"

[[models.drafting.fallbacks]]
base_url = "https://llm.example.org/v1"
model = "example/chat-large"
api_key_env = "EXAMPLE_LLM_KEY"

[models.search]
provider = "searxng"
base_url = "http://searxng:8080"

[[models.search.fallbacks]]
provider = "tavily"
base_url = "https://api.tavily.com"
api_key_env = "TAVILY_API_KEY"

[models.triage]
base_url = "http://homelab:8000/v1"
model = "example/classifier"

[openers]
enabled = true
"""


def build(home: Path, port: int) -> handlers.App:
    (home / "config.toml").write_text(CONFIG)
    (home / ".env").write_text("EXAMPLE_LLM_KEY=preview-made-up-value\nTAVILY_API_KEY=preview-made-up-value\n")
    config = load_config(home).model_copy(update={"owner_id": OWNER})
    endpoint = config.models["chat"]
    config = config.model_copy(update={
        "web": config.web.model_copy(update={"dev_token_env": TOKEN_ENV, "bind": "127.0.0.1", "port": port}),
    })
    client = ChatClient(endpoint, None, httpx.AsyncClient(transport=httpx.MockTransport(ModelScript())), sleep=no_sleep)
    app = handlers.App(
        config=config, home=Home(home), store=Store(home, owner_id=OWNER), client=client,
        bot=FakeBot(), bot_username="dombot", sleep=no_sleep, bot_id=BOT_USER.id,
    )
    clock = Clock(NOW)
    app.dispatch = Dispatcher(app, Presence(app.store, app.dispatch.presence.tz, FixedRandom(), clock), None, no_sleep, clock)
    return app


async def message(app, chat_id, role, text, msg_id, at, sender=None):
    await app.store.record_new_message(StoredMessage(
        chat_id=str(chat_id), role=role, text=text, msg_id=msg_id, is_bot=role == "assistant",
        sender_user_id=sender, created_at=at.isoformat(), visibility_scope=app.store.visibility_for(chat_id),
    ))


async def seed(app: handlers.App) -> None:
    store, now = app.store, NOW
    app.home.write_habits_line(datetime(2026, 1, 1, 23, 30).time(), datetime(2026, 1, 1, 7, 30).time())
    app.home.write_preset("classic", (
        "---\ntitle: Classic\ndescription: Keeping it easy today. A little wit, a little warmth, and no unnecessary drama."
        "\ntraits:\n  humor: 0.6\n---\n"
    ))
    app.home.write_preset("smooth", "---\ntitle: Smooth\ndescription: Calm and easy.\ntraits:\n  humor: 0.4\n---\n")
    await traits.record_pick(store, traits.load_preset(app.home, "classic"), "A slow Sunday.", "reflection",
                             since=now.replace(hour=8, minute=4), now=now)
    await owner.set_global_preset(app, traits.AUTO, "The owner")
    for chat_id, title in ((OWNER, "Owner"), (CREW, "Weekend crew"), (ALEX, "Alex")):
        await store.set_chat_auth_status(chat_id, "approved", title=title)
    await store.note_pending_chat(str(-4009), "Book swap", "Riley")

    msg_id = 1
    for day in range(1, 14):  # a fortnight of evenings for the hourly bars
        for hour, sender in ((9, ALEX), (12, SAM), (18, ALEX), (19, SAM), (20, ALEX), (21, SAM), (21, ALEX), (22, SAM)):
            if (day + hour) % 3 or hour >= 19:
                at = (now - timedelta(days=day)).replace(hour=hour, minute=10)
                await message(app, CREW, "user", "Fictional chatter", msg_id, at, sender)
                msg_id += 1
    replies = (
        (OWNER, "I've got it. I'll remind you tomorrow morning.", 9),
        (CREW, "That plan sounds suspiciously organised for us.", 32),
        (ALEX, "No worries, take your time.", 254),
    )
    for chat_id, text, minutes in replies:
        await message(app, chat_id, "assistant", text, msg_id, now - timedelta(minutes=minutes))
        msg_id += 1

    app.home._write_regular("SOUL.md", "Dom likes quiet evenings.")
    app.home._write_regular("memory/MEMORY.md", "# Travel notes\n\n- Check weekday times.\n- Use Central as the default station.\n")
    app.home._write_regular("memory/people/river.md", "River prefers the early train.")
    app.home._write_regular(f"memory/chats/{CREW}.md", "The crew plans a trip on Saturdays.")
    app.home.scoped(allow_private=True)._write_regular("private/USER.md", "- Likes long walks\n")
    app.home._write_regular("stickers.toml", 'sets = ["example_set_by_examplebot"]\n')
    await app.proposals.create(
        "memory_edit", "", {"file": "memory/MEMORY.md", "op": "add", "text": "- Check weekend times too.", "reason": "The next trip is on Saturday."},
        "chat", notify=False,
    )
    await app.proposals.create(
        "soul_edit", "Mention that Dom enjoys board games", {"old": "Dom likes quiet evenings.", "new": "Dom likes quiet evenings and board games."},
        "reflection",
    )
    await app.memory_import.run("chatgpt", "- Prefers tea to coffee\n- Walks on Saturday mornings")

    for index in range(148):
        outcome = "Replied in 1 bubble" if index % 12 < 5 else "Stayed quiet"
        await store.record_turn(now - timedelta(minutes=4 * index), CREW, "group", 1, [], "example/chat-model", False, 900, outcome)
    await store.record_usage(now.isoformat(), "chat", "example/chat-model", "llm.example.org", CREW, 900, 300, 0.42, 0)
    await app.dispatch.reminders.create("2026-11-22T21:45:00", "Water the plants")
    await app.dispatch.presence.wake(OWNER, read_habits(app.home), minutes=180)


async def main(port: int) -> None:
    os.environ[TOKEN_ENV] = TOKEN
    with tempfile.TemporaryDirectory() as tmp:
        app = build(Path(tmp), port)
        await seed(app)
        server = uvicorn.Server(uvicorn.Config(
            create_app(app, Auth(app.config)), host="127.0.0.1", port=port, log_level="warning", access_log=False,
        ))
        try:
            await server.serve()
        finally:
            await app.dispatch.close()
            app.store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    asyncio.run(main(parser.parse_args().port))
