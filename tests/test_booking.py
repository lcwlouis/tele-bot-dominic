"""Tests for v0.8.6 W2: booking within the owner's windows. No network: Google is the fake from test_google.py."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from test_google import HEADERS, event, names, owner_ctx, pending, pending_ids, ready, settle, setup, tap  # noqa: F401  (setup is a fixture)
from tghelpers import FRIEND, GROUP, OWNER

from dom import booking as booking_module
from dom.booking import (
    BOOKING_CALENDAR, FRIEND_BOOKED, FRIEND_DECLINED, NO_WINDOWS, UNAVAILABLE, Booking, Rules, _booking, find_slots,
    in_window, offered, parse_rules,
)
from dom.models import StoredMessage
from dom.tools import enabled_tools

UTC = ZoneInfo("UTC")
MON = datetime(2026, 11, 30, 9, 0, tzinfo=timezone.utc)  # a Monday morning
BOOK_CAL = "bk1@group.calendar.example.org"
WINDOWS = {
    "windows": [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "19:00", "end": "22:00"},
        {"days": ["sat"], "start": "13:00", "end": "18:00"},
    ],
    "notice_hours": 12, "ahead_days": 14,
}
SECRETS = ("Secret dentist", "Dr Sharma's clinic", "therapist@example.org", "Divorce lawyer")


def friend_ctx(app, scope="friend_dm"):
    chat = FRIEND if scope == "friend_dm" else GROUP
    return owner_ctx(app, scope, chat, FRIEND)


async def make(setup, *, rules=True, bookings_calendar=True, switches=("calendar_read", "calendar_write")):
    app, fake, client = await ready(setup, *switches)
    app.dispatch.now.at = MON
    if bookings_calendar:
        fake.calendars[BOOK_CAL] = BOOKING_CALENDAR
        await app.google.keep_calendar(BOOK_CAL, BOOKING_CALENDAR)
    if rules:
        await app.booking.set_rules(WINDOWS)
    for chat in (FRIEND, GROUP):
        await app.store.set_chat_auth_status(chat, "approved")
    await app.store.record_new_message(StoredMessage(
        chat_id=str(FRIEND), msg_id=1, sender_user_id=FRIEND, sender_name="Sam", visibility_scope=app.store.visibility_for(FRIEND),
        text="can we meet?", created_at=MON.isoformat(),
    ))
    return app, fake, client


def busy(fake, *spans):
    fake.events = [
        event(f"e{index}", SECRETS[index % len(SECRETS)], start, end, attendees=(SECRETS[2],), location=SECRETS[1])
        for index, (start, end) in enumerate(spans)
    ]


def stamps(text):
    return [line.split(") ", 1)[1] for line in text.splitlines() if line[:1].isdigit() and ") " in line]


def offer_id(text) -> int:
    return int(text.split()[1].rstrip(","))


def sent(app, chat):
    return [call[2] for call in app.bot.of("send", chat)]


async def card(app):
    """The newest card the owner was sent, once it is out."""
    await pending(app)
    async with asyncio.timeout(2):
        while not app.bot.of("send", OWNER):
            await asyncio.sleep(0.005)
    return app.bot.of("send", OWNER)[-1]


def inserts(fake):
    return [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/events")]


# ---- windows ----------------------------------------------------------------------------------------------------------


def test_windows_are_parsed_exactly_and_what_is_confirmed_is_what_is_kept():
    rules = parse_rules(WINDOWS, Rules())
    assert [w.describe() for w in rules.windows] == ["Mon, Tue, Wed, Thu, Fri 19:00-22:00", "Sat 13:00-18:00"]
    assert (rules.notice_hours, rules.ahead_days, rules.invite_friends) == (12, 14, False)
    assert parse_rules({"windows": [{"days": ["Saturday", "MON"], "start": "9:00", "end": "11:30"}]}, rules).windows[0].days == (0, 5)
    kept = parse_rules({"notice_hours": 3}, rules)  # a setting left out keeps what it was
    assert kept.windows == rules.windows and kept.notice_hours == 3
    assert parse_rules({"windows": []}, rules).windows == ()  # empty clears
    for bad in (
        {"windows": [{"days": ["mon"], "start": "22:00", "end": "19:00"}]},  # across midnight
        {"windows": [{"days": ["funday"], "start": "19:00", "end": "20:00"}]},
        {"windows": [{"days": [], "start": "19:00", "end": "20:00"}]},
        {"windows": [{"days": ["mon"], "start": "7pm", "end": "9pm"}]},
        {"windows": "evenings"},
        {"windows": [{"days": ["mon"], "start": "01:00", "end": "02:00"}] * 15},
        {"notice_hours": -1}, {"notice_hours": True}, {"ahead_days": 0}, {"ahead_days": 61}, {"invite_friends": "yes"},
    ):
        with pytest.raises(ValueError):
            parse_rules(bad, rules)


async def test_the_owner_sets_windows_in_his_dm_and_they_are_kept(setup):
    app, fake, client = await make(setup, rules=False)
    ctx = owner_ctx(app)
    assert "No meeting windows" in await _booking({"action": "windows"}, ctx)
    result = await _booking({"action": "set_windows", **WINDOWS, "invite_friends": True}, ctx)
    assert result.startswith("Saved.") and "Mon, Tue, Wed, Thu, Fri 19:00-22:00" in result and "Sat 13:00-18:00" in result
    stored = json.loads(await app.store.kv_get("booking:rules"))
    assert stored["windows"][1] == {"days": ["sat"], "start": "13:00", "end": "18:00"} and stored["invite_friends"] is True
    assert (await app.booking.view())["ready"] is True
    assert "Sat 13:00-18:00" in await _booking({"action": "windows"}, ctx)
    # A bad request changes nothing and says why.
    bad = await _booking({"action": "set_windows", "windows": [{"days": ["mon"], "start": "22:00", "end": "19:00"}]}, ctx)
    assert bad.startswith("error:") and json.loads(await app.store.kv_get("booking:rules")) == stored
    # An empty list means no booking at all.
    assert "No meeting windows" in await _booking({"action": "set_windows", "windows": []}, ctx)
    assert (await app.booking.rules()).windows == ()


async def test_setting_windows_asks_for_the_booking_calendar_through_the_calendar_card(setup):
    app, fake, client = await make(setup, rules=False, bookings_calendar=False)
    result = await _booking({"action": "set_windows", **WINDOWS}, owner_ctx(app))
    assert f"approve making the calendar '{BOOKING_CALENDAR}'" in result
    row = await pending(app)
    assert row["action"] == "calendar_create"
    await tap(app, "once")
    assert BOOKING_CALENDAR in fake.calendars.values()
    assert await app.booking._calendar() is not None
    again = await _booking({"action": "set_windows", **WINDOWS}, owner_ctx(app))
    assert "approve making" not in again  # it exists now


async def test_only_the_owner_dm_can_see_or_change_windows(setup):
    app, fake, client = await make(setup)
    for scope in ("friend_dm", "group"):
        for action in ("windows", "set_windows"):
            answer = await _booking({"action": action, **WINDOWS}, friend_ctx(app, scope))
            assert answer.startswith("error") and "19:00" not in answer
    assert (await app.booking.rules()).windows[0].start.hour == 19  # nothing changed
    # A friend's tool has no window actions at all, and the owner's has no offer or pick.
    friend_tool = (await offered(app, "friend_dm"))[0]
    owner_tool = (await offered(app, "owner_dm"))[0]
    assert friend_tool.schema["properties"]["action"]["enum"] == ["offer", "pick"]
    assert owner_tool.schema["properties"]["action"]["enum"] == ["windows", "set_windows"]


async def test_the_tool_is_offered_where_it_can_work(setup):
    app, fake, client = await make(setup)
    for scope in ("friend_dm", "group"):
        assert "booking" in names(await enabled_tools(app, scope))
    assert names(await enabled_tools(app, "owner_dm")).count("booking") == 1
    await app.google.set_switch("calendar_write", False)  # nothing to write to: friends are not offered the tool
    for scope in ("friend_dm", "group"):
        assert "booking" not in names(await enabled_tools(app, scope))
    assert "booking" in names(await enabled_tools(app, "owner_dm"))  # the owner can still set his windows
    assert await offered(app, "friend_dm") == []


# ---- slots ------------------------------------------------------------------------------------------------------------


def slots(rules, *, busy_blocks=(), held=(), minutes=60, now=MON, **kwargs):
    found = find_slots(rules, list(busy_blocks), list(held), now=now, minutes=minutes, zone=UTC, **kwargs)
    return [f"{slot:%a %H:%M}" for slot in found]


def test_slots_are_inside_a_window_far_enough_ahead_and_spread_over_days():
    rules = parse_rules(WINDOWS, Rules())
    # Monday 09:00 + 12 hours' notice: tonight's 21:00 is the first, then the next evenings.
    assert slots(rules) == ["Mon 21:00", "Tue 19:00", "Wed 19:00"]
    assert slots(parse_rules({"notice_hours": 0}, rules)) == ["Mon 19:00", "Tue 19:00", "Wed 19:00"]
    # Saturday afternoons are windows too, and Sunday has none.
    saturday = find_slots(rules, [], [], now=MON, minutes=60, zone=UTC, after=datetime(2026, 12, 5, tzinfo=UTC), before=datetime(2026, 12, 6, tzinfo=UTC))
    assert [f"{slot:%a %H:%M}" for slot in saturday] == ["Sat 13:00", "Sat 14:00", "Sat 15:00"]
    assert slots(rules, after=datetime(2026, 12, 6, tzinfo=UTC), before=datetime(2026, 12, 7, tzinfo=UTC)) == []
    # A longer meeting must end inside the window: 19:00 and 19:30 starts fit 150 minutes, 20:00 would end at 22:30.
    long = find_slots(rules, [], [], now=MON, minutes=150, zone=UTC, limit=50)
    assert long and all(slot.hour == 19 and slot.minute in (0, 30) for slot in long if slot.weekday() < 5)


def test_busy_time_and_slots_held_by_other_offers_are_never_offered():
    rules = parse_rules({**WINDOWS, "notice_hours": 0}, Rules())
    block = (datetime(2026, 11, 30, 19, 0, tzinfo=UTC), datetime(2026, 11, 30, 20, 30, tzinfo=UTC))
    assert slots(rules, busy_blocks=[block])[0] == "Mon 20:30"  # the first free half hour after it, not inside it
    held = [(datetime(2026, 12, 1, 19, 0, tzinfo=UTC), 60)]
    assert slots(rules, held=held) == ["Mon 19:00", "Tue 20:00", "Wed 19:00"]
    everything = [(datetime(2026, 11, 30, 0, 0, tzinfo=UTC), datetime(2026, 12, 20, 0, 0, tzinfo=UTC))]
    assert slots(rules, busy_blocks=everything) == []


def test_the_horizon_and_the_notice_bound_the_search():
    rules = parse_rules({**WINDOWS, "notice_hours": 12, "ahead_days": 1}, Rules())
    assert slots(rules) == ["Mon 21:00"]  # only until Tuesday 09:00
    far = parse_rules({**WINDOWS, "notice_hours": 24 * 7}, Rules())
    assert find_slots(far, [], [], now=MON, minutes=60, zone=UTC)[0] >= MON + timedelta(days=7)


def test_a_meeting_is_inside_a_window_only_when_it_ends_inside_it():
    rules = parse_rules(WINDOWS, Rules())
    assert in_window(rules, datetime(2026, 12, 1, 19, 0, tzinfo=UTC), 180, UTC)
    assert not in_window(rules, datetime(2026, 12, 1, 21, 30, tzinfo=UTC), 60, UTC)
    assert not in_window(rules, datetime(2026, 12, 6, 14, 0, tzinfo=UTC), 60, UTC)  # Sunday
    assert in_window(rules, datetime(2026, 12, 5, 13, 0, tzinfo=UTC), 300, UTC)


# ---- offering -----------------------------------------------------------------------------------------------------------


async def test_a_friend_is_offered_free_window_slots_and_nothing_else_leaves(setup):
    app, fake, client = await make(setup)
    busy(fake, ("2026-11-30T21:00:00+00:00", "2026-11-30T22:00:00+00:00"), ("2026-12-01T19:00:00+00:00", "2026-12-01T20:00:00+00:00"))
    result = await _booking({"action": "offer", "minutes": 60, "context": "board games"}, friend_ctx(app))
    assert stamps(result) == [
        "Tue 01 Dec 20:00-21:00 (UTC)", "Wed 02 Dec 19:00-20:00 (UTC)", "Thu 03 Dec 19:00-20:00 (UTC)",
    ]
    # Mon 30 was busy and Tue's first hour was: neither is offered. Only times come out.
    assert not any(secret in result for secret in SECRETS)
    assert "dentist" not in result.lower() and "calendar" not in result.lower().replace("calendar,", "")
    row = await app.store.get_booking_offer(offer_id(result))
    assert row["state"] == "offered" and row["chat_id"] == FRIEND and row["friend"] == "Sam" and row["scope"] == "friend_dm"
    # Google was asked for start and end times only: no title, place or person was even requested.
    reads = [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/events")]
    assert reads and all("summary" not in r.url.params["fields"] and "attendees" not in r.url.params["fields"]
                         and "location" not in r.url.params["fields"] for r in reads)
    paths = {r.url.path for r in reads}
    assert any("primary" in p for p in paths) and any("bk1" in p for p in paths) and any("cal" in p and "bk1" not in p for p in paths)  # every calendar he reads


async def test_in_a_group_the_offer_holds_only_window_slots_and_no_calendar_detail(setup):
    app, fake, client = await make(setup)
    busy(fake, ("2026-12-01T19:00:00+00:00", "2026-12-01T20:00:00+00:00"))
    result = await _booking({"action": "offer"}, friend_ctx(app, "group"))
    assert (await app.booking.rules()).windows
    for line in stamps(result):
        assert in_window(await app.booking.rules(), datetime.strptime("2026 " + line.split(" (")[0].split("-")[0], "%Y %a %d %b %H:%M").replace(tzinfo=UTC), 60, UTC)
    # What the group may see is the tool result: the times and instructions about them, and nothing from the calendar.
    assert not any(secret in result for secret in SECRETS) and len(stamps(result)) == 3
    row = await app.store.get_booking_offer(offer_id(result))
    assert row["scope"] == "group" and row["chat_id"] == GROUP


async def test_no_windows_means_no_booking_and_dom_is_told_to_ask(setup):
    app, fake, client = await make(setup, rules=False)
    for scope in ("friend_dm", "group"):
        assert await _booking({"action": "offer"}, friend_ctx(app, scope)) == NO_WINDOWS
    assert not [r for r in fake.requests if r.url.path.endswith("/events")]  # the calendar was not even read


async def test_offers_need_the_switches_the_calendar_and_a_fully_read_range(setup):
    app, fake, client = await make(setup, bookings_calendar=False)
    assert await _booking({"action": "offer"}, friend_ctx(app)) == UNAVAILABLE  # no booking calendar yet
    app, fake, client = await make(setup)
    fake.statuses = [403]  # Google refuses the read: nothing about it reaches the chat
    answer = await _booking({"action": "offer"}, friend_ctx(app))
    assert answer == UNAVAILABLE and "Google" not in answer and "403" not in answer
    await app.google.set_switch("calendar_read", False)
    assert await _booking({"action": "offer"}, friend_ctx(app)) == UNAVAILABLE
    assert (await app.store.kv_get("booking:rules")) is not None


async def test_it_says_so_when_nothing_fits(setup):
    app, fake, client = await make(setup)
    fake.events = [event("all", "x", "2026-11-30T00:00:00+00:00", "2026-12-20T00:00:00+00:00")]
    answer = await _booking({"action": "offer"}, friend_ctx(app))
    assert not stamps(answer) and "ask the owner for times" in answer


async def test_a_time_offered_to_one_chat_is_not_offered_to_another_and_chats_hold_few_offers(setup):
    app, fake, client = await make(setup)
    first = stamps(await _booking({"action": "offer"}, friend_ctx(app)))
    second = stamps(await _booking({"action": "offer"}, friend_ctx(app, "group")))
    assert first and second and not set(first) & set(second)
    for _ in range(2):
        assert stamps(await _booking({"action": "offer"}, friend_ctx(app)))
    assert "Too many offers" in await _booking({"action": "offer"}, friend_ctx(app))  # the DM already holds three


# ---- picking, the card and the confirmation --------------------------------------------------------------------------


async def offer_and_pick(app, scope="friend_dm", slot=1, **pick):
    ctx = friend_ctx(app, scope)
    text = await _booking({"action": "offer", "minutes": 60}, ctx)
    result = await _booking({"action": "pick", "offer": offer_id(text), "slot": slot, **pick}, ctx)
    return text, result


async def test_a_pick_sends_the_owner_one_card_and_the_friend_hears_only_that_dom_will_check(setup):
    app, fake, client = await make(setup)
    text, result = await offer_and_pick(app, context="board games on the porch")
    assert "will check" in result and "booked" not in result.lower().replace("never that it is booked", "")
    row = await pending(app)
    assert row["action"] == "booking_confirm"
    assert row["can_allow_always"] is False  # the dashboard offers no Allow always either
    sent_card = await card(app)
    shown = sent_card[2]
    assert "Sam" in shown and "their DM" in shown and "Mon 30 Nov 21:00-22:00 (UTC)" in shown and BOOKING_CALENDAR in shown
    assert "board games on the porch" in shown and "Invite: nobody" in shown
    labels = [b.text for r in sent_card[3]["reply_markup"].inline_keyboard for b in r if b.callback_data]
    assert labels == ["Allow once", "Reject"]  # no Allow always: a friend's request is never approved for good from a tap
    assert not inserts(fake) and not sent(app, FRIEND)  # nothing has happened, and the friend has heard nothing from code
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "awaiting"
    again = await _booking({"action": "pick", "offer": offer_id(text), "slot": 2}, friend_ctx(app))
    assert "not open" in again  # one pick per offer


async def test_approval_books_the_event_and_confirms_to_the_friend(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    await tap(app, "once")
    posts = inserts(fake)
    assert len(posts) == 1 and "bk1" in str(posts[0].url)  # the booking calendar, not Dom's other one
    body = json.loads(posts[0].content)
    assert body["summary"] == "Meet Sam" and body["start"] == {"dateTime": "2026-11-30T21:00:00+00:00"}
    assert body["end"] == {"dateTime": "2026-11-30T22:00:00+00:00"} and "attendees" not in body
    assert posts[0].url.params["sendUpdates"] == "none"
    row = await app.store.get_booking_offer(offer_id(text))
    assert row["state"] == "booked" and row["event_id"].startswith("dombook") and body["id"] == row["event_id"]
    told = sent(app, FRIEND)
    assert told == [FRIEND_BOOKED.format(when="Mon 30 Nov 21:00-22:00 (UTC)")]


async def test_the_friend_is_a_guest_only_when_the_owner_allows_it_and_the_email_is_known(setup):
    app, fake, client = await make(setup)
    # Not allowed: the email is ignored, the card says nobody is invited.
    await offer_and_pick(app, email="sam@example.org")
    assert "Invite: nobody" in (await card(app))[2]
    await tap(app, "once")
    assert "attendees" not in json.loads(inserts(fake)[-1].content)
    # Allowed and known: an attendee, an invite from Google, and the card says so.
    await app.booking.set_rules({"invite_friends": True})
    await offer_and_pick(app, slot=2, email="sam@example.org")
    assert "Invite: sam@example.org" in (await card(app))[2]
    await tap(app, "once")
    post = inserts(fake)[-1]
    assert json.loads(post.content)["attendees"] == [{"email": "sam@example.org"}] and post.url.params["sendUpdates"] == "all"
    # Allowed but unknown: no attendee.
    await offer_and_pick(app, slot=3)
    await tap(app, "once")
    assert "attendees" not in json.loads(inserts(fake)[-1].content)
    # A malformed address is refused before anything is asked.
    ctx = friend_ctx(app)
    text = await _booking({"action": "offer"}, ctx)
    assert (await _booking({"action": "pick", "offer": offer_id(text), "slot": 1, "email": "not an address"}, ctx)).startswith("error")
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "offered"


async def test_a_group_booking_works_the_same_and_the_confirmation_goes_to_the_group(setup):
    app, fake, client = await make(setup)
    text, result = await offer_and_pick(app, "group")
    assert "will check" in result
    shown = (await card(app))[2]
    assert "group" in shown and "their DM" not in shown
    await tap(app, "once")
    assert len(inserts(fake)) == 1
    assert sent(app, GROUP) == [FRIEND_BOOKED.format(when=stamps(text)[0])] and not sent(app, FRIEND)


async def test_an_offer_from_another_chat_cannot_be_picked(setup):
    app, fake, client = await make(setup)
    text = await _booking({"action": "offer"}, friend_ctx(app))
    stranger = owner_ctx(app, "group", GROUP, FRIEND)
    answer = await _booking({"action": "pick", "offer": offer_id(text), "slot": 1}, stranger)
    assert "not open" in answer and (await app.store.get_booking_offer(offer_id(text)))["state"] == "offered"
    assert not await app.desktop_permissions.pending_requests()
    assert "not open" in await _booking({"action": "pick", "offer": 9999, "slot": 1}, friend_ctx(app))
    for bad in ({"offer": offer_id(text), "slot": 9}, {"offer": offer_id(text), "slot": 0}, {"offer": "1", "slot": 1}):
        assert (await _booking({"action": "pick", **bad}, friend_ctx(app))).startswith(("error", "That offer"))


# ---- reject, expire, cancel ----------------------------------------------------------------------------------------------


async def test_a_rejection_tells_the_friend_kindly_and_books_nothing(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    await tap(app, "reject")
    assert sent(app, FRIEND) == [FRIEND_DECLINED] and not inserts(fake)
    assert "why" not in FRIEND_DECLINED.lower() and "owner" not in FRIEND_DECLINED.lower()
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "declined"


async def test_an_unanswered_card_expires_and_the_friend_is_told(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app, "group")
    await pending(app)
    await app.desktop_permissions.expire_pending()
    await settle(app)
    assert sent(app, GROUP) == [FRIEND_DECLINED] and not inserts(fake)
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "declined"


async def test_turning_the_switch_off_cancels_waiting_bookings_and_tells_the_friend(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    await pending(app)
    await app.google.set_switch("calendar_write", False)
    await settle(app)
    assert sent(app, FRIEND) == [FRIEND_DECLINED] and not inserts(fake)
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "declined"


async def test_a_time_taken_since_the_offer_is_not_booked(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    await pending(app)
    busy(fake, ("2026-11-30T21:30:00+00:00", "2026-11-30T22:30:00+00:00"))  # the owner filled it while the card waited
    await tap(app, "once")
    assert not inserts(fake) and sent(app, FRIEND) == [FRIEND_DECLINED]  # the same line as any other decline: it never says why
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "declined"


async def test_a_failed_write_tells_the_friend_nothing_about_why(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    await pending(app)
    fake.statuses = [200, 200, 200, 403, 403]  # the three free/busy reads pass; the insert and the look for it afterwards are refused
    await tap(app, "once")
    assert sent(app, FRIEND) == [FRIEND_DECLINED]
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "declined"
    assert "not confirmed" in app.bot.of("send", OWNER)[-1][2]  # the owner is told it failed, with the reason


# ---- the policy ----------------------------------------------------------------------------------------------------------


async def test_the_owner_can_set_booking_to_allow_and_then_no_card_is_sent(setup):
    app, fake, client = await make(setup)
    await app.desktop_permissions.set_policy("booking_confirm", "allow")
    text, result = await offer_and_pick(app)
    assert not app.bot.of("send", OWNER)  # no card
    assert len(inserts(fake)) == 1
    assert FRIEND_BOOKED.format(when=stamps(text)[0]) in result and "without anything else" in result
    assert not sent(app, FRIEND)  # the reply carries the confirmation: code does not talk over the turn
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "booked"


async def test_never_declines_without_a_card_and_allow_always_is_not_a_choice_on_the_card(setup):
    app, fake, client = await make(setup)
    await app.desktop_permissions.set_policy("booking_confirm", "never")
    text, result = await offer_and_pick(app)
    assert FRIEND_DECLINED in result and not app.bot.of("send", OWNER) and not inserts(fake)
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "declined"
    await app.desktop_permissions.set_policy("booking_confirm", "ask")
    await offer_and_pick(app, slot=2)
    row = await pending(app)
    ok, why = await app.desktop_permissions.decide(row["id"], "always", via="dashboard")
    assert not ok and "always asked" in why
    assert (await app.desktop_permissions.get_policies())["booking_confirm"] == "ask"


# ---- offers expire, and survive a restart -----------------------------------------------------------------------------


async def test_an_offer_expires_after_a_few_hours(setup):
    app, fake, client = await make(setup)
    text = await _booking({"action": "offer"}, friend_ctx(app))
    app.dispatch.now.at = MON + timedelta(hours=booking_module.OFFER_HOURS, minutes=1)
    assert "not open" in await _booking({"action": "pick", "offer": offer_id(text), "slot": 1}, friend_ctx(app))
    await app.booking.recover()
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "expired"
    assert stamps(await _booking({"action": "offer"}, friend_ctx(app)))  # and its slots are free to offer again


async def test_an_open_offer_survives_a_restart_and_a_lost_card_tells_the_friend_once(setup):
    app, fake, client = await make(setup)
    open_offer = offer_id(await _booking({"action": "offer"}, friend_ctx(app, "group")))
    waiting, _ = await offer_and_pick(app)
    await pending(app)
    # A restart: a new Booking over the same database, the pending request ended the way startup ends it.
    await app.desktop_permissions.expire_pending()
    await settle(app)
    told = list(sent(app, FRIEND))
    app.booking = Booking(app)
    await app.booking.recover()
    await settle(app)
    assert (await app.store.get_booking_offer(open_offer))["state"] == "offered"  # still pickable
    assert (await app.store.get_booking_offer(offer_id(waiting)))["state"] in ("declined", "expired")
    assert sent(app, FRIEND).count(FRIEND_DECLINED) == 1 == len(told)  # told once, not again by the sweep
    picked = await _booking({"action": "pick", "offer": open_offer, "slot": 1}, friend_ctx(app, "group"))
    assert "will check" in picked and (await pending(app))["action"] == "booking_confirm"


async def test_the_startup_sweep_closes_a_pick_whose_card_died(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    # The process ended with the pick awaiting the owner: no waiter, no task. On start the sweep closes it.
    row = await app.store.get_booking_offer(offer_id(text))
    assert row["state"] == "awaiting"
    await app.store.sweep_booking_offers(MON.isoformat())  # what startup runs, without the tell
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "expired"


# ---- the dashboard -------------------------------------------------------------------------------------------------------


async def test_the_dashboard_reads_and_saves_the_windows(setup):
    app, fake, client = await make(setup, rules=False)
    got = (await client.get("/api/integrations/google/booking", headers={"Authorization": HEADERS["Authorization"]})).json()
    assert got["windows"] == [] and got["zone"] == "UTC" and got["calendar"] == BOOKING_CALENDAR and got["ready"] is True
    body = {"windows": WINDOWS["windows"], "notice_hours": 6, "ahead_days": 30, "invite_friends": True}
    saved = await client.post("/api/integrations/google/booking", headers=HEADERS, json=body)
    assert saved.status_code == 200 and saved.json()["windows"] == WINDOWS["windows"] and saved.json()["notice_hours"] == 6
    assert (await app.booking.rules()).invite_friends is True
    bad = await client.post("/api/integrations/google/booking", headers=HEADERS, json={**body, "windows": [{"days": ["mon"], "start": "22:00", "end": "19:00"}]})
    assert bad.status_code == 409
    for junk in ({**body, "windows": [{"days": ["noday"], "start": "19:00", "end": "20:00"}]}, {**body, "ahead_days": 999}, {**body, "extra": 1}):
        assert (await client.post("/api/integrations/google/booking", headers=HEADERS, json=junk)).status_code == 422
    assert (await client.post("/api/integrations/google/booking", json=body)).status_code in (401, 403)  # not without the dashboard's credentials
    cleared = await client.post("/api/integrations/google/booking", headers=HEADERS, json={**body, "windows": []})
    assert cleared.json()["windows"] == [] and (await app.booking.rules()).windows == ()
    assert any(row["id"] == "booking_confirm" for row in (await client.get("/api/access", headers={"Authorization": HEADERS["Authorization"]})).json()["actions"])


async def test_old_offers_are_closed_and_then_forgotten_as_new_ones_are_made(setup):
    app, fake, client = await make(setup)
    first = offer_id(await _booking({"action": "offer"}, friend_ctx(app)))
    app.dispatch.now.at = MON + timedelta(hours=5)
    second = offer_id(await _booking({"action": "offer"}, friend_ctx(app)))
    assert (await app.store.get_booking_offer(first))["state"] == "expired"
    app.dispatch.now.at = MON + timedelta(days=16)
    await _booking({"action": "offer"}, friend_ctx(app))
    assert await app.store.get_booking_offer(first) is None  # settled two weeks ago: forgotten
    assert (await app.store.get_booking_offer(second))["state"] == "expired"  # just closed: forgotten two weeks from now


# ---- B1: every decline a friend sees is the same vague line ---------------------------------------------------------------

NEW_YORK = ZoneInfo("America/New_York")
LEAKS = ("taken", "busy", "free", "calendar", "owner", "window", "notice", "booked", "schedule", "time off")


async def _reject(app, fake):
    await tap(app, "reject")


async def _expire(app, fake):
    await app.desktop_permissions.expire_pending()


async def _switch_off(app, fake):
    await app.google.set_switch("calendar_write", False)


async def _restart(app, fake):
    app.booking = Booking(app)
    await app.booking.recover()  # the sweep tells the friend; the dead card then ends without telling again
    await app.desktop_permissions.expire_pending()


async def _taken(app, fake):
    busy(fake, ("2026-11-30T21:30:00+00:00", "2026-11-30T22:30:00+00:00"))
    await tap(app, "once")


async def _windows_cleared(app, fake):
    await app.booking.set_rules({"windows": []})
    await tap(app, "once")


async def _windows_moved(app, fake):
    await app.booking.set_rules({"windows": [{"days": ["tue"], "start": "19:00", "end": "22:00"}]})
    await tap(app, "once")


async def _started(app, fake):
    app.dispatch.now.at = MON + timedelta(hours=14)  # the slot (21:00) began an hour ago
    await tap(app, "once")


async def _notice_raised(app, fake):
    await app.booking.set_rules({"notice_hours": 72})
    await tap(app, "once")


async def _write_failed(app, fake):
    fake.statuses = [200, 200, 200, 403, 403]
    await tap(app, "once")


DECLINES = {
    "rejected": (_reject, ""), "expired": (_expire, ""), "switch off": (_switch_off, ""), "restart": (_restart, ""),
    "taken": (_taken, "no longer free"), "windows cleared": (_windows_cleared, "meeting windows"),
    "windows moved": (_windows_moved, "meeting windows"), "started": (_started, "already started"),
    "notice raised": (_notice_raised, "minimum notice"), "write failed": (_write_failed, "not confirmed"),
}


@pytest.mark.parametrize("name", list(DECLINES))
async def test_every_decline_a_group_sees_is_the_same_vague_line_and_reveals_nothing(setup, name):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app, "group")
    await pending(app)
    trigger, note = DECLINES[name]
    await trigger(app, fake)
    await settle(app)
    assert sent(app, GROUP) == [FRIEND_DECLINED] and not sent(app, FRIEND)
    assert name == "write failed" or not inserts(fake)  # (a refused write was tried, and nothing was made)
    assert not any(word in FRIEND_DECLINED.lower() for word in LEAKS) and not any(secret in FRIEND_DECLINED for secret in SECRETS)
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] in ("declined", "expired")
    if note:  # the owner is told why, shortly
        assert any(note in call[2] for call in app.bot.of("send", OWNER))


async def test_a_decline_in_the_replys_own_words_is_the_same_line_whether_it_is_never_or_a_conflict(setup):
    app, fake, client = await make(setup)
    await app.desktop_permissions.set_policy("booking_confirm", "never")
    _, never = await offer_and_pick(app, "group")
    assert FRIEND_DECLINED in never and not any(word in never.lower().replace("owner's", "") for word in ("taken", "busy", "free", "calendar"))
    await app.desktop_permissions.set_policy("booking_confirm", "allow")
    ctx = friend_ctx(app, "group")
    text = await _booking({"action": "offer", "minutes": 60}, ctx)
    busy(fake, ("2026-11-30T00:00:00+00:00", "2026-12-20T00:00:00+00:00"))  # everything fills up before the pick
    conflict = await _booking({"action": "pick", "offer": offer_id(text), "slot": 1}, ctx)
    assert FRIEND_DECLINED in conflict and not any(word in conflict.lower() for word in ("taken", "busy", "free", "calendar"))
    assert not inserts(fake)


# ---- SF2: wall time and instants across a daylight-saving change ----------------------------------------------------------


def local_starts(found, zone=NEW_YORK):
    return [slot.astimezone(zone).strftime("%H:%M") for slot in found]


def test_spring_forward_skips_times_that_do_not_exist():
    rules = parse_rules({"windows": [{"days": ["sun"], "start": "01:00", "end": "04:00"}], "notice_hours": 0, "ahead_days": 30}, Rules())
    now = datetime(2027, 3, 13, 12, 0, tzinfo=UTC)
    found = find_slots(
        rules, [], [], now=now, minutes=30, zone=NEW_YORK, limit=50, after=datetime(2027, 3, 14, 0, 0, tzinfo=UTC), before=datetime(2027, 3, 15, tzinfo=UTC),
    )
    # 02:00 and 02:30 do not exist on 14 March 2027, and 01:30 EST ends at 03:00 EDT.
    assert local_starts(found) == ["01:00", "01:30", "03:00", "03:30"]
    assert [slot.astimezone(UTC).strftime("%H:%M") for slot in found] == ["06:00", "06:30", "07:00", "07:30"]
    # A window ending 04:00 EDT holds a meeting that starts 03:30 EDT for 30 minutes and not for 60: instants, not wall clock.
    assert in_window(rules, datetime(2027, 3, 14, 7, 30, tzinfo=UTC), 30, NEW_YORK) and not in_window(rules, datetime(2027, 3, 14, 7, 30, tzinfo=UTC), 60, NEW_YORK)
    # 01:30 EST for 90 minutes ends 04:00 EDT: three real hours' worth of wall clock is only 90 minutes.
    assert in_window(rules, datetime(2027, 3, 14, 6, 30, tzinfo=UTC), 90, NEW_YORK) and not in_window(rules, datetime(2027, 3, 14, 6, 30, tzinfo=UTC), 91, NEW_YORK)


def test_fall_back_offers_the_first_of_a_repeated_time_and_counts_real_duration():
    rules = parse_rules({"windows": [{"days": ["sun"], "start": "00:30", "end": "02:00"}], "notice_hours": 0, "ahead_days": 30}, Rules())
    now, day = datetime(2027, 11, 6, 12, 0, tzinfo=UTC), dict(after=datetime(2027, 11, 7, 0, 0, tzinfo=UTC), before=datetime(2027, 11, 8, tzinfo=UTC))
    # The window is two and a half real hours (it holds 01:00 twice) and the repeated 01:00 and 01:30 are the first ones.
    found = find_slots(rules, [], [], now=now, minutes=30, zone=NEW_YORK, limit=50, **day)
    assert [slot.astimezone(UTC).strftime("%H:%M") for slot in found] == ["04:30", "05:00", "05:30"]
    # 90 minutes from 01:30 EDT (05:30Z) end at 07:00Z, exactly the 02:00 EST close, though the wall clock reads 03:00.
    held = [(datetime(2027, 11, 7, 4, 30, tzinfo=UTC), 60)]
    long = find_slots(rules, [], held, now=now, minutes=90, zone=NEW_YORK, limit=50, **day)
    assert [slot.astimezone(UTC).strftime("%H:%M") for slot in long] == ["05:30"]
    assert in_window(rules, datetime(2027, 11, 7, 5, 0, tzinfo=UTC), 90, NEW_YORK)


# ---- SF3: one slot is never offered to two friends ------------------------------------------------------------------------


async def test_two_friends_asking_at_once_are_offered_different_slots(setup):
    app, fake, client = await make(setup)
    fake.delays = {"GET": 0.02}  # the free/busy reads overlap, so both are read before either offer is kept
    first, second = await asyncio.gather(
        _booking({"action": "offer"}, friend_ctx(app)), _booking({"action": "offer"}, friend_ctx(app, "group")),
    )
    assert stamps(first) and stamps(second) and not set(stamps(first)) & set(stamps(second))


async def test_a_booked_slot_stays_held_even_before_the_calendar_shows_it(setup):
    app, fake, client = await make(setup)
    await offer_and_pick(app)
    await tap(app, "once")  # booked; this fake's calendar does not list the event, as Google can lag
    assert "booked" == (await app.store.get_booking_offer(1))["state"]
    other = await _booking({"action": "offer"}, friend_ctx(app, "group"))
    assert "Mon 30 Nov 21:00" not in other and stamps(other)


# ---- SF4: no lock across Google, and one approval books once ---------------------------------------------------------------


async def test_a_slow_google_write_does_not_hold_up_another_booking(setup):
    app, fake, client = await make(setup)
    await offer_and_pick(app)
    await offer_and_pick(app, "group")
    first_id, second_id = await pending_ids(app, 2)
    gate, second_in, arrived = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        if request.method == "POST" and request.url.path.endswith("/events"):
            arrived.append(request)
            if len(arrived) == 1:
                await gate.wait()  # the first booking's insert is slow
            else:
                second_in.set()
        return await fake.handle(request)

    app.google._client = __import__("httpx").AsyncClient(transport=__import__("httpx").MockTransport(handler))
    taps = [asyncio.create_task(app.desktop_permissions.decide(i, "once", via="dashboard")) for i in (first_id, second_id)]
    try:
        async with asyncio.timeout(2):
            await second_in.wait()  # the second booking reached Google while the first was still waiting
    finally:
        gate.set()
    await asyncio.gather(*taps)
    await settle(app)
    assert len(arrived) == 2 and sorted(sent(app, FRIEND) + sent(app, GROUP)) == sorted([FRIEND_BOOKED.format(when="Mon 30 Nov 21:00-22:00 (UTC)"), FRIEND_BOOKED.format(when="Tue 01 Dec 19:00-20:00 (UTC)")])


async def test_one_approval_tapped_twice_books_once(setup):
    app, fake, client = await make(setup)
    text, _ = await offer_and_pick(app)
    await pending(app)
    fake.delays = {"POST": 0.05}
    ticket = await app.google.ticket("calendar_write")
    results = await asyncio.gather(app.booking._book(offer_id(text), ticket), app.booking._book(offer_id(text), ticket))
    assert len(inserts(fake)) == 1
    assert sorted(said is None for _, said in results) == [False, True]  # the loser tells nobody anything
    assert (await app.store.get_booking_offer(offer_id(text)))["state"] == "booked"


# ---- rework 2: a crash after Google made the event, what the friend's turn is told, and the end time across DST ----------


async def crashed_after_insert(app, fake, *, event_made: bool):
    """The process died with the approval claimed (state 'booking', the event id saved) and, if `event_made`, after Google
    made the event but before 'booked' was saved. Returns the offer's id; the old process's pending card is then ended."""
    text, _ = await offer_and_pick(app)
    await pending(app)
    offer = offer_id(text)
    row = await app.store.get_booking_offer(offer)
    event_id = "dombook000001abcdef012345"
    assert await app.store.move_booking_offer(offer, "awaiting", "booking", MON.isoformat(), event_id=event_id)
    if event_made:
        fake.events.append(event(event_id, "Meet Sam", row["picked"], (datetime.fromisoformat(row["picked"]) + timedelta(hours=1)).isoformat()))
    await app.desktop_permissions.expire_pending()
    await settle(app)
    return offer


async def test_a_crash_after_google_made_the_event_settles_it_booked_and_confirms_to_the_friend(setup):
    app, fake, client = await make(setup)
    offer = await crashed_after_insert(app, fake, event_made=True)
    assert not sent(app, FRIEND)
    app.booking = Booking(app)
    await app.booking.recover()
    await settle(app)
    assert (await app.store.get_booking_offer(offer))["state"] == "booked"
    assert sent(app, FRIEND) == [FRIEND_BOOKED.format(when="Mon 30 Nov 21:00-22:00 (UTC)")]
    assert not inserts(fake)  # nothing was made twice
    app.booking = Booking(app)
    await app.booking.recover()
    await settle(app)
    assert len(sent(app, FRIEND)) == 1  # and a later start does not tell again


async def test_a_crash_before_google_made_the_event_declines_it(setup):
    app, fake, client = await make(setup)
    offer = await crashed_after_insert(app, fake, event_made=False)
    app.booking = Booking(app)
    await app.booking.recover()
    await settle(app)
    assert (await app.store.get_booking_offer(offer))["state"] == "declined"
    assert sent(app, FRIEND) == [FRIEND_DECLINED]


async def test_when_google_cannot_be_reached_at_the_sweep_a_booking_waits_for_the_next_one(setup):
    app, fake, client = await make(setup)
    offer = await crashed_after_insert(app, fake, event_made=True)
    fake.statuses = [403]  # the first call after start is refused: it is not known whether the event is there
    app.booking = Booking(app)
    await app.booking.recover()
    await settle(app)
    assert (await app.store.get_booking_offer(offer))["state"] == "booking" and not sent(app, FRIEND)
    await app.booking.recover()  # the next start: Google answers
    await settle(app)
    assert (await app.store.get_booking_offer(offer))["state"] == "booked"
    assert sent(app, FRIEND) == [FRIEND_BOOKED.format(when="Mon 30 Nov 21:00-22:00 (UTC)")]


# ---- rework 2: the friend's turn is never told why nothing was offered -----------------------------------------------------


@pytest.mark.parametrize("scope", ["friend_dm", "group"])
async def test_the_tool_result_in_a_friends_chat_never_says_why_nothing_was_offered(setup, scope):
    app, fake, client = await make(setup)
    fake.events = [event("all", SECRETS[0], "2026-11-30T00:00:00+00:00", "2026-12-20T00:00:00+00:00")]
    answer = await _booking({"action": "offer"}, friend_ctx(app, scope))
    assert not stamps(answer) and not any(word in answer.lower() for word in ("window", "free", "busy", "calendar", "schedule"))
    assert "ask the owner for times" in answer
    notes = [row[0] for row in app.store._conn.execute("SELECT text FROM messages WHERE chat_id = ? AND role = 'event'", (str(OWNER),))]
    assert any("no free time" in note for note in notes)  # the owner's own history has the reason


# ---- rework 2: the end time is the real end, across a clock change ---------------------------------------------------------


def test_a_meeting_across_fall_back_shows_its_real_end():
    start = datetime(2027, 11, 7, 5, 30, tzinfo=UTC)  # 01:30 EDT; 90 minutes later it is 02:00 EST, not 03:00
    assert booking_module._when(start, 90, NEW_YORK, tz=False) == "Sun 07 Nov 01:30-02:00"
    assert booking_module._when(datetime(2027, 3, 14, 6, 30, tzinfo=UTC), 90, NEW_YORK, tz=False) == "Sun 14 Mar 01:30-04:00"
