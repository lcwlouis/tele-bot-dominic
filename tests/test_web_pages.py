import re
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from dom.models import StoredMessage
from dom.presence import read_habits
from dom.telegram import owner
from dom.telegram.handlers import handle_callback

from tghelpers import FRIEND, GROUP, OWNER, tg
from test_owner_controls import callback
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN


@pytest.fixture
def browser(monkeypatch):
    from dom.web.app import create_app
    from dom.web.auth import Auth

    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)

    def connect(app):
        config = app.config.model_copy(update={
            "web": app.config.web.model_copy(update={"dev_token_env": "DOM_TEST_TOKEN"}),
        })
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app, Auth(config))),
            base_url=ORIGIN, headers={"Authorization": f"Bearer {TOKEN}"},
        )

    return connect


async def test_dashboard_pages_and_day_dial(make_app, browser):
    app = make_app()
    app.home.write_habits_line(time(23), time(7))
    app.home.write_preset("classic", "---\ntitle: Classic\ntraits:\n  humor: 0.8\n---\n")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Reader")
    await app.store.set_chat_preset(GROUP, "classic")
    now = app.dispatch.now()
    for sender in (OWNER, FRIEND):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(GROUP), role="user", text="Hello", msg_id=sender,
            sender_user_id=sender, created_at=(now - timedelta(hours=1)).isoformat(),
            visibility_scope=app.store.visibility_for(GROUP),
        ))
    habits = read_habits(app.home)
    await app.dispatch.presence.wake(OWNER, habits)
    app.dispatch.now.advance(minutes=5)
    await app.dispatch.presence.sleep(OWNER, habits)
    await app.dispatch.presence.wake(GROUP, habits)
    app.dispatch.now.advance(minutes=5)
    app.home._write_regular("SOUL.md", "One old line")
    await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "<b>A new line</b>"}, "reflection")
    await app.memory_import.run("chatgpt", "- Likes a library\n- Has a notebook")
    await app.memory_import.run("chatgpt", "- Likes a walk")
    await app.store.record_usage(now.isoformat(), "chat", "example-model", "example.org", GROUP, 2, 3, 0.8, 1)
    await app.store.record_usage(
        (now - timedelta(days=1)).isoformat(), "search", "search-model", "example.org", GROUP, 2, 3, 0.1, 0,
    )
    await app.store.record_turn(now, GROUP, "group", 2, ["web_search", "final"], "example-model", True, 250, "Replied")
    await app.store.record_turn(now, OWNER, "owner_dm", 1, ["final"], "example-model", False, 50, "Stayed quiet")

    async with browser(app) as client:
        index = await client.get("/")
        assert index.status_code == 200
        assert all(label in index.text for label in ("Waiting for you", "Chats", "Usage", "Turn log"))
        assert 'id="dial"' in index.text and "Online in Book Club" in index.text and "until 12:35" in index.text
        assert "23:00 to 07:00" in index.text
        assert index.text.count('class="online-arc"') == 1  # the 5-minute owner mark reaches the group's session
        assert 'data-hour="11"' in index.text and 'data-count="2"' in index.text
        assert "/static/dashboard.css" in index.text and "/static/htmx.min.js" in index.text
        assert "<style" not in index.text and " style=" not in index.text
        chats = (await client.get("/chats")).text
        assert all(label in chats for label in ("Classic", "Humor", "Your DM", "Friend DM", "Reader", "Leave group"))
        proposals = (await client.get("/proposals")).text
        assert "&lt;b&gt;A new line&lt;/b&gt;" in proposals and "Approve all" in proposals
        assert proposals.count("memories imported from ChatGPT") == 1
        assert "1 memory imported from ChatGPT" in proposals
        assert "Likes a library" not in (await client.get("/proposals?kind=soul_edit")).text
        assert "A change" not in (await client.get("/proposals?source=import%3Achatgpt")).text
        usage = (await client.get("/usage")).text
        assert "$0.80" in usage and "$0.10" in usage and "search-model" in usage and 'class="fallback-mark"' in usage
        turns = (await client.get(f"/turns?chat={GROUP}")).text
        assert "Replied" in turns and "Stayed quiet" not in turns and "Search" in turns and "Fallback" in turns
        assert (await client.get("/static/htmx.min.js")).status_code == 200


