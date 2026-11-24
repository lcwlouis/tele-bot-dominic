"""Serve the dashboard with a fictional Dom, for the new app's Playwright screenshots (web/screens).

    uv run python tests/web_preview.py --port 8765

It runs the real create_app, so the real auth middleware and CSP apply. Every name and message here is made up;
it never reads a real Dom's home. The bearer token below is a fixed local test value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import struct
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))

from dom import traits  # noqa: E402
from dom.config import load_config  # noqa: E402
from dom.briefs import write_brief  # noqa: E402
from dom.desktop.fleet import DesktopFleet  # noqa: E402
from dom.dispatch import Dispatcher  # noqa: E402
from dom.home import Home  # noqa: E402
from dom.llm import ChatClient  # noqa: E402
from dom.models import StoredMessage  # noqa: E402
from dom.plugins.tools import DRAFT_PLUGIN_TOOL  # noqa: E402
from dom.presence import Presence, read_habits  # noqa: E402
from dom.selfwork import task_files  # noqa: E402
from dom import skills  # noqa: E402
from dom.skills import Skill, write_skill  # noqa: E402
from dom.store import Store  # noqa: E402
from dom.telegram import handlers, owner  # noqa: E402
from dom.tools import TurnCtx  # noqa: E402
from dom.web import api as web_api  # noqa: E402
from dom.web.app import create_app  # noqa: E402
from dom.web.auth import Auth  # noqa: E402
from dom.web.shared import SOUL_INSTRUCTIONS  # noqa: E402
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
base_url = "https://decisions.example.org/alpha"
model = "example/classifier"

[[studio.voice_fallbacks]]
provider = "openrouter_tts"
model = "example/voice-model"
api_key_env = "EXAMPLE_VOICE_KEY"

[[studio.voice_fallbacks]]
provider = "gemini_tts"
model = "example-speech"
api_key_env = "EXAMPLE_SPEECH_KEY"

[[studio.image_fallbacks]]
provider = "openrouter_image"
model = "example/image-model"
api_key_env = "EXAMPLE_VOICE_KEY"

[[studio.video_fallbacks]]
provider = "openrouter_video"
model = "heygen/heygen-avatar-6-turbo"
api_key_env = "EXAMPLE_VOICE_KEY"
data_policy = "no_training"

[providers."Spare box"]
kind = "openai"
base_url = "http://sparebox:8000/v1"

[openers]
enabled = true
"""


class Computer:
    """A desktop that is always ready, so the Desk shows a live computer. It never runs anything."""

    error = None
    client = None

    def status(self) -> str:
        return "ready"

    def touch(self) -> None:
        pass


