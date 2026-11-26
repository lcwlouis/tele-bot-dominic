"""Regression tests for the v0.8.4 release-gate round 1 findings."""

from __future__ import annotations

import httpx

from dom.config import ModelEndpoint, ProviderEndpoint
from dom.embedding import EmbeddingProvider, EmbeddingRouter
from dom.llm import ChatClient, FallbackClient
from dom.search import SearchClient, SearchRouter


def _chat(key: str, http: httpx.AsyncClient) -> ChatClient:
    endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env=None, input=("text",))
    return ChatClient(endpoint, key, http)


def _provider(base: str) -> ProviderEndpoint:
    return ProviderEndpoint(base_url=base, model="m", api_key_env=None)


async def test_running_chat_keys_walks_every_router_down_to_its_leaf_keys(make_app):
    app = make_app()
    http = app.client.http
    app.client = FallbackClient(_chat("chat-primary", http), _chat("chat-fallback", http), _chat("chat-third", http))
    app.draft_client = FallbackClient(_chat("draft-key", http), None)
    app.escalation_client = FallbackClient(_chat("escalation-key", http), None)
    app.search_client = SearchRouter(
        SearchClient(_provider("https://s1.test"), "search-one", http),
        SearchClient(_provider("https://s2.test"), "search-two", http),
    )
    app.embedding_client = EmbeddingRouter(
        EmbeddingProvider(_provider("https://e1.test"), "embed-one", http),
        EmbeddingProvider(_provider("https://e2.test"), "embed-two", http),
    )
    app.knowledge.router = EmbeddingRouter(EmbeddingProvider(_provider("https://e3.test"), "embed-serving", http))
    app.knowledge.staged_router = EmbeddingRouter(EmbeddingProvider(_provider("https://e4.test"), "embed-staged", http))

    assert app.running_chat_keys() == {
        "chat-primary", "chat-fallback", "chat-third", "draft-key", "escalation-key",
        "search-one", "search-two", "embed-one", "embed-two", "embed-serving", "embed-staged",
    }


# ---- B2: chat notes and entity notes keep to their audience ---------------------------------------------

import json  # noqa: E402

from dom.knowledge import KnowledgeIndex  # noqa: E402
from dom.tools import TurnCtx, _search  # noqa: E402
from test_knowledge import allowed, embedding_router, tool_ctx, tool_search, vectors  # noqa: E402
from tghelpers import FRIEND, GROUP, OWNER  # noqa: E402

OTHER = -2002


def seed_tag_notes(app):
    app.home._write_regular(
        f"memory/chats/{OTHER}.md",
        "- Atlantis chat line tagged any {any} {entities project:chat-plan|place:lagoon}\n"
        f"- Atlantis chat line tagged elsewhere {{chat {GROUP}}}\n",
    )
    app.home._write_regular(
        f"memory/chats/{GROUP}.md", "- Atlantis line of this group {entities project:group-plan|place:lagoon}\n",
    )
    app.home._write_regular(
        "memory/places/lagoon.md",
        "- Atlantis untagged place line {entities project:owner-plan|place:lagoon}\n"
        "- Atlantis shared place line {any} {entities project:shared-plan|place:lagoon}\n",
    )


async def test_chat_note_lines_tagged_any_and_untagged_entity_lines_never_leave_their_audience(make_app):
    app = make_app()
    seed_tag_notes(app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(vectors([]))) as http:
        app.knowledge = KnowledgeIndex(
            app.store, app.home, embedding_router(app, http), allowed=allowed, chat_id=OWNER,
        )
        await app.knowledge.run_pending()
        group = tool_ctx(app, GROUP, "group", FRIEND, participants=(FRIEND,))
        friend_dm = tool_ctx(app, FRIEND, "friend_dm", FRIEND, participants=(FRIEND,))
        other = tool_ctx(app, OTHER, "group", FRIEND, participants=())
        owner_dm = tool_ctx(app, OWNER, "owner_dm", OWNER)
        for ctx in (group, friend_dm):
            for mode, query in (
                ("keyword", "Atlantis"), ("meaning", "Atlantis"), ("entity", "place:lagoon"),
                ("entity", "project:chat-plan"), ("entity", "project:owner-plan"),
            ):
                texts = " ".join(hit["text"] for hit in await tool_search(app, ctx, query, mode))
                assert "tagged any" not in texts and "tagged elsewhere" not in texts, (ctx.chat_id, mode, query)
                assert "untagged place" not in texts, (ctx.chat_id, mode, query)
            keyword = " ".join(hit["text"] for hit in await tool_search(app, ctx, "Atlantis"))
            assert "shared place line" in keyword
            related = await app.knowledge.related("place:lagoon", requester_chat_id=ctx.chat_id, participants=(FRIEND,))
            assert "project:shared-plan" in related
            assert not {"project:chat-plan", "project:owner-plan", f"chat:{OTHER}"} & set(related)
        mine = " ".join(hit["text"] for hit in await tool_search(app, group, "Atlantis"))
        assert "line of this group" in mine  # the chat's own note stays searchable there
        assert "tagged any" in " ".join(hit["text"] for hit in await tool_search(app, other, "Atlantis"))
        everything = " ".join(hit["text"] for hit in await tool_search(app, owner_dm, "Atlantis"))
        assert "untagged place line" in everything and "tagged any" in everything