@pytest.mark.parametrize("action,callback_data,shared", [
    ("wake", f"wake:{GROUP}", "override_chat"),
    ("sleep", f"sleep:{GROUP}", "override_chat"),
    ("leave", f"leaveyes:{GROUP}", "leave_chat"),
    ("preset", f"presetset:{GROUP}:classic", "set_preset"),
])
async def test_chat_actions_share_owner_functions(make_app, browser, monkeypatch, action, callback_data, shared):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    app.home.write_preset("classic", "A preset")
    call = AsyncMock(wraps=getattr(owner, shared))
    monkeypatch.setattr(owner, shared, call)
    async with browser(app) as client:
        response = await client.post(
            f"/chats/{GROUP}/{action}", headers=POST_HEADERS, data={"preset": "classic"},
        )
    assert response.status_code == 200
    assert call.await_count == 1
    if action == "leave":
        await app.store.set_chat_auth_status(GROUP, "approved")
    await handle_callback(app, callback(app, OWNER, callback_data))
    assert call.await_count == 2
    if action == "leave":
        assert app.bot.of("leave", GROUP)
    assert any(message.role == "event" for message in await app.store.get_recent_chat_window(OWNER))


async def test_web_decides_through_proposals_including_selected_imports(make_app, browser, monkeypatch):
    app = make_app()
    await app.memory_import.run("chatgpt", "- Likes a library\n- Has a notebook")
    await app.memory_import.run("chatgpt", "- Likes a walk")
    first, second, third = await app.proposals.pending()
    decide = AsyncMock(wraps=app.proposals.decide)
    monkeypatch.setattr(app.proposals, "decide", decide)
    async with browser(app) as client:
        response = await client.post(
            f"/proposals/import/{first['id']}", headers=POST_HEADERS,
            data={"decision": "selected", "selected": [first["id"], third["id"]]},
        )
        assert response.status_code == 200
        decide.assert_awaited_once_with(first["id"], True)
        assert [p["id"] for p in await app.proposals.pending()] == [second["id"], third["id"]]
        response = await client.post(
            f"/proposals/{second['id']}/reject", headers=POST_HEADERS,
        )
        assert response.status_code == 200
        decide.assert_awaited_with(second["id"], False)


async def test_agent_and_dispatch_write_turn_and_daily_retention(make_app):
    app = make_app()
    now = app.dispatch.now()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.dispatch.presence.wake(GROUP, read_habits(app.home))
    from dom.telegram import handlers
    from tghelpers import drain_dispatch

    await handlers.handle_message(app, tg(chat_id=GROUP))
    await drain_dispatch(app.dispatch)
    rows = await app.store.recent_turns()
    assert len(rows) == 1
    row = rows[0]
    assert row["chat_id"] == str(GROUP)
    assert row["scope"] == "group" and row["calls"] == 1 and row["model"] == "m"
    assert row["trace"] == '["final"]' and row["fallback"] == 0
    assert row["outcome"] == "Replied in 1 bubble" and row["duration_ms"] >= 0
    for age in (30, 31):
        await app.store.record_turn(now - timedelta(days=age), OWNER, "owner_dm", 0, [], "", False, 0, "Stayed quiet")
    await app.dispatch.jobs.tick()
    assert len(await app.store.recent_turns()) == 2


def online_arcs(html):
    return re.findall(r'<path d="([^"]+)" class="online-arc"', html)


def today_rows(html):
    section = re.search(r'<dl class="today"[^>]*>(.*?)</dl>', html, re.S).group(1)
    return dict(re.findall(r"<dt>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", section, re.S))


def test_online_arcs_have_a_minimum_length_and_merge():
    from dom.web.pages import MIN_ARC_HOURS, _online_arcs

    assert MIN_ARC_HOURS == 0.25
    assert _online_arcs([(12, 12.05)]) == [[12, 12.25]]
    assert _online_arcs([(12, 13)]) == [[12, 13]]
    assert _online_arcs([(23.95, 24)]) == [[23.75, 24]]  # the day's end keeps the mark inside the dial
    assert _online_arcs([(12, 12.05), (12.1, 12.5)]) == [[12, 12.5]]  # the extended mark reaches the next one
    assert _online_arcs([(15, 15.05), (9, 9.05)]) == [[9, 9.25], [15, 15.25]]
    assert _online_arcs([]) == []


