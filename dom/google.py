"""Google for the owner (v0.8.5 W4, v0.8.6 W1 and W4): the OAuth connection, its tokens and the API calls.

The owner connects once from Settings → Services. The dashboard starts an authorization-code flow with PKCE (S256)
and a random `state` that is bound to the browser that started it by a short-lived cookie, is single-use and expires
after ten minutes. The callback exchanges the code on the server and keeps the tokens only in the encrypted
`SecretStore` (namespace `google`). Nothing else, no log line, trace or turn log, ever holds a token.

What Dom may do with the connection is limited three ways, each checked in code at the moment of use: the scope Google
granted, the owner's switch for it (Calendar read, Calendar write, Gmail read, Gmail drafts, Files, Contacts) and the owner's DM.
Dom never sends mail: no send exists here. He never writes to the owner's own calendars either: with the narrow
`calendar.app.created` scope he makes calendars of his own and writes only there. The ids of the calendars he made (and
which of their events carry a nudge) are kept in the encrypted `SecretStore` too, and code checks every write against them.

An expired or revoked refresh token (`invalid_grant`) marks the connection `needs_reconnect`, drops the dead token and
sends the owner exactly one quiet notice; it stays quiet until the owner connects again.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from dom.notice import dashboard_url, send_notice

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
SHEETS_API = "https://sheets.googleapis.com/v4"
PEOPLE_API = "https://people.googleapis.com/v1"

SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.events"
SCOPE_APP_CALENDAR = "https://www.googleapis.com/auth/calendar.app.created"
SCOPE_OPENID = "openid"  # only to learn which Google account connected (the `sub`), so Dom's calendars follow the account
SCOPE_GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
# Only the files Dom made and the files the owner opened with him through Google Picker: never the rest of the Drive.
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_CONTACTS = "https://www.googleapis.com/auth/contacts.readonly"
BASE_SCOPES = (SCOPE_CALENDAR, SCOPE_APP_CALENDAR, SCOPE_GMAIL_READ, SCOPE_GMAIL_COMPOSE, SCOPE_OPENID)
SCOPES = (*BASE_SCOPES, SCOPE_DRIVE_FILE, SCOPE_CONTACTS)  # everything Dom recognises; a connect asks only for BASE_SCOPES and what is named

# The owner's switches, in the order the dashboard shows them, and the scope each needs.
SWITCHES: dict[str, tuple[str, str]] = {
    "calendar_read": (SCOPE_CALENDAR, "Calendar: read events"),
    "calendar_write": (SCOPE_APP_CALENDAR, "Dom's own calendars: create them, add, move and delete events there (each needs your approval)"),
    "gmail_read": (SCOPE_GMAIL_READ, "Gmail: search and read mail"),
    "gmail_drafts": (SCOPE_GMAIL_COMPOSE, "Gmail: save drafts (each needs your approval; Dom never sends)"),
    "drive_files": (SCOPE_DRIVE_FILE, "Files in Google Drive: read the files he made and the ones you hand him, make docs and sheets in his own folder (each write needs your approval)"),
    "contacts_read": (SCOPE_CONTACTS, "Contacts: look up a name for an email address or phone number"),
}

CLIENT_ID_ENV, CLIENT_SECRET_ENV = "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"
PICKER_KEY_ENV, PROJECT_NUMBER_ENV = "GOOGLE_PICKER_API_KEY", "GOOGLE_PROJECT_NUMBER"  # Google Picker, in the owner's browser only
PICKER_PATH = "/google/picker"
CALLBACK_PATH = "/api/integrations/google/callback"
BINDING_COOKIE = "dom_google_binding"
SETTINGS_PATH = "/app/system"
FOREGROUND_SECONDS = 8.0  # what a tool call inside a turn may spend on Google in all; the background has no such bound
SLOW = "Google is slow right now. Try again in a moment."
STALE = "Google was switched off, disconnected or connected to another account since this was asked, so nothing was done."
UNCERTAIN = "Google did not confirm that: it may or may not have happened. Check before trying again."
# The approval kinds whose cards a switch going off cancels.
SWITCH_ACTIONS: dict[str, tuple[str, ...]] = {
    "calendar_write": ("calendar_create", "calendar_add", "calendar_move", "calendar_delete", "booking_confirm"),
    "gmail_drafts": ("gmail_draft",),
    "drive_files": ("doc_create", "doc_append", "sheet_create", "sheet_append", "sheet_update"),
}
ALL_GOOGLE_ACTIONS = (
    "calendar_create", "calendar_add", "calendar_move", "calendar_delete", "gmail_draft", "booking_confirm",
    "doc_create", "doc_append", "sheet_create", "sheet_append", "sheet_update",
)
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("dom_google_deadline", default=None)
NAMESPACE, TOKEN_KEY, CALENDARS_KEY = "google", "oauth", "calendars"
UNKNOWN_ACCOUNT = "unknown"  # a connection made before the account's id was kept
MAX_CALENDARS = 5  # the calendars Dom may keep at once
STATE_KEY = "google:state"
STATE_TTL = 600.0  # seconds a started authorization stays valid
MAX_PENDING = 8
REFRESH_MARGIN = 60.0  # refresh an access token this many seconds before it expires
MAX_TRIES = 4
BACKOFF_CAP = 8.0
CALLS_PER_MINUTE = 60
TIMEOUT = 15.0
RECONNECT_LEAD = "Google needs you to reconnect"
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")


class GoogleError(Exception):
    """A Google failure whose text is safe to show the owner and the model: it never holds a token or a response body."""


class GoogleNotFound(GoogleError):
    """Google answered 404: what was asked for is not there. For a Drive file this also means "Dom was not given it":
    drive.file hides every other file."""


class GoogleSlow(GoogleError):
    """Google did not answer within the time a tool call inside a turn may wait."""


class GoogleUncertain(GoogleError):
    """A write that is not safe to repeat got no clear answer: it may or may not have happened."""


class InvalidCallback(GoogleError):
    """The callback is not one this server started (unknown, expired or used state, or another browser's cookie): it
    changes nothing at all."""


@dataclass(frozen=True)
class Ticket:
    """What an approval was asked under: which switch, which connection, and the epochs of both when the card was made.
    Any change to the switch or the connection moves an epoch, so an older ticket can never be used again."""

    switch: str
    connection: str | None
    switch_epoch: int
    link_epoch: int


class _InvalidGrant(Exception):
    """The token endpoint refused a refresh token as expired or revoked."""


@dataclass
class _Pending:
    verifier: str
    binding_hash: str
    expires: float


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def allows(state: dict[str, Any], switch: str) -> bool:
    return (
        state["status"] == "connected" and state["switches"].get(switch) is True
        and SWITCHES[switch][0] in state["scopes"]
    )


def _default_state() -> dict[str, Any]:
    return {
        "status": "disconnected", "scopes": [], "notified": False, "connected_at": None, "last_error": "",
        "switches": {name: False for name in SWITCHES},
    }


class GoogleIntegration:
    def __init__(self, app: App, client: httpx.AsyncClient | None = None):
        self.app = app
        self._client = client
        self._pending: dict[str, _Pending] = {}
        self._refreshing: dict[str, asyncio.Task[str]] = {}  # by connection: one refresh each, shared by its callers
        self._switch_epoch: dict[str, int] = {name: 0 for name in SWITCHES}  # bumped on every change of a switch
        self._link_epoch = 0  # bumped on every connect, disconnect and revoke
        self._apply = asyncio.Lock()  # around local token and state changes only, never across a network call
        self._calls: deque[float] = deque()
        self.clock = monotonic  # tests replace these two
        self.wall = time.time
        self._calendar_locks: dict[str, asyncio.Lock] = {}
        self._making: set[str] = set()  # names of calendars being made right now
        self.folder_flight: asyncio.Task[str] | None = None  # the one task finding or making Dom's Drive folder (shared by callers)
        self.contacts_warm = False  # the People API wants one empty search before the first real one
        self.agenda: tuple[float, str] | None = None
        self._agenda_refresh: asyncio.Task[None] | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT, trust_env=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---- configuration and state ----------------------------------------------------------------------------------

    def client_credentials(self) -> tuple[str, str] | None:
        config = self.app.config
        client_id, client_secret = config.secret(CLIENT_ID_ENV), config.secret(CLIENT_SECRET_ENV)
        return (client_id.strip(), client_secret.strip()) if client_id and client_secret else None

    def redirect_uri(self) -> str | None:
        return dashboard_url(self.app.config.web.public_url, CALLBACK_PATH)

    def picker_config(self) -> dict[str, str] | None:
        """What the Picker page needs from `.env`: the OAuth client id, a browser API key and the Cloud project number.
        The key and number are never logged or shown anywhere but that one page; None while any is missing."""
        credentials = self.client_credentials()
        key, number = (self.app.config.secret(name) for name in (PICKER_KEY_ENV, PROJECT_NUMBER_ENV))
        if credentials is None or not key or not number or not key.strip() or not number.strip().isdigit():
            return None
        return {"client_id": credentials[0], "api_key": key.strip(), "project_number": number.strip()}

    def picker_url(self, file_id: str | None = None) -> str | None:
        """The dashboard link that opens the Picker, with a file preselected when one is given."""
        url = dashboard_url(self.app.config.web.public_url, PICKER_PATH)
        return None if url is None else url + (f"?{urlencode({'file': file_id})}" if file_id else "")

    async def state(self) -> dict[str, Any]:
        merged = _default_state()
        raw = await self.app.store.kv_get(STATE_KEY)
        if raw:
            try:
                saved = json.loads(raw)
            except ValueError:
                saved = {}
            if isinstance(saved, dict):
                switches = saved.pop("switches", {})
                merged.update({key: value for key, value in saved.items() if key in merged})
                if isinstance(switches, dict):
                    merged["switches"] = {name: switches.get(name) is True for name in SWITCHES}
        return merged

    async def _save(self, **changes: Any) -> dict[str, Any]:
        state = await self.state()
        state.update(changes)
        await self.app.store.kv_set(STATE_KEY, json.dumps(state))
        return state

    async def available(self, switch: str) -> bool:
        """The switch is on, Google granted its scope and the connection is live."""
        return allows(await self.state(), switch)

    async def granted(self, switch: str) -> bool:
        """Google granted the switch's scope to the live connection (the switch itself may be off)."""
        state = await self.state()
        return state["status"] == "connected" and SWITCHES[switch][0] in state["scopes"]

    async def set_switch(self, switch: str, on: bool) -> dict[str, Any]:
        if switch not in SWITCHES:
            raise ValueError("unknown switch")
        self._switch_epoch[switch] += 1  # first, with nothing awaited, so a write about to go out sees it
        async with self._apply:
            state = await self.state()
            if on and (state["status"] != "connected" or SWITCHES[switch][0] not in state["scopes"]):
                raise ValueError("Google has not granted that permission. Connect Google again and tick it on the consent screen.")
            await self._save(switches={**state["switches"], switch: on})
        self.agenda = None
        if not on:  # whatever was waiting for a tap under this switch must not run now
            await self._cancel(SWITCH_ACTIONS.get(switch, ()))
        await self.app.store.record_owner_event(f"Google switch '{switch}' turned {'on' if on else 'off'}")
        return await self.view()

    async def _cancel(self, actions: Iterable[str], *, tag: str | None = None) -> None:
        if actions:
            await self.app.desktop_permissions.cancel_pending(actions, tag=tag)

    async def view(self) -> dict[str, Any]:
        """What the Google card shows. Credentials appear only as set or not set."""
        state = await self.state()
        return {
            "client_id_set": bool(self.app.config.secret(CLIENT_ID_ENV)),
            "client_secret_set": bool(self.app.config.secret(CLIENT_SECRET_ENV)),
            "redirect_uri": self.redirect_uri(),
            "status": state["status"],
            "connected_at": state["connected_at"],
            "last_error": state["last_error"],
            "own_calendars_granted": state["status"] == "connected" and SCOPE_APP_CALENDAR in state["scopes"],
            "picker_ready": self.picker_config() is not None,
            "picker_path": PICKER_PATH,
            "calendars": await self.own_calendars(),
            "switches": [
                {
                    "id": name, "label": label, "on": state["switches"][name],
                    "granted": scope in state["scopes"] and state["status"] == "connected",
                    "extra": scope not in BASE_SCOPES,  # asked for only when wanted: "Reconnect to allow" names it
                }
                for name, (scope, label) in SWITCHES.items()
            ],
        }

    # ---- Dom's own calendars: what he made, and the nudges on their events -----------------------------------------
    #
    # One encrypted record holds a book per Google account (`books`, by a hash of the account's `sub`) and which account
    # connected last. Only the connected account's book is ever shown, counted or written; when another account connects,
    # the old book is set aside (kept for when that account returns) and its nudges are cancelled.

    async def _record(self) -> dict[str, Any]:
        raw = await self.app.secrets.get(NAMESPACE, CALENDARS_KEY)
        raw = raw if isinstance(raw, dict) else {}
        books_raw = raw.get("books") if isinstance(raw.get("books"), dict) else {}
        if "calendars" in raw or "nudges" in raw:  # a record from before books were kept per account
            books_raw = {**books_raw, UNKNOWN_ACCOUNT: raw}
        books = {}
        for account, book in books_raw.items():
            if not isinstance(account, str) or not isinstance(book, dict):
                continue
            books[account] = {
                "calendars": [
                    {"id": item["id"], "name": item["name"]} for item in book.get("calendars") or []
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str)
                ],
                "nudges": {
                    key: {"reminder": item["reminder"], "minutes": item["minutes"]} for key, item in (book.get("nudges") or {}).items()
                    if isinstance(key, str) and isinstance(item, dict) and type(item.get("reminder")) is int and type(item.get("minutes")) is int
                },
            }
        last = raw.get("last")
        return {"books": books, "last": last if isinstance(last, str) else None}

    async def _account(self) -> str | None:
        tokens = await self._tokens()
        return str(tokens.get("account") or UNKNOWN_ACCOUNT) if tokens else None

    @staticmethod
    def _empty_book() -> dict[str, Any]:
        return {"calendars": [], "nudges": {}}

    async def _book(self) -> dict[str, Any]:
        """The connected account's book: `calendars` [{id, name}] he made, and `nudges` "calendar/event" -> {reminder,
        minutes}. Empty while disconnected, so nothing of an account that is not connected shows."""
        account = await self._account()
        if account is None:
            return self._empty_book()
        return (await self._record())["books"].get(account) or self._empty_book()

    async def own_calendars(self) -> list[dict[str, str]]:
        return (await self._book())["calendars"]

    async def nudge_for(self, calendar_id: str, event_id: str) -> dict[str, int] | None:
        return (await self._book())["nudges"].get(f"{calendar_id}/{event_id}")

    async def edit_book(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        """Change what Dom keeps about his calendars: read, change and write back together, with only local work inside."""
        async with self._apply:
            account = await self._account()
            if account is None:
                raise GoogleError("Google is not connected.")
            record = await self._record()
            result = change(record["books"].setdefault(account, self._empty_book()))
            await self.app.secrets.put(NAMESPACE, CALENDARS_KEY, record)
        return result

    def calendar_lock(self, calendar_id: str) -> asyncio.Lock:
        """Held around everything that changes one of Dom's calendars (a write and its nudge, or its removal), so a move and
        a delete cannot interleave their reminders, and a calendar removed while a write waits is seen as gone."""
        return self._calendar_locks.setdefault(calendar_id, asyncio.Lock())

    async def reserve_calendar(self, name: str) -> None:
        """Take a slot for a calendar about to be made: the cap and the name are checked together with the calendars already
        made and those being made right now, with nothing awaited between the check and the reservation."""
        async with self._apply:
            have = [item["name"] for item in await self.own_calendars()]
            taken = {*(n.casefold() for n in have), *self._making}
            if name.casefold() in taken:
                raise GoogleError(f"Dom already has a calendar called '{name}'.")
            if len(have) + len(self._making) >= MAX_CALENDARS:
                raise GoogleError(f"Dom keeps at most {MAX_CALENDARS} calendars: ask the owner to remove one in Settings first.")
            self._making.add(name.casefold())

    def release_calendar(self, name: str) -> None:
        self._making.discard(name.casefold())

    async def keep_calendar(self, calendar_id: str, name: str) -> None:
        def add(book: dict[str, Any]) -> None:
            if all(item["id"] != calendar_id for item in book["calendars"]):
                book["calendars"].append({"id": calendar_id, "name": name})

        await self.edit_book(add)

    async def forget_calendar(self, calendar_id: str) -> list[int]:
        """Drop one calendar and every nudge on its events; the reminder ids those nudges held come back to be cancelled."""
        def drop(book: dict[str, Any]) -> list[int]:
            book["calendars"] = [item for item in book["calendars"] if item["id"] != calendar_id]
            gone = [key for key in book["nudges"] if key.startswith(f"{calendar_id}/")]
            return [book["nudges"].pop(key)["reminder"] for key in gone]

        return await self.edit_book(drop)

    async def remove_calendar(self, calendar_id: str) -> None:
        """Delete one of Dom's calendars at Google (the owner confirmed on the dashboard), then forget it and cancel its
        nudges. An explicit owner action: it works with the write switch off, and needs only a live connection that
        was granted the scope. Only a calendar he made can be named; Google would refuse any other as well. Cards still
        waiting to write to it are cancelled first, and a write already approved finds it gone when it runs."""
        state = await self.state()
        if state["status"] != "connected" or SCOPE_APP_CALENDAR not in state["scopes"]:
            raise GoogleError("Google has not granted Dom his own calendars. Connect Google again.")
        if all(item["id"] != calendar_id for item in await self.own_calendars()):
            raise GoogleError("Dom did not make that calendar.")
        await self._cancel(("calendar_add", "calendar_move", "calendar_delete", "booking_confirm"), tag=calendar_id)
        async with self.calendar_lock(calendar_id):
            with self.budget(FOREGROUND_SECONDS):
                await self.call(
                    "DELETE", f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='')}", ticket=await self.ticket("calendar_write"),
                    allow=(404, 410),
                )
            reminders = await self.forget_calendar(calendar_id)
        for reminder_id in reminders:
            await self.app.dispatch.reminders.cancel(reminder_id)
        await self.app.store.record_owner_event("Removed one of Dom's Google calendars")

    # ---- connect ------------------------------------------------------------------------------------------------

    def begin(self, also: Iterable[str] = ()) -> tuple[str, str]:
        """Start an authorization: the Google consent URL and the binding value for the browser's cookie. It asks for the
        base scopes and, for each switch named in `also`, that switch's scope: a newer scope is requested only when its
        switch is wanted, and Google keeps what was granted before (`include_granted_scopes`)."""
        wanted = list(dict.fromkeys(also))
        if any(name not in SWITCHES for name in wanted):
            raise GoogleError("Dom does not know that permission.")
        scopes = list(dict.fromkeys([*BASE_SCOPES, *(SWITCHES[name][0] for name in wanted)]))
        credentials, redirect = self.client_credentials(), self.redirect_uri()
        if credentials is None:
            raise GoogleError(f"Set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} in .env first.")
        if redirect is None:
            raise GoogleError("Set web.public_url to the dashboard's https address first.")
        now = self.clock()
        self._pending = {key: item for key, item in self._pending.items() if item.expires > now}
        while len(self._pending) >= MAX_PENDING:
            self._pending.pop(next(iter(self._pending)))
        state, verifier, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        self._pending[_sha(state)] = _Pending(verifier, _sha(binding), now + STATE_TTL)
        query = urlencode({
            "client_id": credentials[0], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(scopes), "state": state, "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true",
        })
        return f"{AUTH_URL}?{query}", binding

    async def finish(self, *, state: str | None, binding: str | None, code: str | None, error: str | None) -> None:
        """Complete the callback. The state is spent only once it is valid, so a replay or a second tab finds nothing.
        Behind Cloudflare Access the request also carries an Access identity; on a token-only dashboard it carries no
        dashboard credential (a browser redirect from Google cannot). Either way the single-use, unexpired state
        matching the binding cookie that only the owner's authenticated connect request set is what authenticates it."""
        pending = self._pending.get(_sha(state)) if state else None
        if pending is None or pending.expires <= self.clock():
            raise InvalidCallback("This connect link has expired or was already used. Start again from Settings.")
        if not binding or not hmac.compare_digest(pending.binding_hash, _sha(binding)):
            raise InvalidCallback("This connect link was not started from this browser. Start again from Settings.")
        self._pending.pop(_sha(state), None)  # valid, unexpired and this browser's: only now is it spent
        if error:
            reason = error if _ERROR_CODE.fullmatch(error) else "error"
            raise GoogleError(f"Google did not connect ({reason}).")
        if not code:
            raise GoogleError("Google sent no code. Start again from Settings.")
        credentials, redirect = self.client_credentials(), self.redirect_uri()
        if credentials is None or redirect is None:
            raise GoogleError("Google is not configured.")
        try:
            data = await self._token_call({
                "grant_type": "authorization_code", "code": code, "code_verifier": pending.verifier,
                "client_id": credentials[0], "client_secret": credentials[1], "redirect_uri": redirect,
            })
        except _InvalidGrant:
            raise GoogleError("Google refused the code (invalid_grant). Start again from Settings.") from None
        refresh, access = data.get("refresh_token"), data.get("access_token")
        scopes = [scope for scope in str(data.get("scope", "")).split() if scope in SCOPES]
        if not (isinstance(refresh, str) and refresh and isinstance(access, str) and access):
            raise GoogleError("Google did not return a refresh token. Remove Dom from your Google account's connected apps and try again.")
        if not scopes:
            raise GoogleError("Google granted none of the permissions Dom asked for.")
        account = _account_key(data.get("id_token"))
        async with self._apply:
            self._link_epoch += 1
            # Every connection gets its own id, so a refresh or an approval started under an earlier one can tell.
            await self.app.secrets.put(NAMESPACE, TOKEN_KEY, {
                "refresh_token": refresh, "access_token": access, "expires_at": self.wall() + _lifetime(data),
                "connection": secrets.token_hex(8), "account": account,
            })
            set_aside = await self._switch_account(account)
            state_now = await self.state()
            switches = {name: state_now["switches"][name] and SWITCHES[name][0] in scopes for name in SWITCHES}
            await self._save(
                status="connected", scopes=scopes, notified=False, last_error="", switches=switches,
                connected_at=datetime.now(timezone.utc).isoformat(),
            )
        self.agenda = None
        await self._cancel(ALL_GOOGLE_ACTIONS)  # anything waiting was asked of the account before this one
        if set_aside:  # a different account: the earlier one's calendars stay set aside and its nudges are cancelled
            for reminder_id in set_aside:
                await self.app.dispatch.reminders.cancel(reminder_id)
            await self.app.store.record_owner_event(
                f"A different Google account connected: Dom's calendars from the earlier one are set aside and {len(set_aside)} nudge(s) cancelled"
            )
        await self.app.store.record_owner_event("Connected Google (" + ", ".join(sorted(s.rsplit("/", 1)[-1] for s in scopes)) + ")")

    async def _switch_account(self, account: str) -> list[int]:
        """Called under `_apply` as an account connects: remember it as the last one, and when it is not the one before,
        empty that one's nudges (their reminder ids come back to be cancelled) and keep its calendars for its return."""
        record = await self._record()
        previous, set_aside = record["last"], []
        if previous is not None and previous != account and previous in record["books"]:
            nudges = record["books"][previous]["nudges"]
            set_aside = [item["reminder"] for item in nudges.values()]
            nudges.clear()
        if record["last"] != account or set_aside:
            record["last"] = account
            await self.app.secrets.put(NAMESPACE, CALENDARS_KEY, record)
        return set_aside

    async def record_failure(self, message: str) -> None:
        async with self._apply:
            await self._save(last_error=message[:200])

    async def disconnect(self) -> bool:
        """Revoke the token at Google and delete it here. Returns whether Google confirmed the revoke. A connection
        made while Google was being asked is a different one and is left alone."""
        tokens = await self._tokens()
        connection = tokens.get("connection") if tokens else None
        revoked = False
        if tokens is not None:
            try:
                response = await self.http.post(REVOKE_URL, data={"token": tokens["refresh_token"]})
                revoked = response.is_success
            except httpx.HTTPError as exc:
                log.warning("Google revoke failed (%s)", type(exc).__name__)
            if not revoked:
                log.warning("Google did not confirm the revoke; the token is deleted here anyway")
        async with self._apply:
            current = await self._tokens()
            if current is not None and current.get("connection") != connection:
                return revoked  # a connect finished during the revoke: that account stays connected
            self._link_epoch += 1
            await self.app.secrets.delete(NAMESPACE, TOKEN_KEY)
            await self._save(
                status="disconnected", scopes=[], notified=False, last_error="", connected_at=None,
                switches={name: False for name in SWITCHES},
            )
        self.agenda = None
        await self._cancel(ALL_GOOGLE_ACTIONS)
        await self.app.store.record_owner_event("Disconnected Google" + ("" if revoked else " (Google did not confirm the revoke)"))
        return revoked

    # ---- tokens -------------------------------------------------------------------------------------------------

    async def _tokens(self) -> dict[str, Any] | None:
        tokens = await self.app.secrets.get(NAMESPACE, TOKEN_KEY)
        return tokens if isinstance(tokens, dict) and isinstance(tokens.get("refresh_token"), str) else None

    async def connection_id(self) -> str | None:
        """Which connection is live: it changes whenever Google is connected, so an approval or a refresh can tell it
        was meant for an earlier account."""
        tokens = await self._tokens()
        return str(tokens.get("connection") or "") if tokens else None

    async def ticket(self, switch: str) -> Ticket:
        """What an approval asked now is bound to. The epochs are read first, with nothing awaited, so a change that
        lands while the connection id is read can only make the ticket stale, never wrongly fresh."""
        switch_epoch, link_epoch = self._switch_epoch[switch], self._link_epoch
        return Ticket(switch, await self.connection_id(), switch_epoch, link_epoch)

    def stale(self, ticket: Ticket) -> bool:
        return ticket.switch_epoch != self._switch_epoch[ticket.switch] or ticket.link_epoch != self._link_epoch

    async def permits(self, ticket: Ticket) -> str | None:
        """Why an action may not run now, or None: nothing about its switch or connection moved since it was asked,
        the switch is on and it is still the same connection. Checked right before it runs, and again right before
        the write itself (`call(ticket=...)`)."""
        if self.stale(ticket) or not allows(await self.state(), ticket.switch):
            return "error: " + STALE
        if ticket.connection is None or await self.connection_id() != ticket.connection:
            return "error: " + STALE
        return None

    @contextlib.contextmanager
    def budget(self, seconds: float | None) -> Iterator[None]:
        """Bound (or, with None, unbound) what Google calls made inside may spend in all. Tools inside a turn use
        FOREGROUND_SECONDS so a slow Google never holds the reply."""
        token = _deadline.set(None if seconds is None else self.clock() + seconds)
        try:
            yield
        finally:
            _deadline.reset(token)

    async def _token_call(self, data: dict[str, str]) -> dict[str, Any]:
        """One call to Google's token endpoint with the backoff every Google call gets. Never logs the form."""
        response = await self._send("POST", TOKEN_URL, data=data, authorized=False, idempotent=True)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.is_success:
            return body
        code = body.get("error") if isinstance(body.get("error"), str) else ""
        if code == "invalid_grant":
            raise _InvalidGrant
        reason = code if _ERROR_CODE.fullmatch(code) else f"status {response.status_code}"
        raise GoogleError(f"Google refused the request ({reason}).")

    async def access_token(self, *, force: bool = False, connection: str | None = None) -> str:
        if (await self.state())["status"] != "connected":
            raise GoogleError("Google is not connected.")
        tokens = await self._tokens()
        if tokens is None:
            raise GoogleError("Google is not connected.")
        if connection is not None and tokens.get("connection") != connection:
            raise GoogleError("Google was disconnected or connected to another account since this was asked.")
        access = tokens.get("access_token")
        if not force and isinstance(access, str) and access and float(tokens.get("expires_at") or 0) - self.wall() > REFRESH_MARGIN:
            return access
        key = str(tokens.get("connection") or "")
        if (task := self._refreshing.get(key)) is None:  # single flight per connection: callers share one refresh
            with self.budget(None):  # it carries on in the background even if the caller's own deadline passes
                task = self._refreshing[key] = asyncio.create_task(self._refresh(tokens))
            task.add_done_callback(partial(self._refresh_done, key))
        return await asyncio.shield(task)

    def _refresh_done(self, key: str, task: asyncio.Task) -> None:
        self._refreshing.pop(key, None)
        if not task.cancelled():
            task.exception()  # retrieved here: each waiter sees it, nobody leaves it unretrieved

    async def _refresh(self, tokens: dict[str, Any]) -> str:
        credentials = self.client_credentials()
        if credentials is None:
            raise GoogleError("Google is not configured.")
        connection = tokens.get("connection")
        try:
            data = await self._token_call({
                "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                "client_id": credentials[0], "client_secret": credentials[1],
            })
        except _InvalidGrant:
            await self._revoked(connection)
            raise GoogleError("Google access expired or was revoked. Reconnect it in Settings.") from None
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise GoogleError("Google returned no access token.")
        rotated = data.get("refresh_token")
        async with self._apply:
            current = await self._tokens()
            if current is None or current.get("connection") != connection:
                # Disconnected, or another account connected, while this refresh was away: its result is dropped.
                raise GoogleError("Google was disconnected or connected to another account while refreshing. Try again.")
            await self.app.secrets.put(NAMESPACE, TOKEN_KEY, {
                **current, "access_token": access, "expires_at": self.wall() + _lifetime(data),
                "refresh_token": rotated if isinstance(rotated, str) and rotated else current["refresh_token"],
            })
        return access

    async def _revoked(self, connection: object) -> None:
        """The refresh token is dead: drop it, mark the connection and tell the owner once. Only for the connection
        that failed; a newer one is left alone."""
        async with self._apply:
            current = await self._tokens()
            if current is None or current.get("connection") != connection:
                return
            state = await self.state()
            self._link_epoch += 1
            await self.app.secrets.delete(NAMESPACE, TOKEN_KEY)
            await self._save(status="needs_reconnect", notified=True, last_error="Google access expired or was revoked.")
            first = not (state["status"] == "needs_reconnect" and state["notified"])
        self.agenda = None
        await self._cancel(ALL_GOOGLE_ACTIONS)
        if not first or self.app.owner_id is None:
            return
        try:
            await send_notice(
                self.app, self.app.owner_id, RECONNECT_LEAD,
                ["Calendar and Gmail stay off until you connect again."], path=SETTINGS_PATH, label="Open Settings",
            )
        except Exception:
            log.warning("could not tell the owner that Google needs reconnecting")

    # ---- requests -----------------------------------------------------------------------------------------------

    async def _throttle(self) -> None:
        now = self.clock()
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        if len(self._calls) >= CALLS_PER_MINUTE:
            raise GoogleError("Too many Google requests this minute. Try again shortly.")
        self._calls.append(now)

    async def _send(
        self, method: str, url: str, *, params: dict | None = None, body: Any = None, data: dict | None = None,
        authorized: bool = True, idempotent: bool | None = None, ticket: Ticket | None = None, max_bytes: int | None = None,
    ) -> httpx.Response:
        """One HTTP call, retried on 429, 5xx and network errors with backoff, and a 401 refreshes the token once.

        A call that is not safe to repeat (a POST without an idempotency key) is never retried after Google may have
        acted on it: that raises GoogleUncertain. Inside `budget()` the whole call, retries and backoff included,
        stops at the deadline with GoogleSlow: the token (a refresh in flight included) and the request share that one
        deadline. With a `ticket`, nothing is written once its switch or connection has moved. With `max_bytes` the answer is
        read as it arrives and refused once it passes that size."""
        if idempotent is None:
            idempotent = method != "POST"
        connection = ticket.connection if ticket is not None else None
        deadline, refreshed = _deadline.get(), False
        for attempt in range(MAX_TRIES):
            remaining = None if deadline is None else deadline - self.clock()
            if remaining is not None and remaining <= 0:
                raise GoogleSlow(SLOW)
            await self._throttle()
            sent = False
            try:
                async with asyncio.timeout(remaining):  # None: no deadline. One clock for the token and the request.
                    headers = {"Authorization": f"Bearer {await self.access_token(connection=connection)}"} if authorized else {}
                    if ticket is not None and self.stale(ticket):  # the last look, with nothing awaited before the write
                        raise GoogleError(STALE)
                    sent = True
                    if max_bytes is None:
                        response = await self.http.request(method, url, params=params, json=body, data=data, headers=headers)
                    else:
                        response = await self._bounded(method, url, params, body, data, headers, max_bytes)
            except TimeoutError:  # the deadline ran out, in a refresh still going on or while Google had the request
                raise (GoogleUncertain(UNCERTAIN) if sent and not idempotent else GoogleSlow(SLOW)) from None
            except httpx.HTTPError as exc:
                log.warning("Google %s %s failed (%s)", method, urlsplit(url).path, type(exc).__name__)
                reached = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if not idempotent and reached:
                    raise GoogleUncertain(UNCERTAIN) from None
                if attempt == MAX_TRIES - 1:
                    raise GoogleError("Google is not answering.") from None
                await self._backoff(min(2.0 ** attempt, BACKOFF_CAP), deadline)
                continue
            status = response.status_code
            if status == 401 and authorized and not refreshed:
                refreshed = True
                try:
                    async with asyncio.timeout(None if deadline is None else max(deadline - self.clock(), 0.001)):
                        await self.access_token(force=True, connection=connection)
                except TimeoutError:
                    raise GoogleSlow(SLOW) from None
                continue
            if status == 429 or status >= 500:
                log.warning("Google %s %s answered %s", method, urlsplit(url).path, status)
                if status >= 500 and not idempotent:
                    raise GoogleUncertain(UNCERTAIN)  # Google may have done it before failing
                if attempt == MAX_TRIES - 1:
                    raise GoogleError("Google is busy. Try again later.")
                await self._backoff(min(_retry_after(response) or 2.0 ** attempt, BACKOFF_CAP), deadline)
                continue
            return response
        raise GoogleError("Google is not answering.")

    async def _bounded(
        self, method: str, url: str, params: dict | None, body: Any, data: dict | None, headers: dict[str, str], max_bytes: int,
    ) -> httpx.Response:
        """One call whose answer is read in pieces and refused as soon as it passes `max_bytes`, so a huge answer is never held
        or parsed whole. What comes back is the same kind of response, already decoded."""
        async with self.http.stream(method, url, params=params, json=body, data=data, headers=headers) as streamed:
            pieces, size = [], 0
            async for piece in streamed.aiter_bytes():
                size += len(piece)
                if size > max_bytes:
                    log.warning("Google %s %s answered more than %s bytes", method, urlsplit(url).path, max_bytes)
                    raise GoogleError(f"Google's answer is too big for Dom to read ({max_bytes // 1000} KB at most): ask for less.")
                pieces.append(piece)
            kept = [(k, v) for k, v in streamed.headers.multi_items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(streamed.status_code, headers=kept, content=b"".join(pieces), request=streamed.request)

    async def _backoff(self, delay: float, deadline: float | None) -> None:
        if deadline is not None and delay >= deadline - self.clock():
            raise GoogleSlow(SLOW)  # waiting would run past what a turn may spend
        await self.app.sleep(delay)

    async def call(
        self, method: str, url: str, *, params: dict | None = None, body: Any = None,
        idempotent: bool | None = None, ticket: Ticket | None = None, allow: tuple[int, ...] = (), max_bytes: int | None = None,
    ) -> Any:
        """An authorized API call returning the parsed JSON (None for an empty answer, or an `allow`ed status such as
        a 409 that means "already done"). With `max_bytes`, an answer bigger than that is refused before it is parsed."""
        response = await self._send(
            method, url, params=params, body=body, idempotent=idempotent, ticket=ticket, max_bytes=max_bytes,
        )
        if response.is_success or response.status_code in allow:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                raise GoogleError("Google sent an answer Dom could not read.") from None
        self._refuse(method, url, response)

    async def call_text(self, url: str, *, params: dict | None = None, limit: int) -> tuple[str, bool]:
        """A GET whose answer is text (an export, a file's content), cut to `limit` characters: the text and whether it was cut."""
        response = await self._send("GET", url, params=params)
        if not response.is_success:
            self._refuse("GET", url, response)
        text = response.content.decode("utf-8", errors="replace")
        return text[:limit], len(text) > limit

    @staticmethod
    def _refuse(method: str, url: str, response: httpx.Response) -> None:
        log.warning("Google %s %s answered %s", method, urlsplit(url).path, response.status_code)
        if response.status_code == 403:
            raise GoogleError("Google refused: this permission was not granted. Reconnect Google and tick it.")
        if response.status_code == 404:
            raise GoogleNotFound("Google could not find that.")
        raise GoogleError(f"Google refused the request (status {response.status_code}).")

    # ---- the owner's agenda, kept ready for the turn note --------------------------------------------------------

    async def turn_note(self, scope: str, chat_id: int) -> str:
        """Today and tomorrow for the owner DM's turn note. It only reads what the background refresh left in memory,
        so the reply never waits on Google; a failed refresh leaves the block out."""
        owner_id = self.app.owner_id
        if scope != "owner_dm" or owner_id is None or chat_id != owner_id:
            return ""
        if not await self.available("calendar_read"):
            return ""
        now = self.clock()
        if (self.agenda is None or now - self.agenda[0] > AGENDA_REFRESH) and (
            self._agenda_refresh is None or self._agenda_refresh.done()
        ):
            self._agenda_refresh = self.app.dispatch._spawn(self._refresh_agenda())
        if self.agenda is not None and now - self.agenda[0] <= AGENDA_STALE:
            return self.agenda[1]
        return ""

    async def _refresh_agenda(self) -> None:
        from dom.google_tools import agenda_block

        try:
            text = await agenda_block(self)
        except GoogleError as exc:
            log.info("calendar agenda not refreshed: %s", exc)
            self.agenda = None
        except Exception:
            log.exception("calendar agenda refresh failed")
            self.agenda = None
        else:
            self.agenda = (self.clock(), text)


AGENDA_REFRESH = 900.0  # the agenda is fetched again when older than this
AGENDA_STALE = 3600.0  # and left out of the turn note when older than this


def _account_key(id_token: object) -> str:
    """Which Google account this is: a hash of the `sub` in the ID token the token endpoint just returned over TLS (no
    signature check needed for a token received straight from Google). The token itself is never kept."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        sub = claims.get("sub") if isinstance(claims, dict) else None
    except (IndexError, ValueError):
        return UNKNOWN_ACCOUNT
    return _sha("google-account:" + sub)[:32] if isinstance(sub, str) and sub else UNKNOWN_ACCOUNT


def _lifetime(data: dict[str, Any]) -> float:
    value = data.get("expires_in")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 3000.0


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