# ---- S1: the owner DM's background work obeys the monthly cap -------------------------------------------

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402

from dom.memory_pass import MemoryPass  # noqa: E402
from dom.usage import ModelCallSkipped  # noqa: E402

_CLOCK = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


async def _spend(app, cost):
    await app.dispatch.usage.record("chat", "mock", "example.org", {"cost": cost}, chat_id=OWNER)


async def test_owner_dm_background_media_calls_stop_at_the_monthly_cap_but_not_the_daily_one(make_app):
    app = make_app()
    app.dispatch.now.at = _CLOCK
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 1.0, "monthly_spend_cap_usd": 0.0})
    await _spend(app, 1.5)
    for background in (False, True):  # over the daily cap only: the owner DM and its background work both go on
        async with app.dispatch._media_request(OWNER, background=background):
            pass
    app.config = app.config.model_copy(update={"monthly_spend_cap_usd": 1.0})
    async with app.dispatch._media_request(OWNER):  # the owner's own turn stays exempt
        pass
    with pytest.raises(ModelCallSkipped):
        async with app.dispatch._media_request(OWNER, background=True):
            pass


async def test_the_owner_dm_memory_pass_asks_nothing_once_the_monthly_cap_is_reached(make_app):
    app = make_app(contents=["- a remembered line"])
    app.dispatch.now.at = _CLOCK
    app.config = app.config.model_copy(update={"daily_spend_cap_usd": 0.0, "monthly_spend_cap_usd": 1.0})
    await _spend(app, 1.5)
    assert await MemoryPass(app)._ask(OWNER, [{"role": "user", "content": "x"}]) is None
    assert not app.script.requests


# ---- S4: a group's migration retags the entity notes too ------------------------------------------------

from dom.home import Home, init_home  # noqa: E402


def test_migrating_a_group_retags_its_lines_in_places_projects_and_goals(tmp_path):
    init_home(tmp_path)
    home = Home(tmp_path)
    for kind in ("places", "projects", "goals"):
        home._write_regular(
            f"memory/{kind}/thing.md", f"- line for the group {{chat -1001}}\n- line for another {{chat -5}}\n- shared {{any}}\n",
        )
    assert home.move_chat_notes(-1001, -1009)
    for kind in ("places", "projects", "goals"):
        text = home.read_memory_file(f"memory/{kind}/thing.md")
        assert "{chat -1009}" in text and "{chat -1001}" not in text
        assert "{chat -5}" in text and "{any}" in text


# ---- S5: the cap notices are in the short Telegram style --------------------------------------------------

async def test_a_spend_cap_notice_has_a_bold_lead_and_a_link_to_the_spend_page(make_app):
    from dom.config import WebConfig
    from tghelpers import no_sleep

    app = make_app()
    app.dispatch.now.at = _CLOCK
    app.config = app.config.model_copy(update={
        "daily_spend_cap_usd": 0.0, "monthly_spend_cap_usd": 1.0, "web": WebConfig(public_url="https://example.org"),
    })
    await _spend(app, 1.5)
    assert await app.dispatch.usage.over_cap(app.config, app.bot, sleep=no_sleep)
    [(_, _, text, options)] = app.bot.of("send", OWNER)
    assert text.splitlines()[0] == "Monthly spend cap reached: $1.50 of $1.00" and len(text.splitlines()) == 3
    assert [(e.type, e.offset, e.length) for e in options["entities"]] == [("bold", 0, len(text.splitlines()[0]))]
    [[button]] = options["reply_markup"].inline_keyboard
    assert (button.text, button.url) == ("Details on the dashboard", "https://example.org/app/system/spend")