async def test_a_three_minute_wake_is_drawn_as_a_fifteen_minute_arc(make_app, browser):
    from dom.web.pages import _arc

    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    habits = read_habits(app.home)
    await app.dispatch.presence.wake(GROUP, habits)
    app.dispatch.now.advance(minutes=3)
    await app.dispatch.presence.sleep(GROUP, habits)
    async with browser(app) as client:
        arcs = online_arcs((await client.get("/")).text)
    assert arcs == [_arc(12, 12.25)]
    assert arcs != [_arc(12, 12.05)]


@pytest.mark.parametrize("kind,source,label", [
    ("soul_edit", "reflection", "Soul edit, suggested in reflection"),
    ("soul_edit", "dashboard", "Soul edit, asked for in the dashboard"),
    ("memory_fact", "import:chatgpt", "Memory imported from ChatGPT"),
    ("memory_fact", "import:muse", "Memory imported from Muse"),
    ("skill", "desktop", "Skill proposed after a desktop task"),
    ("plugin", "draft_plugin", "Plugin draft"),
])
def test_proposal_labels_are_sentences(kind, source, label):
    from dom.web.pages import _proposal_label

    assert _proposal_label(kind, source) == label
    assert "\u00b7" not in label


async def test_proposal_cards_use_sentence_labels(make_app, browser):
    app = make_app()
    app.home._write_regular("SOUL.md", "One old line")
    await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "A new line"}, "reflection")
    await app.proposals.create("memory_fact", "Likes tea", {"file": "USER.md", "text": "Likes tea"}, "import:chatgpt", notify=False)
    async with browser(app) as client:
        html = (await client.get("/proposals")).text
    assert "Soul edit, suggested in reflection" in html
    assert "\u00b7" not in html


async def test_today_list_reads_usage_turns_and_reminders(make_app, browser):
    app = make_app()
    now = app.dispatch.now()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    async with browser(app) as client:
        empty = today_rows((await client.get("/")).text)
        assert empty == {
            "Turns today": "0", "Replies sent": "0", "Spend today": "$0.00", "Next reminder": "No reminders",
        }
        await app.store.record_usage(now.isoformat(), "chat", "example-model", "example.org", GROUP, 2, 3, 0.8, 0)
        await app.store.record_usage((now - timedelta(days=1)).isoformat(), "chat", "m", "example.org", GROUP, 2, 3, 5, 0)
        for outcome in ("Replied in 2 bubbles", "Replied privately", "Stayed quiet", "Reacted \U0001f923"):
            await app.store.record_turn(now, GROUP, "group", 1, [], "m", False, 5, outcome)
        await app.store.record_turn(now - timedelta(days=1), GROUP, "group", 1, [], "m", False, 5, "Replied in 1 bubble")
        await app.dispatch.reminders.create("2026-01-01T18:30:00", "Water the plants")
        await app.dispatch.reminders.create("2026-01-01T15:00:00", "Call the dentist")
        rows = today_rows((await client.get("/")).text)
    assert rows == {
        "Turns today": "4", "Replies sent": "2", "Spend today": "$0.80", "Next reminder": "15:00 Call the dentist",
    }


async def test_today_counts_all_turns_across_the_owners_local_day(make_app, browser):
    app = make_app()
    zone = ZoneInfo("America/New_York")
    app.config = app.config.model_copy(update={"timezone": zone.key})
    start = datetime(2026, 11, 1, tzinfo=zone)  # This local day has 25 hours.
    end = start + timedelta(days=1)
    app.dispatch.now.at = end - timedelta(minutes=1)

    async def record(at, outcome):
        await app.store.record_turn(at, OWNER, "owner_dm", 1, [], "m", False, 5, outcome)

    await record(start - timedelta(microseconds=1), "Replied")
    for minute in range(205):
        await record(start + timedelta(minutes=minute), "Replied privately" if minute % 2 == 0 else "replied")
    await record(app.dispatch.now(), "Replied")
    await record(end, "Replied")

    async with browser(app) as client:
        rows = today_rows((await client.get("/")).text)
    assert rows["Turns today"] == "206"
    assert rows["Replies sent"] == "104"


