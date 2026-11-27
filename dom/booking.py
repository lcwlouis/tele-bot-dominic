"""Booking within the owner's windows (v0.8.6 W2, E110, D23): a friend asks to meet, in a DM or in a group, and Dom offers
only times the owner said he is willing to meet and that are free on every calendar he reads.

The owner first tells Dom his windows ("weekdays 7 to 10pm, Saturday afternoons") in his own DM; they are kept as
structured rules in the store. With no windows there is no booking. A friend's `offer` finds up to three slots inside a
window, far enough ahead and free across the owner's calendar and Dom's own; `pick` sends the owner one approval card
(who, where, when, why) and tells the friend Dom will check. Only on approval does Dom add the event to his booking
calendar and confirm; a rejection, an expiry or a restart tells the friend kindly and never says why.

What leaves the owner DM is the offered times and nothing else, and that is enforced by construction: the free/busy read
asks Google for times only (no title, place or attendee is requested), the tool result a friend's chat sees is built
here from slot times alone, and a failure says nothing about the calendar. Offers live in the `booking_offers` table, so a
pick finds the slot Dom really offered across restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from dom.actions import Actions, Bubble
from dom.desktop.tools import untrusted
from dom.google import FOREGROUND_SECONDS, GoogleError, Ticket, allows
from dom.google_tools import (
    BUSY_PAGES, PAGE_SIZE, TIMES_ONLY, Card, _ADDRESS, _as_dt, _create_calendar, _events, _google_time, _merge_busy, _on_calendar,
    _own_calendar, _zone, insert_event, parse_when,
)
from dom.models import Scope
from dom.notice import shorten
from dom.telegram import executor
from dom.tools import Tool, TurnCtx

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

KEY = "booking:rules"
BOOKING_CALENDAR = "Dom - Bookings"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_WINDOWS = 14
STEP = timedelta(minutes=30)  # slots begin on a window's start and every half hour after it
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 15, 240, 60
MAX_SLOTS = 3
MAX_OPEN_OFFERS = 3  # open offers a chat may hold at once
OFFER_HOURS = 4  # how long a friend has to pick from what was offered
DEFAULT_NOTICE_HOURS, MAX_NOTICE_HOURS = 12, 24 * 14
DEFAULT_AHEAD_DAYS, MAX_AHEAD_DAYS = 14, 60
_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")

NO_WINDOWS = (
    "The owner has not said when he is up for meeting, so you cannot offer any times. Tell the friend you will check with "
    "him, and use tell_owner to ask him to set his meeting times. Do not guess or invent times."
)
UNAVAILABLE = (
    "Booking is not available right now. Tell the friend you will check with the owner, and use tell_owner if it needs him. "
    "Do not guess or invent times."
)
FRIEND_BOOKED = "All set: {when}. Looking forward to it."
FRIEND_DECLINED = "Sorry, I can't make that one work. Maybe another time?"
FRIEND_TAKEN = "Sorry, that time just got taken. Want me to find another?"


# ---- the owner's rules ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    days: tuple[int, ...]  # 0 is Monday
    start: dtime
    end: dtime

    def to_json(self) -> dict[str, Any]:
        return {"days": [DAYS[day] for day in self.days], "start": self.start.strftime("%H:%M"), "end": self.end.strftime("%H:%M")}

    def describe(self) -> str:
        days = ", ".join(DAYS[day].capitalize() for day in self.days)
        return f"{days} {self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


@dataclass(frozen=True)
class Rules:
    windows: tuple[Window, ...] = ()
    notice_hours: int = DEFAULT_NOTICE_HOURS
    ahead_days: int = DEFAULT_AHEAD_DAYS
    invite_friends: bool = False  # add the friend to the event as an attendee, when their email is known

    def to_json(self) -> dict[str, Any]:
        return {
            "windows": [window.to_json() for window in self.windows], "notice_hours": self.notice_hours,
            "ahead_days": self.ahead_days, "invite_friends": self.invite_friends,
        }

    def describe(self, zone: str) -> str:
        if not self.windows:
            return "No meeting windows are set, so Dom books nothing."
        return (
            f"Meeting windows ({zone}): " + "; ".join(window.describe() for window in self.windows)
            + f". At least {self.notice_hours} hours' notice, up to {self.ahead_days} days ahead. "
            + ("A friend with a known email is invited to the event." if self.invite_friends else "Friends are not invited to the event.")
        )


def _day(raw: object) -> int:
    name = str(raw).strip().lower()[:3]
    if name not in DAYS:
        raise ValueError(f"days must be from {', '.join(DAYS)} (got {str(raw)[:20]!r}).")
    return DAYS.index(name)


def _clock(raw: object, field: str) -> dtime:
    match = _HHMM.fullmatch(str(raw).strip())
    if match is None:
        raise ValueError(f"{field} must be a 24-hour time like 19:00.")
    return dtime(int(match[1]), int(match[2]))


def _whole(raw: object, field: str, low: int, high: int) -> int:
    if type(raw) is not int or not low <= raw <= high:
        raise ValueError(f"{field} must be a whole number from {low} to {high}.")
    return raw


def parse_rules(args: dict, previous: Rules) -> Rules:
    """The rules the owner asked for. `windows` replaces the list (empty clears it, and with it all booking); a setting
    left out keeps what it was. Anything that is not exact is refused, so what Dom confirms back is what is kept."""
    windows = previous.windows
    if "windows" in args:
        raw = args["windows"]
        if not isinstance(raw, list) or len(raw) > MAX_WINDOWS:
            raise ValueError(f"windows must be a list of at most {MAX_WINDOWS} windows.")
        built = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("days"), list) or not item["days"]:
                raise ValueError("each window needs days (a list like [\"mon\", \"tue\"]), start and end.")
            start, end = _clock(item.get("start"), "start"), _clock(item.get("end"), "end")
            if end <= start:
                raise ValueError("a window must end after it starts, on the same day (split one that crosses midnight).")
            built.append(Window(tuple(sorted({_day(day) for day in item["days"]})), start, end))
        windows = tuple(built)
    notice = previous.notice_hours if "notice_hours" not in args else _whole(args["notice_hours"], "notice_hours", 0, MAX_NOTICE_HOURS)
    ahead = previous.ahead_days if "ahead_days" not in args else _whole(args["ahead_days"], "ahead_days", 1, MAX_AHEAD_DAYS)
    invite = previous.invite_friends
    if "invite_friends" in args:
        if not isinstance(args["invite_friends"], bool):
            raise ValueError("invite_friends must be true or false.")
        invite = args["invite_friends"]
    return Rules(windows, notice, ahead, invite)


def _rules_from_json(raw: Any) -> Rules:
    try:
        return parse_rules(raw, Rules()) if isinstance(raw, dict) else Rules()
    except ValueError:  # a record that no longer validates books nothing
        return Rules()


# ---- slots -----------------------------------------------------------------------------------------------------------


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def in_window(rules: Rules, start: datetime, minutes: int, zone: ZoneInfo) -> bool:
    """Whether a meeting of `minutes` from `start` lies wholly inside one of the owner's windows."""
    local = start.astimezone(zone)
    end = local + timedelta(minutes=minutes)
    return any(
        local.weekday() in window.days and datetime.combine(local.date(), window.start, zone) <= local
        and end <= datetime.combine(local.date(), window.end, zone)
        for window in rules.windows
    )


