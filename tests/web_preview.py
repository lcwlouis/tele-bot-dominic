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
from dom import tracer, turn_log  # noqa: E402
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
desktop_mode = "local"
monthly_spend_cap_usd = 30.0

[area_spend_caps_usd]
media = 5.0

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
base_url = "https://openrouter.ai/api/alpha"
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


def _fictional_photo() -> bytes:
    """A made-up picture of a place: sky above, water below, a blue chair. No real photo is ever in the preview."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (480, 360), (190, 215, 235))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 200, 480, 360), fill=(70, 120, 160))
    draw.rectangle((150, 150, 210, 230), fill=(30, 70, 180))
    draw.rectangle((270, 150, 330, 230), fill=(30, 70, 180))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


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
# OpenRouter's decision models (GET /api/v1/models?output_modalities=decisions), for the triage picker.
PREVIEW_DECISIONS = {"data": [
    {"id": "example/classifier", "name": "Example: Classifier", "architecture": {"input_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001"}, "context_length": 32000},
    {"id": "example/decider-flash:free", "name": "Example: Decider Flash (free)",
     "architecture": {"input_modalities": ["text"]}, "pricing": {"prompt": "0"}, "context_length": 128000},
    {"id": "example/decider-pro", "name": "Example: Decider Pro", "architecture": {"input_modalities": ["text", "image"]},
     "pricing": {"prompt": "0.000001"}, "context_length": 128000},
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
        if request.method == "GET" and request.url.params.get("output_modalities") == "decisions":
            return httpx.Response(200, json=PREVIEW_DECISIONS)
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



async def preview_provenance(store, kind, at, chat_id):
    """Where a fictional kept item came from: the studio's voice, a cloud photo that was redone, one still unpriced."""
    def step(part, usage_kind, model, host, row, redo=False):
        return {"part": part, "kind": usage_kind, "model": model, "host": host, "usage_id": row, **({"redo": True} if redo else {})}

    if kind == "voice":
        return {"steps": [step("item", "studio_voice", "Higgs TTS", "local", None)], "preset": "Cosy", "seconds": 6.4, "duration": 4.2}
    if kind == "video_note":  # the scene still: a cloud first try, then a local redo that was kept
        still = await store.record_usage(at.isoformat(), "studio_image", "bytedance-seed/seedream-5-5", "openrouter.ai", chat_id, 0, 0, 0.021, 1)
        return {"steps": [
            step("still", "studio_image", "bytedance-seed/seedream-5-5", "openrouter.ai", still),
            step("still", "studio_image", "FLUX.2 klein", "local", None, redo=True),
            step("item", "studio_video", "FlashHead", "local", None),
        ], "kept_redo": ["still"], "outfit": "balcony", "preset": "Cosy", "seconds": 40.0, "duration": 6.5}
    model = "bytedance-seed/seedream-5-5"
    first = await store.record_usage(at.isoformat(), "studio_image", model, "openrouter.ai", chat_id, 0, 0, 0.043 if chat_id == OWNER else 0.0, 1)
    steps = [step("item", "studio_image", model, "openrouter.ai", first)]
    if chat_id != OWNER:  # its cost has not been reported yet
        return {"steps": steps, "outfit": "harbour", "preset": "Wistful", "seconds": 11.0}
    second = await store.record_usage(at.isoformat(), "studio_image", model, "openrouter.ai", chat_id, 0, 0, 0.043, 1)
    steps.append(step("item", "studio_image", model, "openrouter.ai", second, redo=True))
    return {"steps": steps, "kept_redo": ["item"], "outfit": "casual", "preset": "Cosy", "seconds": 18.5}

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
    # Groups the current bot can no longer reach (E74): one listed with its history kept, one the owner hid.
    for chat_id, title in ((-4005, "Old book club"), (-4006, "Hiking 2025")):
        await store.set_chat_auth_status(chat_id, "approved", title=title)
        await store.set_chat_reachable(chat_id, False, "bot was kicked")
        store._conn.execute("UPDATE chats SET unreachable_at = ? WHERE chat_id = ?", ((now - timedelta(days=2)).isoformat(), str(chat_id)))
    await store.hide_chat(-4006, True)
    # Alex, by name: the Memory page labels a person's notes by who they are, not their ID.
    store._conn.execute(
        "INSERT OR IGNORE INTO people_identities (person_key, telegram_user_id, current_handle, known_handles_json, "
        "display_name, preferred_name, profile_path, updated_at) VALUES ('uid_4002', 4002, 'alex_example', '[]', 'Alex', 'Alex', "
        "'memory/people/4002.md', ?)", (now.isoformat(),),
    )

    msg_id = 1
    for day in range(1, 14):  # a fortnight of evenings for the hourly bars
        for hour, sender in ((9, ALEX), (12, SAM), (18, ALEX), (19, SAM), (20, ALEX), (21, SAM), (21, ALEX), (22, SAM)):
            if (day + hour) % 3 or hour >= 19:
                at = (now - timedelta(days=day)).replace(hour=hour, minute=10)
                await message(app, CREW, "user", "Fictional chatter", msg_id, at, sender)
                msg_id += 1
    # Studio media in the owner's DM: a voice note this morning and a photo a few minutes ago, inside the gap. Both
    # are kept on disk, so the Library lists them; a photo in the crew's chat was removed past the cap.
    for chat_id, kind, minutes, scene, keep in (
        (OWNER, "voice", 600, "Good morning, the train is at nine.", True),
        (CREW, "photo", 60 * 26, "Dom at the harbour at dusk.", False),
        (OWNER, "video_note", 60 * 3, "Dom waving from the balcony.", False),
        (OWNER, "photo", 14, "Dom on a park bench with a paper cup of tea.", True),
    ):
        at = now - timedelta(minutes=minutes)
        data = screenshot() if kind == "photo" else b"OggS" + bytes(60)
        media = {"kind": kind, "scene": scene, "file_id": f"preview-{msg_id}"}
        media["made"] = await preview_provenance(app.store, kind, at, chat_id)
        if keep:
            media.update(await app.media_files.keep(chat_id, msg_id, kind, data, at))
        await app.store.record_new_message(StoredMessage(
            chat_id=str(chat_id), role="assistant", text="", msg_id=msg_id, is_bot=True, media_json=json.dumps(media),
            created_at=at.isoformat(), visibility_scope=app.store.visibility_for(chat_id),
        ))
        msg_id += 1
    replies = (
        (OWNER, "I've got it. I'll remind you tomorrow morning.", 9),
        (CREW, "That plan sounds suspiciously organised for us.", 32),
        (ALEX, "No worries, take your time.", 254),
    )
    for chat_id, text, minutes in replies:
        await message(app, chat_id, "assistant", text, msg_id, now - timedelta(minutes=minutes))
        msg_id += 1
    for minutes, text in ((60 * 49, "See you all at the next meeting."), (60 * 50, "Has everyone finished the book?")):
        await message(app, -4005, "user", text, msg_id, now - timedelta(minutes=minutes), ALEX)
        msg_id += 1

    app.home._write_regular("SOUL.md", SOUL)
    app.home._write_regular("memory/MEMORY.md", "# Travel notes\n\n- Check weekday times.\n- Use Central as the default station.\n")
    app.home._write_regular("memory/people/river.md", "River prefers the early train.")
    app.home._write_regular(f"memory/people/{ALEX}.md", (
        "# Alex\n\n- Works as a nurse (as of 2026-11) {chat -4001}\n- Prefers tea to coffee {any}\n"
        "- Plans a trip to the coast (until 2027-04-30) {chat -4001} {entities person:4002|place:the-coast} {core dom 2026-11-20: she talks about it all the time}\n"
        "- Allergic to peanuts {core owner 2026-11-21}\n"
    ))
    app.home._write_regular(f"memory/people/{ALEX}.former.md", "- Works at the bakery (as of 2025-06) {any} [former 2026-03-01: replaced]\n")
    app.home._write_regular("memory/JOURNAL.md", (
        "## 2026-11-21\nMood: easy, a little sleepy\nThe crew planned Saturday's trip and Alex finally booked the train.\n"
        "Nudge: ask Alex how the night shift went.\n\n## 2026-11-22\nMood: bright\nA slow Sunday with long walks in the owner's DM.\n"
    ))
    app.home._write_regular("memory/journal/2026-09.md", (
        "# Journal 2026-09\nWeek 2026-W36: The crew started planning trips again.\nMonth 2026-09: A quiet month of plans.\n\n"
        "## 2026-09-03\nMood: curious\nRiver asked about the early train.\n"
    ))
    app.home._write_regular(f"memory/chats/{CREW}.md", "The crew plans a trip on Saturdays.")
    app.home._write_regular("memory/WORLD.md", (
        "# Dom's world\n\n## Home and work\n- Lives in a small flat above a bakery {entities place:the-bakery}\n"
        "- Draws picture books from a desk by the window\n\n## Routine\n"
        "- Weekday lunch: noodles, a rice bowl or a sandwich at the harbour cafe {entities place:the-harbour-cafe}\n"
        "- Thursday evening: usually the climbing gym\n\n## Regular spots\n- Coffee: the corner kiosk {core owner 2026-11-20}\n"
    ))
    app.home.write_file("memory/WORLD.change.json", json.dumps({"day": "2026-11-18", "reason": "He took up climbing and wanted a weekly slot for it."}))
    app.home._write_regular("memory/places/the-harbour-cafe.md", "- A small cafe on the water with blue chairs {any} {entities place:the-harbour-cafe}\n")
    app.home._write_regular("memory/places/the-bakery.md", "- The bakery downstairs, open from six {any}\n")
    await app.place_photos.save("the-harbour-cafe", _fictional_photo())
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
    # The store stamps real time on enqueue too: pin both tasks to the preview's clock, so the list's order and
    # relative times hold in every screenshot.
    store._conn.execute("UPDATE desktop_tasks SET created_at = ? WHERE id = ?", ((now - timedelta(minutes=20)).isoformat(), waiting))
    store._conn.execute("UPDATE desktop_tasks SET created_at = ? WHERE id = ?", ((now - timedelta(hours=3, minutes=40)).isoformat(), finished))
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

    # Goals and noticings (R6, R7): two goals under way, one paused, and two things Dom noticed overnight.
    walk = await app.goals.create("Walk 8,000 steps most days", category="health", check_in_rhythm="every_few_days")
    await app.goals.create("Book the coast trip with Alex", category="relationships", target_date="2026-12-20", check_in_rhythm="weekly")
    guitar = await app.goals.create("Practise guitar twice a week", category="interests", check_in_rhythm="weekly")
    await app.goals.set_status(guitar["id"], "paused")
    await app.goals.add_progress(walk["id"], "Three good days this week.")
    # Assistant mode (R15): what became of each message's last day, as the Settings card shows it.
    await store.kv_set_many({
        "assistant:morning:last": json.dumps({"day": "2026-11-21", "state": "sent", "at": "2026-11-21T00:06:00+00:00"}),
        "assistant:evening:last": json.dumps({"day": "2026-11-21", "state": "asleep", "at": "2026-11-21T13:40:00+00:00"}),
    })
    await store.create_noticings(now.date().isoformat(), [
        {"kind": "followup", "title": "Ask Alex how the night shift went", "body": "She mentioned a double shift on Friday.",
         "action_kind": "chat", "action_payload_json": "{}", "source_kind": "owner_dm", "source_chat_id": None,
         "source_msg_id": None, "source_label": "your DM"},
        {"kind": "deadline", "title": "Saturday's train needs booking", "body": "Fares go up on Thursday.",
         "action_kind": "reminder", "action_payload_json": "{}", "source_kind": "memory", "source_chat_id": None,
         "source_msg_id": None, "source_label": "Dom's notes"},
    ], now.isoformat())

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

    # Fictional saved logins, site permissions, and approvals history for System -> Access (deterministic fixed key).
    from dom.config import set_dotenv

    set_dotenv(app.home.root, "DOM_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    await app.desktop_permissions.save_login(
        site="library.example.org", label="City library", username="reader", password="preview-only-password",
    )
    await app.desktop_permissions.set_site_rule("library.example.org", "fill_login", "allow")
    await app.desktop_permissions.record_decision(
        action="fill_login", site="library.example.org", detail="City library (re***)",
        status="allowed_always", decided_via="telegram",
    )
    await app.desktop_permissions.record_decision(
        action="download", site="docs.example.org", detail="Files: itinerary.pdf",
        status="rejected", decided_via="dashboard",
    )