async def test_a_reminder_on_another_day_names_the_day(make_app, browser):
    app = make_app()
    await app.dispatch.reminders.create("2026-01-03T09:00:00", "Renew the pass")
    async with browser(app) as client:
        assert today_rows((await client.get("/")).text)["Next reminder"] == "Sat 09:00 Renew the pass"


async def test_forms_apply_on_change_and_only_get_filters_have_no_script_buttons(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    app.home.write_preset("classic", "---\ntitle: Classic\n---\n")
    app.home._write_regular("SOUL.md", "One old line")
    await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "y"}, "reflection", notify=False)
    async with browser(app) as client:
        chats = (await client.get("/chats")).text
        proposals = (await client.get("/proposals")).text
        turns = (await client.get("/turns")).text
    preset = re.search(r'<form class="inline-form" method="post" action="/chats/-?\d+/preset".*?</form>', chats, re.S).group(0)
    assert 'hx-trigger="change"' in preset
    assert re.search(
        r'<label class="sr-only" for="preset-\d+">Preset for .+ \(changes on selection\)</label>', preset,
    )
    assert re.search(r'<select name="preset" id="preset-\d+"', preset)
    assert "<noscript>" not in preset and "<button" not in preset
    for html in (proposals, turns):
        form = re.search(r'<form class="filters".*?</form>', html, re.S).group(0)
        assert 'hx-trigger="change"' in form
        assert re.search(r"<noscript>\s*<button[^>]*>Filter</button>\s*</noscript>", form)
        assert form.count("<button") == 1


async def test_unread_zero_is_fog_and_other_counts_are_not(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "approved", title="Reader")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(FRIEND), role="user", text="Hi", msg_id=1, sender_user_id=FRIEND,
        created_at=app.dispatch.now().isoformat(), visibility_scope=app.store.visibility_for(FRIEND),
    ))
    async with browser(app) as client:
        counts = re.findall(r'<span class="(unread num[^"]*)" aria-label="(\d+) unread"', (await client.get("/chats")).text)
        css = (await client.get("/static/dashboard.css")).text
    assert ("unread num", "1") in counts
    assert all(cls == "unread num zero" for cls, count in counts if count == "0") and len(counts) > 1
    assert ".chat .unread.zero { color: var(--fog); }" in css


async def test_empty_plugin_and_brief_pages_say_what_to_do(make_app, browser):
    app = make_app()
    async with browser(app) as client:
        plugins = (await client.get("/plugins")).text
        briefs = (await client.get("/briefs")).text
    assert "No plugins are installed." in plugins
    assert "Ask Dom in your DM to write a plugin for something he does often." in plugins
    assert "You'll review the code here before it runs." in plugins
    assert "No core-change briefs yet." in briefs
    assert "When Dom thinks his own code should change, he writes a brief here for a coding agent." in briefs


def test_every_interactive_element_keeps_the_arc_focus_ring():
    from pathlib import Path

    import dom.web

    css = (Path(dom.web.__file__).parent / "static" / "dashboard.css").read_text()
    assert ":focus-visible { outline: 2px solid var(--arc); outline-offset: 2px; }" in css
    assert not re.search(r"outline:\s*(none|0)\b", css)
    assert "prefers-reduced-motion: reduce" in css