def find_slots(
    rules: Rules, busy: list[tuple[datetime, datetime]], held: list[tuple[datetime, int]], *, now: datetime, minutes: int,
    zone: ZoneInfo, after: datetime | None = None, before: datetime | None = None, limit: int = MAX_SLOTS,
) -> list[datetime]:
    """Up to `limit` start times for a meeting of `minutes`, each inside a window, at least the notice away, within the
    horizon, and clear of every busy block and every slot already offered to someone else. They are spread over different
    days first; the same day is used again only when fewer days than `limit` have room."""
    earliest = max(now + timedelta(hours=rules.notice_hours), after or now)
    latest = min(now + timedelta(days=rules.ahead_days), before or now + timedelta(days=rules.ahead_days))
    length = timedelta(minutes=minutes)
    taken = [*busy, *((start, start + timedelta(minutes=span)) for start, span in held)]
    found: list[datetime] = []
    day = earliest.astimezone(zone).date()
    while datetime.combine(day, dtime.min, zone) < latest:
        for window in rules.windows:
            if day.weekday() not in window.days:
                continue
            slot, close = datetime.combine(day, window.start, zone), datetime.combine(day, window.end, zone)
            while slot + length <= close:
                if earliest <= slot and slot + length <= latest and not any(_overlaps(slot, slot + length, a, b) for a, b in taken):
                    found.append(slot)
                slot += STEP
        day += timedelta(days=1)
    found = sorted(set(found))
    chosen: list[datetime] = []
    for slot in found:  # one per day first, so the friend sees different days
        if len(chosen) < limit and all(slot.date() != other.date() for other in chosen):
            chosen.append(slot)
    for slot in found:
        if len(chosen) < limit and slot not in chosen and all(not _overlaps(slot, slot + length, o, o + length) for o in chosen):
            chosen.append(slot)
    return sorted(chosen)