async def test_the_provider_budget_notice_is_a_short_notice_too(make_app):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from dom.llm import BudgetRefusal, ChatBudgetExhausted

    app = make_app()
    reset = (_CLOCK + timedelta(days=1)).isoformat()
    refusal = BudgetRefusal(
        source="openrouter_workspace_budget", scope="workspace", limit_usd=0.5, spend_usd=0.505, resets_at=reset,
    )
    route = app.dispatch._chat_route_id()
    await app.dispatch._provider_budget_notice(route, ChatBudgetExhausted((refusal,)))
    [(_, _, text, options)] = app.bot.of("send", OWNER)
    lead, *details = text.splitlines()
    assert lead == "OpenRouter's workspace budget ($0.50) is used up." and len(details) == 2
    assert details[0] == "Raise it in OpenRouter → Workspace settings." and details[1].startswith("Resets ")
    assert [e.type for e in options["entities"]] == ["bold"]


# ---- S6: /tasks, /skills and /t3 are menu cards that close and expire -------------------------------------

from datetime import timedelta  # noqa: E402

from dom.skills import Skill, write_skill  # noqa: E402
from dom.t3.tools import Bridge  # noqa: E402
from dom.telegram import cards, owner  # noqa: E402
from test_desktop_fleet import fleet_for  # noqa: E402


def _card_rows(app):
    import sqlite3

    with sqlite3.connect(app.store.db_path) as conn:
        return conn.execute("SELECT chat_id, message_id, expires_at FROM menu_cards").fetchall()


def _closes(markup):
    return [button for row in markup.inline_keyboard for button in row if button.callback_data == cards.CLOSE_DATA]


async def test_the_tasks_card_has_a_close_button_and_expires(make_app):
    app = make_app()
    app.dispatch.now.at = _CLOCK
    await fleet_for(app).enqueue(OWNER, "look at prices")
    await owner.show_tasks(app)
    assert _closes(app.bot.of("send", OWNER)[-1][3]["reply_markup"])
    [(chat_id, _, expires_at)] = _card_rows(app)
    assert chat_id == OWNER and datetime.fromisoformat(expires_at) == _CLOCK + timedelta(minutes=app.config.menu_card_minutes)


async def test_the_skills_card_has_a_close_button_and_expires(make_app):
    app = make_app()
    app.dispatch.now.at = _CLOCK
    write_skill(app.home, Skill("notes", "Keeps notes", "Write the notes down."), "")
    await owner.show_skills(app)
    assert _closes(app.bot.of("send", OWNER)[-1][3]["reply_markup"])
    assert len(_card_rows(app)) == 1


async def test_the_t3_cards_have_a_close_button_and_expire(make_app):
    app = make_app()
    app.dispatch.now.at = _CLOCK
    app.t3 = Bridge(app)
    await app.store.record_t3(OWNER, "coding_send", "laptop", "Fixture thread", "sent", "ok", "")  # so the history card shows
    await app.t3.show()
    sends = app.bot.of("send", OWNER)
    assert sends and all(_closes(call[3]["reply_markup"]) for call in sends)
    assert len(_card_rows(app)) == len(sends)
    assert any(button.callback_data == "t3reconnect" for row in sends[0][3]["reply_markup"].inline_keyboard for button in row)


# ---- W5: one version for the bot and the studio -----------------------------------------------------------

def test_the_studio_package_version_matches_the_bot_and_its_health_endpoint():
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    bot = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    studio = tomllib.loads((root / "studio/pyproject.toml").read_text())["project"]["version"]
    health = re.search(r'^VERSION = "([^"]+)"', (root / "studio/studio_service/http.py").read_text(), re.M).group(1)
    lock = re.search(r'name = "dom-studio"\nversion = "([^"]+)"', (root / "studio/uv.lock").read_text()).group(1)
    assert bot == studio == health == lock


# ---- the hang: draining a dispatch whose finished tasks have not been removed yet --------------------------

async def test_drain_dispatch_lets_the_done_callbacks_of_finished_tasks_run():
    import asyncio
    from types import SimpleNamespace

    from tghelpers import drain_dispatch

    dispatch = SimpleNamespace(_tasks=set())
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    dispatch._tasks.add(task)
    task.get_loop().call_soon(dispatch._tasks.discard, task)  # what the task's done-callback does, one turn later
    await asyncio.wait_for(drain_dispatch(dispatch), 5)
    assert not dispatch._tasks