async def test_everywhere_preset_select_sits_above_the_chats_and_sets_the_global_preset(make_app, browser):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    app.home.write_preset("classic", "---\ntitle: Classic\n---\n")
    await app.store.set_chat_preset(GROUP, "classic")
    async with browser(app) as client:
        chats = (await client.get("/chats")).text
        everywhere = re.search(r'<form class="inline-form" method="post" action="/chats/all/preset".*?</form>', chats, re.S).group(0)
        assert chats.index("/chats/all/preset") < chats.index(f"/chats/{GROUP}/preset")
        assert 'hx-trigger="change"' in everywhere and 'hx-post="/chats/all/preset"' in everywhere
        assert re.search(r'<select name="preset" id="preset-all"', everywhere)
        assert "<button" not in everywhere and "Everywhere" in everywhere
        own = re.search(rf'<form class="inline-form" method="post" action="/chats/{GROUP}/preset".*?</form>', chats, re.S).group(0)
        assert re.search(r'<option value="">Same as everywhere</option>', own)

        assert (await client.post("/chats/all/preset", data={"preset": "classic"})).status_code in (401, 403)
        response = await client.post("/chats/all/preset", headers=POST_HEADERS, data={"preset": "classic"})
        assert response.status_code == 200 and "everywhere to Classic" in response.text
        assert await app.store.kv_get("preset:global") == "classic"
        assert '<option value="classic" selected>' in re.search(
            r'action="/chats/all/preset".*?</form>', response.text, re.S).group(0)

        response = await client.post("/chats/all/preset", headers=POST_HEADERS, data={"preset": "nope"})
        assert "unavailable" in response.text and await app.store.kv_get("preset:global") == "classic"

        await client.post(f"/chats/{GROUP}/preset", headers=POST_HEADERS, data={"preset": ""})
        assert (await app.store.list_chats("approved"))[0]["preset"] == ""


async def test_chats_sliders_edit_validate_and_reset_to_default(make_app, browser):
    from dom import traits

    app = make_app()
    app.home._write_regular("SOUL.md", "---\ntraits:\n  humor: 0.5\n  empathy: 0.5\n---\n")
    app.home.write_preset("calm", "---\ntitle: Calm\ntraits:\n  humor: 0.3\n---\n")
    await app.store.set_chat_auth_status(GROUP, "approved", title="Book Club")
    await app.store.set_chat_auth_status(FRIEND, "pending_approval", title="Stranger")

    async with browser(app) as client:
        chats = (await client.get("/chats")).text
        for name in traits.NAMES:
            assert f'name="{name}"' in chats
            assert f'id="trait-{GROUP}-{name}"' in chats
        assert f'action="/chats/{GROUP}/traits"' in chats
        assert f'hx-post="/chats/{GROUP}/traits/reset"' in chats
        assert "Reset to default" in chats

        # CSRF / htmx protection on slider endpoints
        assert (await client.post(f"/chats/{GROUP}/traits", data={"humor": "0.8"})).status_code in (401, 403)
        assert (await client.post(f"/chats/{GROUP}/traits/reset")).status_code in (401, 403)

        # Out-of-range and alias inputs go through traits validation (clamped to [0.05, 0.95])
        response = await client.post(
            f"/chats/{GROUP}/traits", headers=POST_HEADERS, data={"humor": "1.8", "sarcasm_level": "-1"},
        )
        assert response.status_code == 200
        assert '<details class="traits" open>' in response.text
        assert "Traits for Book Club:" in response.text
        sliders, _ = await traits.for_chat(app.home, app.store, GROUP)
        assert sliders["humor"] == 0.95 and sliders["sarcasm"] == 0.05 and sliders["empathy"] == 0.5

        # Editing one slider preserves existing overrides without pinning untouched traits
        await client.post(f"/chats/{GROUP}/traits", headers=POST_HEADERS, data={"emoji": "0.8"})
        await client.post(f"/chats/{GROUP}/preset", headers=POST_HEADERS, data={"preset": "calm"})
        sliders, _ = await traits.for_chat(app.home, app.store, GROUP)
        assert sliders["humor"] == 0.95 and sliders["emoji"] == 0.8 and sliders["sarcasm"] == 0.05

        # Invalid values or unapproved/unknown chats change nothing
        before = await app.store.get_chat_personality(GROUP)
        for bad in ({"humor": "nan"}, {"humor": "nope"}, {"unknown": "0.5"}, {"humor": "0.7", "empathy": "bad"}):
            resp = await client.post(f"/chats/{GROUP}/traits", headers=POST_HEADERS, data=bad)
            assert resp.status_code == 200 and "unavailable" in resp.text.lower()
            assert await app.store.get_chat_personality(GROUP) == before
        for other in (FRIEND, -999999):
            resp = await client.post(f"/chats/{other}/traits", headers=POST_HEADERS, data={"humor": "0.7"})
            assert resp.status_code == 200 and "unavailable" in resp.text.lower()

        # Reset to default clears per-chat overrides and restores preset/soul defaults
        reset = await client.post(f"/chats/{GROUP}/traits/reset", headers=POST_HEADERS)
        assert reset.status_code == 200 and '<details class="traits" open>' in reset.text
        assert "reset to default" in reset.text.lower()
        assert await app.store.get_chat_personality(GROUP) == ("calm", "{}")
        sliders, _ = await traits.for_chat(app.home, app.store, GROUP)
        assert sliders == {"humor": 0.3, "empathy": 0.5}
        events = await app.store.get_recent_chat_window(OWNER)
        assert any(m.role == "event" and "Traits for Book Club" in m.text for m in events)