def _when(start: datetime, minutes: int, zone: ZoneInfo, *, tz: bool = True) -> str:
    local = start.astimezone(zone)
    return f"{local.strftime('%a %d %b %H:%M')}-{(local + timedelta(minutes=minutes)).strftime('%H:%M')}" + (f" ({zone.key})" if tz else "")


# ---- the booking flow ------------------------------------------------------------------------------------------------


class Booking:
    def __init__(self, app: App):
        self.app = app

    def now(self) -> datetime:
        return self.app.dispatch.now()

    async def rules(self) -> Rules:
        raw = await self.app.store.kv_get(KEY)
        try:
            return _rules_from_json(json.loads(raw)) if raw else Rules()
        except ValueError:
            return Rules()

    async def set_rules(self, args: dict) -> Rules:
        """Change the rules, from the owner DM or the dashboard. Raises ValueError for anything not exact."""
        rules = parse_rules(args, await self.rules())
        await self.app.store.kv_set(KEY, json.dumps(rules.to_json()))
        await self.app.store.record_owner_event(
            "Booking windows changed: " + ("; ".join(window.describe() for window in rules.windows) or "none, so no booking"),
        )
        return rules

    async def view(self) -> dict[str, Any]:
        """What the dashboard shows: the rules, whether Google can book, and whether the booking calendar exists."""
        state = await self.app.google.state()
        return {
            **(await self.rules()).to_json(), "zone": _zone(self.app).key,
            "calendar": BOOKING_CALENDAR, "calendar_made": await self._calendar() is not None,
            "ready": allows(state, "calendar_read") and allows(state, "calendar_write"),
        }

    async def _calendar(self) -> dict[str, str] | None:
        return _own_calendar(await self.app.google.own_calendars(), BOOKING_CALENDAR)

    async def ensure_calendar(self) -> str:
        """Ask the owner (W1's card) for the booking calendar when Dom has none. A note for the owner DM's tool result."""
        g = self.app.google
        if await self._calendar() is not None:
            return ""
        if not await g.available("calendar_write"):
            return f" Booking also needs Google's 'Dom's own calendars' switch on, so Dom can make '{BOOKING_CALENDAR}'."
        try:
            with g.budget(FOREGROUND_SECONDS):
                await _create_calendar(g, {"name": BOOKING_CALENDAR}, _zone(self.app))
        except (ValueError, GoogleError) as exc:
            return f" Dom could not ask for the calendar '{BOOKING_CALENDAR}': {exc}"
        return f" Dom asked you to approve making the calendar '{BOOKING_CALENDAR}', where bookings go."

    async def _busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """When the owner is busy across his calendar and every calendar of Dom's, as bare start and end times: the
        request asks Google for times only, so no title, place or person is ever fetched. Raises when any part could not
        be read, because a range that was not fully read is never called free."""
        g = self.app.google
        calendars = ["primary", *(item["id"] for item in await g.own_calendars())]
        reads = await asyncio.gather(*(
            _events(g, start, end, PAGE_SIZE * BUSY_PAGES, pages=BUSY_PAGES, calendar_id=calendar, fields=TIMES_ONLY) for calendar in calendars
        ))
        if any(more for _, more in reads):
            raise GoogleError("the calendar holds more events than could be read")
        return _merge_busy([item for items, _ in reads for item in items])

    # -- the owner's DM

    async def _owner(self, args: dict, ctx: TurnCtx) -> str:
        zone = _zone(self.app).key
        if args.get("action") == "windows":
            return (await self.rules()).describe(zone) + ("" if await self._calendar() else f" The calendar '{BOOKING_CALENDAR}' does not exist yet.")
        if args.get("action") != "set_windows":
            return "error: action must be windows or set_windows"
        try:
            rules = await self.set_rules({key: args[key] for key in ("windows", "notice_hours", "ahead_days", "invite_friends") if key in args})
        except ValueError as exc:
            return f"error: {exc}"
        note = await self.ensure_calendar() if rules.windows else ""
        return f"Saved. {rules.describe(zone)}{note} Tell the owner exactly what was saved, in plain words, so he can correct it."

    # -- a friend's DM or a group

    async def _friend(self, args: dict, ctx: TurnCtx) -> str:
        g = self.app.google
        state = await g.state()
        if not (allows(state, "calendar_read") and allows(state, "calendar_write")):
            return UNAVAILABLE
        action = args.get("action")
        if action == "offer":
            return await self._offer(args, ctx)
        if action == "pick":
            return await self._pick(args, ctx)
        return "error: action must be offer or pick"

    async def _offer(self, args: dict, ctx: TurnCtx) -> str:
        g, zone, now = self.app.google, _zone(self.app), self.now()
        rules = await self.rules()
        if not rules.windows:
            return NO_WINDOWS
        if await self._calendar() is None:
            return UNAVAILABLE
        try:
            minutes = DEFAULT_MINUTES if args.get("minutes") is None else _whole(args["minutes"], "minutes", MIN_MINUTES, MAX_MINUTES)
            after, before = (self._bound(args.get(name), zone) for name in ("after", "before"))
        except ValueError as exc:
            return f"error: {exc}"
        horizon = now + timedelta(days=rules.ahead_days)
        try:
            with g.budget(FOREGROUND_SECONDS):
                busy = await self._busy(now, horizon)
        except (GoogleError, ValueError, TypeError):
            log.info("booking: the calendar could not be read for an offer")
            return UNAVAILABLE  # nothing about the calendar is said, not even why
        held = [(datetime.fromisoformat(start), span) for start, span in await self.app.store.held_booking_slots(now.isoformat())]
        slots = find_slots(rules, busy, held, now=now, minutes=minutes, zone=zone, after=after, before=before)
        if not slots:
            return "There is no free time inside the owner's windows for that. Tell the friend nothing fits then, without saying why, and offer to try other dates."
        offer_id = await self.app.store.create_booking_offer(
            chat_id=ctx.chat_id, scope=ctx.scope, sender_id=ctx.sender_id,
            friend=await self._name(ctx.chat_id, ctx.sender_id), minutes=minutes, slots=[slot.isoformat() for slot in slots],
            context=shorten(" ".join(str(args.get("context") or "").split()), 150), at=now.isoformat(),
            expires_at=(now + timedelta(hours=OFFER_HOURS)).isoformat(), limit=MAX_OPEN_OFFERS,
        )
        if offer_id is None:
            return "Too many offers are open in this chat already. Ask the friend to pick one of those, or tell them you will check with the owner."
        lines = [f"{index}) {_when(slot, minutes, zone)}" for index, slot in enumerate(slots, 1)]
        return (
            f"Offer {offer_id}, {minutes} minutes, open for {OFFER_HOURS} hours. Offer exactly these times and nothing else:\n"
            + "\n".join(lines)
            + f"\nWhen the friend picks one, call booking pick with offer {offer_id} and its number, then say you will check and get back "
            "to them. Do not say anything else about the owner's schedule, and never offer a time that is not listed."
        )

    @staticmethod
    def _bound(raw: object, zone: ZoneInfo) -> datetime | None:
        if raw in (None, ""):
            return None
        return _as_dt(parse_when(raw, zone, field="after/before"), zone)

    async def _name(self, chat_id: int, sender_id: int | None) -> str:
        name = await self.app.store.latest_sender_name(chat_id, sender_id) if sender_id is not None else ""
        if not name:
            name = str((await self.app.store.get_chat_auth_status(str(chat_id))).get("title") or "")
        return shorten(" ".join(name.split()), 60) or "a friend"

    async def _pick(self, args: dict, ctx: TurnCtx) -> str:
        g, zone, store, now = self.app.google, _zone(self.app), self.app.store, self.now()
        offer = await store.get_booking_offer(args["offer"]) if type(args.get("offer")) is int else None
        # Another chat's offer is not found at all: an offer id says nothing outside the chat that holds it.
        if offer is None or offer["chat_id"] != ctx.chat_id or offer["state"] != "offered" or datetime.fromisoformat(offer["expires_at"]) <= now:
            return "That offer is not open any more. Offer times again if the friend still wants to meet."
        slots = [datetime.fromisoformat(item) for item in json_list(offer["slots"])]
        index = args.get("slot")
        if type(index) is not int or not 1 <= index <= len(slots):
            return f"error: slot must be a number from 1 to {len(slots)}, one of the times offered."
        start, minutes = slots[index - 1], offer["minutes"]
        rules = await self.rules()
        if start <= now or not in_window(rules, start, minutes, zone):
            return "That time is no longer open. Offer times again."
        email = str(args.get("email") or "").strip()
        if email and not _ADDRESS.fullmatch(email):
            return "error: that email address does not look right. Ask the friend for it again, or leave it out."
        invite = email if rules.invite_friends and email else ""
        if (calendar := await self._calendar()) is None:
            return UNAVAILABLE
        perms = self.app.desktop_permissions
        if await perms.resolve("booking_confirm") == "never":
            await store.move_booking_offer(offer["id"], "offered", "declined", now.isoformat())
            return "The owner is not taking bookings through you. Tell the friend kindly that you can't set it up, without giving a reason."
        friend = await self._name(ctx.chat_id, ctx.sender_id)
        context = shorten(" ".join(str(args.get("context") or offer["context"]).split()), 150)
        if not await store.move_booking_offer(
            offer["id"], "offered", "awaiting", now.isoformat(), picked=start.isoformat(), email=invite, friend=friend, sender_id=ctx.sender_id,
        ):
            return "That offer is already being checked. Tell the friend you are still checking."
        where = f"group “{await self._title(ctx.chat_id)}”" if ctx.scope == "group" else "their DM"
        card = Card([
            f"Book {minutes} min with: “{friend}”", f"In: {where}", f"When: {_when(start, minutes, zone)}", f"On: {calendar['name']}",
            f"Invite: {invite} (Google emails them)" if invite else "Invite: nobody, it goes on your calendar only",
            *([f"About: “{context}”"] if context else []),
        ], headline=f"Book {minutes} min with “{friend}”")
        ticket = await g.ticket("calendar_write")

        async def run(background: bool) -> str:
            with g.budget(None if background else FOREGROUND_SECONDS):
                owner_text, said = await self._book(offer["id"], ticket)
            if background:  # the owner tapped later: Dom tells the friend himself, and the owner's history gets the outcome
                await self._tell(ctx.chat_id, said)
                return owner_text
            # Policy Allow, inside the friend's turn: the reply tells the friend, so the model is given the line to say.
            await store.record_owner_event(f"Booking for {untrusted(friend, limit=80)}: {owner_text}"[:600])
            return f"Tell the friend, in your own words and without anything else about the owner: \"{said}\""

        async def declined() -> None:
            if await store.move_booking_offer(offer["id"], "awaiting", "declined", self.now().isoformat()):
                await self._tell(ctx.chat_id, FRIEND_DECLINED)

        try:
            result = await perms.run_governed(
                "booking_confirm", detail=card.detail(), lines=card.telegram(), dashboard_only=not card.fits(),
                perform=run, guard=lambda: g.permits(ticket), tag=calendar["id"], declined=declined,
            )
        except BaseException:
            await store.move_booking_offer(offer["id"], "awaiting", "offered", now.isoformat())
            raise
        if result.startswith("error"):  # nothing was asked and nothing ran: the offer stays open
            await store.move_booking_offer(offer["id"], "awaiting", "offered", now.isoformat())
            return UNAVAILABLE
        if result.startswith("Asked the owner"):
            return "Asked the owner. Tell the friend, in a line, that you will check and get back to them. Say nothing else about it."
        return result

    async def _title(self, chat_id: int) -> str:
        return shorten(" ".join(str((await self.app.store.get_chat_auth_status(str(chat_id))).get("title") or "").split()), 60) or str(chat_id)

    async def _book(self, offer_id: int, ticket: Ticket) -> tuple[str, str]:
        """The approved booking: make the event on the booking calendar under the calendar's lock and settle the offer,
        always. Returns what the owner's history gets, and the line for the friend (kind, and never why)."""
        g, zone, store = self.app.google, _zone(self.app), self.app.store
        offer = await store.get_booking_offer(offer_id)
        if offer is None or offer["state"] != "awaiting" or not offer["picked"]:
            return "error: that offer is no longer open, so nothing was booked.", FRIEND_DECLINED
        start, minutes = datetime.fromisoformat(offer["picked"]), offer["minutes"]
        end = start + timedelta(minutes=minutes)
        said = FRIEND_DECLINED

        async def work() -> str:
            nonlocal said
            if any(_overlaps(start, end, a, b) for a, b in await self._busy(start, end)):  # taken since it was offered
                said = FRIEND_TAKEN
                return "not booked: that time is no longer free on the calendar."
            event_id = f"dombook{offer_id:06d}{secrets.token_hex(6)}"  # base32hex-safe; a repeat finds it already there
            body: dict[str, Any] = {
                "summary": f"Meet {offer['friend']}", "start": _google_time(start), "end": _google_time(end),
                "description": "Booked by Dom for a friend." + (f" About: {offer['context']}" if offer["context"] else ""),
            }
            if offer["email"]:
                body["attendees"] = [{"email": offer["email"]}]
            if (problem := await insert_event(g, calendar, event_id, body, ticket, updates="all" if offer["email"] else "none")) is not None:
                return problem
            if not await store.move_booking_offer(offer_id, "awaiting", "booked", self.now().isoformat(), event_id=event_id):
                return "error: the offer was closed while it was booked: check the calendar."
            said = FRIEND_BOOKED.format(when=_when(start, minutes, zone))
            return f"Booked {minutes} min with {offer['friend']} on {_when(start, minutes, zone)}."

        try:
            if (calendar := await self._calendar()) is None:
                result = "error: Dom's booking calendar is gone, so nothing was booked."
            else:
                result = await _on_calendar(g, calendar, work)
        except GoogleError as exc:
            result = f"error: {exc}"
        except Exception:  # whatever happened, the offer is settled and the friend told below
            log.exception("booking %s failed", offer_id)
            result = "error: it failed unexpectedly"
        if result.startswith("Booked"):
            await store.record_chat_event(offer["chat_id"], f"Booking confirmed for {_when(start, minutes, zone)}.")
        else:
            await store.move_booking_offer(offer_id, "awaiting", "declined", self.now().isoformat())
            said = said if said == FRIEND_TAKEN else FRIEND_DECLINED
        return result, said

    async def _tell(self, chat_id: int, text: str) -> None:
        app = self.app
        try:
            if not await app.store.is_chat_approved(chat_id):
                return
            async with app.dispatch.chat_slot(chat_id):
                await executor.perform(
                    app.bot, app.store, chat_id, None, Actions(bubbles=[Bubble(text=text)]), sleep=app.sleep, now=app.dispatch.now,
                    link_previews=False,
                )
        except Exception:
            log.exception("could not tell chat %s about its booking", chat_id)

    async def recover(self) -> None:
        """At start: close what ran out, and tell the friends whose booking was waiting for a tap that died with the
        restart (their card is gone, so it can never be approved)."""
        try:
            lost = await self.app.store.sweep_booking_offers(self.now().isoformat())
        except Exception:
            log.exception("could not sweep booking offers")
            return
        for offer in lost:
            self.app.dispatch._spawn(self._tell(offer["chat_id"], FRIEND_DECLINED))