def screenshot() -> bytes:
    """A small plain PNG standing in for a screenshot Dom took."""
    width, height = 320, 200
    rows = b"".join(b"\x00" + b"".join(bytes((18 + y // 12, 52 + x // 8 % 60, 66 + y // 6)) for x in range(width)) for y in range(height))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


# A fictional provider model list (OpenRouter's shape): which models reason, and one that names its own levels.
PREVIEW_MODELS = {"data": [
    {"id": "example/chat-model", "name": "Example: Chat", "supported_parameters": ["tools", "reasoning"]},
    {"id": "example/chat-large", "name": "Example: Chat Large", "supported_parameters": ["tools", "reasoning"],
     "reasoning": {"supported_efforts": ["xhigh", "high", "medium", "low"], "default_effort": "medium"}},
    {"id": "example/coder-pro", "name": "Example: Coder Pro", "supported_parameters": ["tools", "reasoning"]},
    {"id": "example/draft-model", "name": "Example: Draft", "supported_parameters": ["tools", "reasoning"]},
    {"id": "example/tiny", "name": "Example: Tiny", "supported_parameters": ["tools"]},
    {"id": "example/vision-model", "name": "Example: Vision", "supported_parameters": ["tools"]},
]}
# OpenRouter lists each media kind's models on its own (/images/models, /audio/speech/models, /videos/models).
PREVIEW_MEDIA_MODELS = {"data": [
    {"id": "example/image-model", "name": "Example: Image"},
    {"id": "example/voice-model", "name": "Example: Voice"},
]}

PREVIEW_AVATAR_MODELS = {"data": [
    {"id": "heygen/heygen-avatar-6", "name": "Avatar 6", "supported_resolutions": ["1080p"],
     "pricing_skus": {"duration_seconds_1080p": "0.10"},
     "architecture": {"input_modalities": ["image", "audio"], "output_modalities": ["video"]}},
    {"id": "heygen/heygen-avatar-6-turbo", "name": "Avatar 6 Turbo", "supported_resolutions": ["1080p"],
     "pricing_skus": {"duration_seconds_1080p": "0.05"},
     "architecture": {"input_modalities": ["image", "audio"], "output_modalities": ["video"]}},
]}


SOUL = """# Dom

Dom likes quiet evenings.

## How he talks

- Short replies, **warm** and a little dry.
- One question at a time, never a list of them.
- Plans in `plain words`, with a [map link](https://example.org/map) when it helps.

> Friends first, plans second.
"""
SOUL_REPLY = json.dumps({
    "reply": "I'd keep the quiet evenings, but I do light up over a **board game**. Want me to say so?",
    "edit": {"old": "Dom likes quiet evenings.", "new": "Dom likes quiet evenings and the odd board game."},
})


def preview_models(script: ModelScript):
    """The fake providers: a model list for GET /models, a yes for a decisions request, else the chat script."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and (
            request.url.path.endswith("/videos/models") or request.url.params.get("output_modalities") == "video"
        ):
            return httpx.Response(200, json=PREVIEW_AVATAR_MODELS)
        if request.method == "GET" and request.url.path.endswith(("/images/models", "/speech/models")):
            return httpx.Response(200, json=PREVIEW_MEDIA_MODELS)
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json=PREVIEW_MODELS)
        if request.method == "GET" and request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"label": "preview"}})
        if request.url.path.endswith("/decisions"):
            return httpx.Response(200, json={"answers": {"check": {"type": "choice", "choice": "yes"}}})
        if request.method == "POST" and SOUL_INSTRUCTIONS[:40] in request.content.decode(errors="replace"):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": SOUL_REPLY}}]})
        return script(request)
    return handle


def build(home: Path, port: int) -> handlers.App:
    (home / "config.toml").write_text(CONFIG)
    (home / ".env").write_text("EXAMPLE_LLM_KEY=preview-made-up-value\nTAVILY_API_KEY=preview-made-up-value\n")
    config = load_config(home).model_copy(update={"owner_id": OWNER, "desktop_mode": "local"})
    endpoint = config.models["chat"]
    config = config.model_copy(update={
        "web": config.web.model_copy(update={"dev_token_env": TOKEN_ENV, "bind": "127.0.0.1", "port": port}),
    })
    client = ChatClient(endpoint, None, httpx.AsyncClient(transport=httpx.MockTransport(preview_models(ModelScript()))), sleep=no_sleep)
    app = handlers.App(
        config=config, home=Home(home), store=Store(home, owner_id=OWNER), client=client,
        bot=FakeBot(), bot_username="dombot", sleep=no_sleep, bot_id=BOT_USER.id,
    )
    app.desktop_worker = DesktopFleet(app, sandbox_factory=lambda host, name: Computer())
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

    app.home._write_regular("SOUL.md", SOUL)
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
    await app.dispatch.reminders.create("2026-11-23T08:00:00", "Check train times for Saturday")
    await app.dispatch.reminders.create("2026-11-24T18:30:00", "Pack a charger")
    await app.dispatch.reminders.create(
        "2026-11-25T07:15:00", "Check https://trains.example.org/timetables/central-to-harbor/saturday-early-departures-and-platform-changes",
    )

    # Work: a desktop task waiting for a sign-in, one finished, Dom's own tasks, skills and a brief.
    waiting = await store.enqueue_desktop_task(OWNER, "Find a train from Central to Harbor after 6 pm.")
    await store.update_desktop_task(waiting, OWNER, ("queued",), "waiting_owner", error="Sign in to continue.")
    finished = await store.enqueue_desktop_task(OWNER, "Look up opening hours for the library.", "shop")
    await store.update_desktop_task(finished, OWNER, ("queued",), "done", result=(
        "The library opens at 9 am on weekdays. Source: https://library.example.org/opening-hours/central-branch/"
        "weekdays-and-weekends-including-public-holidays-and-the-summer-timetable?ref=dom-desktop-task-lookup\n"
        "    Mon–Fri  09:00–18:00\n    Sat      10:00–16:00\n    Sun      closed\n"
        "As data:\n```json\n{\"weekdays\": \"09:00-18:00\", \"saturday\": \"10:00-16:00\", \"sunday\": null}\n```"
    ))
    planned, ran = await store.add_self_tasks([
        {"goal": "Read about slow-travel itineraries", "why": "The owner plans a trip.", "tools": ["browse"], "sites": ["travel.example.org"], "minutes": 10, "steps": 20},
        {"goal": "Check the weather page for Saturday", "why": "To suggest a coat.", "tools": ["browse"], "sites": [], "minutes": 5, "steps": 10},
    ], now - timedelta(hours=30), now - timedelta(hours=6))
    await store.update_self_task(ran["id"], ("planned",), "done", result="Saturday looks clear, 14 degrees.", files=["weather.png"])
    ended = (now - timedelta(hours=3)).isoformat()  # the store stamps real time; the screenshots need a fixed day
    store._conn.execute("UPDATE desktop_tasks SET finished_at = ? WHERE id = ?", (ended, finished))
    # Budgets: the waiting task started with 30 steps; the finished one was extended once with Keep going.
    store._conn.execute("UPDATE desktop_tasks SET steps = 12, steps_budget = 30 WHERE id = ?", (waiting,))
    store._conn.execute("UPDATE desktop_tasks SET steps = 41, steps_budget = 30, extra_steps = 30 WHERE id = ?", (finished,))
    store._conn.execute("UPDATE self_tasks SET finished_at = ? WHERE id = ?", (ended, ran["id"]))
    shots = task_files(app.home.root, ran["id"])
    shots.mkdir(parents=True)
    (shots / "weather.png").write_bytes(screenshot())
    write_skill(app.home, Skill("trip-notes", "Keep a short note for each trip the owner plans", "Open the trip note.\nAdd the dates and the stations.\nKeep it under ten lines."), "")
    write_skill(app.home, Skill("weekly-recap", "Summarise the week on Sunday evening", "List the chats that were busy.\nName one thing to follow up."), "")
    skills.retire_skill(app.home, "weekly-recap")
    write_brief(app.home, now.date(), title="Wake groups gently", problem="Dom wakes a group the moment anyone says his name, even at 3 am.",
                proposal="- Wait for a second message\n- Skip the wake between 1 and 6 am\n\n```python\n"
                "if 1 <= now.hour < 6 and not urgent:\n    return None  # wait for the morning\n```", acceptance="1. No wake at 3 am\n2. Still wakes at 9 am")
    await app.dispatch.presence.wake(OWNER, read_habits(app.home), minutes=180)


PLUGIN = """name = "trip_planner"
description = "Plan a day trip from two stations"
version = "1"
network = true

[[tools]]
name = "plan"
description = "Plan a trip"
parameters = {type = "object"}

[[commands]]
name = "trip"
description = "Plan a day trip"

[[panels]]
id = "week"
title = "Trips this week"
"""
CALENDAR = """name = "wall_calendar"
description = "Show what is on the wall calendar"
version = "1"

[[tools]]
name = "events"
description = "List calendar events"
parameters = {type = "object"}

[[panels]]
id = "today"
title = "On the calendar"
"""
TIDES = """name = "tide_times"
description = "Show today's tide times"
version = "1"

[[tools]]
name = "tides"
description = "List today's tides"
parameters = {type = "object"}
"""
PANEL = {
    "title": "Trips this week",
    "paragraphs": ["Two day trips are planned. The Saturday one has the most stops."],
    "items": [["Saturday", "Central to Harbor"], ["Sunday", "Harbor to Hillside"]],
    "table": {"columns": ["Day", "Stops", "Travel time"], "rows": [["Saturday", "4", "1 h 20 min"], ["Sunday", "2", "40 min"]]},
}


async def seed_system(app: handlers.App) -> None:
    """System and Waiting: plugins (one pending, one approved with a panel), more proposals, turns and spend."""
    store, now = app.store, NOW
    runner = app.plugin_runner  # the sandbox never runs in the preview: its answers are fixed
    runner.trial = lambda *_: {"output": "Planned 2 trips.", "self_test": True}
    runner.self_test = lambda: True
    runner.run_request = lambda folder, request, manifest, *, approved_hash: PANEL
    ctx = TurnCtx("owner_dm", OWNER, OWNER, app.home, store, OWNER, app=app)
    for name, description, manifest, source in (
        ("wall_calendar", "Show what is on the wall calendar", CALENDAR, "def events():\n    return ['Dentist on Tuesday']\n"),
        ("trip_planner", "Plan a day trip from two stations", PLUGIN, "def plan(a, b):\n    return f'{a} to {b}'\n"),
    ):
        await DRAFT_PLUGIN_TOOL.handler({"name": name, "description": description, "files": {"main.py": source}, "manifest_toml": manifest}, ctx)
    calendar = next(row for row in await store.list_plugins() if row["name"] == "wall_calendar")
    await app.plugins.mark_reviewed("wall_calendar", calendar["hash"])
    await app.plugins.decide("wall_calendar", calendar["hash"], "approve")
    # A plugin the owner turned down: it shows as Rejected, with Review again.
    await DRAFT_PLUGIN_TOOL.handler({
        "name": "tide_times", "description": "Show today's tide times", "files": {"main.py": "def tides():\n    return []\n"},
        "manifest_toml": TIDES,
    }, ctx)
    tides = next(row for row in await store.list_plugins() if row["name"] == "tide_times")
    await app.plugins.decide("tide_times", tides["hash"], "reject")

    await app.proposals.create(
        "skill", "Skill: packing-list", {"name": "packing-list", "description": "Keep a packing list for each trip", "body": "Open the trip note.\nList what to bring.\nTick items off."},
        "chat", notify=False,
    )
    await app.proposals.create("lan_address", "The printer moved", {"old": "192.0.2.10", "new": "192.0.2.20", "name": "printer", "places": ["config.toml"]}, "chat", notify=False)
    await store.create_proposal(
        "cross_chat", "Tell the crew about Saturday",
        {"text": "Heads up: the train on Saturday leaves at 9 am.", "reason": "The owner asked Dom to share the plan.", "leak_score": 0},
        "chat", now.isoformat(),
    )
    old = await app.proposals.create("soul_edit", "Mention tea", {"old": "Dom likes quiet evenings.", "new": "Dom likes quiet evenings and tea."}, "reflection", notify=False)
    await app.proposals.decide(old, False)

    for day in range(1, 29):  # a month of spend, with the odd fallback
        at = (now - timedelta(days=day)).replace(hour=14).isoformat()
        await store.record_usage(at, "chat", "example/chat-model", "llm.example.org", CREW, 900, 300, 0.2 + day % 5 * 0.07, int(day % 9 == 0))
        if day % 2 == 0:
            await store.record_usage(at, "search", "example/classifier", "homelab", CREW, 300, 20, 0.04 + day % 3 * 0.01, 0)
        if day % 4 == 0:
            await store.record_usage(at, "reflection", "example/chat-large", "llm.example.org", None, 1500, 400, 0.18, 0)
    await store.record_usage(now.isoformat(), "search", "example/classifier", "homelab", CREW, 300, 20, 0.05, 0)
    await store.record_usage(now.isoformat(), "media", "example/vision-model", "llm.example.org", ALEX, 800, 60, 0.11, 0)

    call = {
        "kind": "chat", "model": "example/chat-model", "host": "homelab", "fallback": False, "status": 200, "duration_ms": 1900,
        "input_tokens": 2048, "output_tokens": 96, "cached_tokens": 1536, "system_hash": "9f3a2c", "tools_hash": "4be1d7",
        "user_hash": "c07a11", "user_prefix_hash": "1d5e90",
    }
    await store.record_turn(
        now - timedelta(minutes=2), CREW, "group", 2, ["final", "web_search", "lock_wait@900", "model@1900", "web_search@1100", "typing@1600", "send@120"],
        "example/chat-model", True, 5600, "Replied in 2 bubbles", model_calls=[call], first_bubble_ms=4200, last_bubble_ms=5600,
    )
    await store.record_turn(
        now - timedelta(minutes=3), OWNER, "owner_dm", 1, ["final", "model@1300", "typing@800"], "example/chat-model", False, 2100,
        "Held back: leak check", model_calls=[{**call, "duration_ms": 1300, "cached_tokens": 0}], first_bubble_ms=None, last_bubble_ms=None,
    )
    # A turn that searched twice and called the model twice: repeated steps and stages must still render.
    await store.record_turn(
        now - timedelta(minutes=5), OWNER, "owner_dm", 1, ["web_search", "web_search", "final", "model@700", "web_search@400", "model@900", "send@100"],
        "example/chat-model", False, 2100, "Replied in 1 bubble", model_calls=[call, {**call, "duration_ms": 900}], first_bubble_ms=2000, last_bubble_ms=2100,
    )


async def main(port: int) -> None:
    os.environ[TOKEN_ENV] = TOKEN
    with tempfile.TemporaryDirectory() as tmp:
        app = build(Path(tmp), port)
        await seed(app)
        await seed_system(app)
        web_api.running_version = lambda: "v0.0.0 · preview"  # a fixed version, so the screens never change with each commit
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