async def test_pending_chat_approvals_list_and_share_owner_decision_function(make_app, browser, monkeypatch):
    app = make_app()
    await app.store.set_chat_auth_status(GROUP, "pending_approval", title="Book Club", invited_by_handle="@inviter")
    await app.store.set_chat_auth_status(FRIEND, "pending_approval", title="Fran", invited_by_handle="@newbie")
    decide = AsyncMock(wraps=owner.decide_pending_chat)
    monkeypatch.setattr(owner, "decide_pending_chat", decide)

    async with browser(app) as client:
        index = (await client.get("/")).text
        proposals = (await client.get("/proposals")).text
        for html in (index, proposals):
            assert "Group approval" in html and "Book Club" in html and "Invited by @inviter" in html
            assert "Stranger DM approval" in html and "DM with Fran" in html and "From @newbie" in html
            assert f'hx-post="/proposals/chat/{GROUP}/approve' in html
            assert f'hx-post="/proposals/chat/{GROUP}/ignore' in html
            assert f'hx-post="/proposals/chat/{FRIEND}/approve' in html
            assert f'hx-post="/proposals/chat/{FRIEND}/ignore' in html
        assert "Nothing is waiting for you." not in proposals
        assert 'aria-label="2 proposals"' in proposals

        # Filtering by kind includes or excludes pending chats properly
        app.home._write_regular("SOUL.md", "One old line")
        await app.proposals.create("soul_edit", "A change", {"old": "One old line", "new": "A new line"}, "reflection", notify=False)
        only_soul = (await client.get("/proposals?kind=soul_edit")).text
        assert "A change" in only_soul and "Group approval" not in only_soul
        only_chats = (await client.get("/proposals?kind=chat_approval")).text
        assert "Group approval" in only_chats and "A change" not in only_chats

        # CSRF / htmx protection
        assert (await client.post(f"/proposals/chat/{GROUP}/approve")).status_code in (401, 403)

        # Approve group through dashboard
        approved = await client.post(f"/proposals/chat/{GROUP}/approve", headers=POST_HEADERS)
        assert approved.status_code == 200
        assert approved.headers.get("X-Dom-Decided") == "true"
        assert "Approved: Book Club" in approved.text
        assert "Book Club" not in approved.text.split("Approved: Book Club", 1)[1]
        assert await app.store.is_chat_approved(GROUP)
        assert decide.await_count == 1

        # Re-deciding the same chat reports Already handled without setting X-Dom-Decided
        stale = await client.post(f"/proposals/chat/{GROUP}/ignore", headers=POST_HEADERS)
        assert stale.status_code == 200 and "Already handled." in stale.text
        assert "X-Dom-Decided" not in stale.headers
        assert await app.store.is_chat_approved(GROUP)

    # Telegram owner-DM button uses the exact same decision function
    await handle_callback(app, callback(app, OWNER, f"ignore:{FRIEND}"))
    assert decide.await_count == 3
    assert (await app.store.get_chat_auth_status(FRIEND))["authorization_status"] == "ignored"
    events = await app.store.get_recent_chat_window(OWNER)
    event_texts = [m.text for m in events if m.role == "event"]
    assert any("approved Book Club" in text for text in event_texts)
    assert any("ignored DM with Fran" in text for text in event_texts)