def json_list(raw: str) -> list[str]:
    value = json.loads(raw)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


# ---- the tool --------------------------------------------------------------------------------------------------------


async def _booking(args: dict, ctx: TurnCtx) -> str:
    app = ctx.app
    if app is None:
        return "error: booking is not available here"
    if ctx.is_owner_dm():
        return await app.booking._owner(args, ctx)
    if ctx.scope == "owner_dm" or ctx.chat_id == ctx.owner_id:
        return "error: booking is not available here"
    return await app.booking._friend(args, ctx)


_WINDOW = {
    "type": "object",
    "properties": {
        "days": {"type": "array", "items": {"type": "string", "enum": list(DAYS)}, "minItems": 1},
        "start": {"type": "string", "description": "24-hour time like 19:00"}, "end": {"type": "string", "description": "24-hour time like 22:00"},
    },
    "required": ["days", "start", "end"], "additionalProperties": False,
}


async def offered(app: App, scope: Scope) -> list[Tool]:
    """The booking tool: windows and their settings in the owner's DM; offering and picking in friends' DMs and groups,
    and only while Google can read the calendar and write to Dom's own."""
    if scope == "owner_dm":
        return [Tool(
            name="booking",
            description=(
                "When the owner is willing to meet friends. windows: show the current meeting windows. set_windows: replace them "
                "(windows is a list of days, start and end in the owner's timezone; an empty list means no booking at all), and "
                "optionally notice_hours (least notice), ahead_days (furthest ahead) and invite_friends (add a friend with a known "
                "email to the event). Turn what the owner says into exact windows, save them, then repeat them back so he can correct "
                "you. Without windows Dom books nothing. Friends are offered only times inside these windows that are also free on "
                "the calendar, and the owner approves each booking with a card."
            ),
            schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["windows", "set_windows"]},
                    "windows": {"type": "array", "items": _WINDOW, "maxItems": MAX_WINDOWS},
                    "notice_hours": {"type": "integer", "minimum": 0, "maximum": MAX_NOTICE_HOURS},
                    "ahead_days": {"type": "integer", "minimum": 1, "maximum": MAX_AHEAD_DAYS},
                    "invite_friends": {"type": "boolean"},
                },
                "required": ["action"], "additionalProperties": False,
            },
            handler=_booking, scopes={"owner_dm"},
        )]
    state = await app.google.state()
    if not (allows(state, "calendar_read") and allows(state, "calendar_write")):
        return []
    return [Tool(
        name="booking",
        description=(
            "Set up a meeting between the owner and the person asking, within the times the owner is willing to meet. offer: "
            "find up to three free times of `minutes` (optionally after or before an ISO date); say those times and only those. "
            "pick: the friend chose one (offer id and slot number, plus their email only if they gave it, and context for what it is "
            "about); the owner is asked, so tell the friend you will check, never that it is booked. Never state, guess or hint at "
            "the owner's schedule or availability yourself: times come from this tool or not at all. If it says there are no times "
            "or windows, say you will check with him."
        ),
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["offer", "pick"]},
                "minutes": {"type": "integer", "minimum": MIN_MINUTES, "maximum": MAX_MINUTES},
                "after": {"type": "string", "description": "ISO date or date and time: not before"},
                "before": {"type": "string", "description": "ISO date or date and time: not after"},
                "context": {"type": "string", "maxLength": 150},
                "offer": {"type": "integer"}, "slot": {"type": "integer", "minimum": 1, "maximum": MAX_SLOTS},
                "email": {"type": "string", "maxLength": 254},
            },
            "required": ["action"], "additionalProperties": False,
        },
        handler=_booking, scopes={"friend_dm", "group"},
    )]