TRACED_TURN = 900  # the owner-DM turn whose trace the Talk screens open (/app/talk?trace=1&turn=900)


async def seed_talk(app: handlers.App) -> None:
    """The owner DM on the Talk page, one message typed on the dashboard, and the full trace of Dom's answer."""
    store, now = app.store, NOW
    start = now - timedelta(minutes=6)
    lines = [
        ("user", "Are we still on for board games on Friday?", 70, 0, "dashboard"),
        ("assistant", "Yes! Alex is bringing the new one with the trains.", 71, 9, None),
        ("assistant", "Want me to remind you on Friday afternoon?", 72, 11, None),
    ]
    for role, text, msg_id, seconds, origin in lines:
        await store.record_new_message(StoredMessage(
            chat_id=str(OWNER), role=role, text=text, msg_id=msg_id, is_bot=role == "assistant",
            sender_user_id=None if role == "assistant" else OWNER, created_at=(start + timedelta(seconds=seconds)).isoformat(),
            visibility_scope=store.visibility_for(OWNER), origin=origin,
        ))
    await store.mark_chat_read(OWNER, 72)  # answered already: the preview must not answer it again at start
    calls = [{"kind": "chat", "model": "example/chat-model", "input_tokens": 2048, "output_tokens": 96, "cached_tokens": 1536}]
    row = await store.record_turn(start, OWNER, "owner_dm", 1, ["final"], "example/chat-model", False, 11000, "Replied",
                                  model_calls=calls, first_bubble_ms=9000, last_bubble_ms=11000)
    # What the turn cost, so Your DM's (i) on Dom's bubbles has a price.
    await store.record_usage((start + timedelta(seconds=3)).isoformat(), "chat", "example/chat-model", "homelab", OWNER, 2048, 96, 0.00042, 0)
    store._conn.execute("UPDATE turns SET id = ? WHERE id = ?", (TRACED_TURN, row))

    writer = tracer.TraceWriter(tracer.TraceStore(app.home.root / "traces.db"), lambda: (7, 256_000_000), lambda: ())
    app.traces = writer
    trace = turn_log.TurnTrace(chat_id=OWNER, scope="owner_dm", started=start.timestamp(), outcome="Replied",
                               model="example/chat-model", row_id=TRACED_TURN)
    at = start.timestamp()
    trace.timings = ["debounce@1500", "model@1300", "typing@2400", "send@120"]
    trace.events = [
        {"type": "arrival", "at": at + 0.1, "stage": "triage", "msg_id": 70, "state": "online", "scope": "owner_dm", "unread": 1, "skills": []},
        {"type": "triage", "at": at + 0.6, "mode": "online", "verdict": {"action": "reply", "confidence": 0.94, "done": "yes", "done_confidence": 0.81, "skill": None, "emoji": "👍"}},
        {"type": "arrival", "at": at + 1.6, "stage": "turn", "msg_id": 70, "state": "online", "unread": 1, "catch_up": False},
        {"type": "prompt", "at": at + 1.7, "fingerprint": "system=9f3a2c tools=4be1d7 window=1d5e90", "scope": "owner_dm", "messages": 2, "tools": ["web_search", "remind"]},
    ]
    call = turn_log.ModelCall("chat", "example/chat-model", False, "9f3a2c", "4be1d7", "c07a11", "1d5e90", duration_ms=1300,
                              status=200, input_tokens=2048, output_tokens=96, cached_tokens=1536, host="homelab")
    answer = json.dumps({"bubbles": [{"text": lines[1][1]}, {"text": lines[2][1]}]})
    call.raw = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": answer}}],
                                         "usage": {"prompt_tokens": 2048, "completion_tokens": 96, "cost": 0.00042}})
    request = {"model": "example/chat-model", "messages": [
        {"role": "system", "content": "You are Dom. (The made-up system message of the preview.)"},
        {"role": "user", "content": "<notes>Board games on Fridays.</notes>\n<chat_window>…</chat_window>\n<turn_note>Sunday evening.</turn_note>"},
    ], "tools": [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}},
                 {"type": "function", "function": {"name": "remind", "parameters": {"type": "object"}}}]}
    trace.events += [
        {"type": "model_call", "at": at + 3.0, "call": call, "request": request, "url": "https://llm.example/v1/chat/completions", "error": None},
        {"type": "actions", "at": at + 3.0, "actions": json.loads(answer)},
        {"type": "delivery", "at": at + 9.0, "kind": "bubble", "chat_id": OWNER, "msg_id": 71, "text": lines[1][1]},
        {"type": "delivery", "at": at + 11.0, "kind": "bubble", "chat_id": OWNER, "msg_id": 72, "text": lines[2][1]},
    ]
    await writer.write(trace)


async def main(port: int) -> None:
    os.environ[TOKEN_ENV] = TOKEN
    with tempfile.TemporaryDirectory() as tmp:
        app = build(Path(tmp), port)
        await seed(app)
        await seed_system(app)
        await seed_talk(app)
        web_api.running_version = lambda: "v0.0.0 · preview"  # a fixed version, so the screens never change with each commit
        server = uvicorn.Server(uvicorn.Config(
            create_app(app, Auth(app.config)), host="127.0.0.1", port=port, log_level="warning", access_log=False,
        ))
        try:
            await server.serve()
        finally:
            await app.dispatch.close()
            if app.traces is not None:
                app.traces.store.close()
            app.store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    asyncio.run(main(parser.parse_args().port))
