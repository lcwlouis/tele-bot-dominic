"""Google Calendar and Gmail for the owner (v0.8.5 W4): the OAuth connection, its tokens and the API calls.

The owner connects once from Settings → Services. The dashboard starts an authorization-code flow with PKCE (S256)
and a random `state` that is bound to the browser that started it by a short-lived cookie, is single-use and expires
after ten minutes. The callback exchanges the code on the server and keeps the tokens only in the encrypted
`SecretStore` (namespace `google`). Nothing else, no log line, trace or turn log, ever holds a token.

What Dom may do with the connection is limited three ways, each checked in code at the moment of use: the scope Google
granted, the owner's switch for it (Calendar read, Calendar write, Gmail read, Gmail drafts) and the owner's DM.
Dom never sends mail: no send exists here.

An expired or revoked refresh token (`invalid_grant`) marks the connection `needs_reconnect`, drops the dead token and
sends the owner exactly one quiet notice; it stays quiet until the owner connects again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

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

SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.events"
SCOPE_GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPES = (SCOPE_CALENDAR, SCOPE_GMAIL_READ, SCOPE_GMAIL_COMPOSE)

# The owner's switches, in the order the dashboard shows them, and the scope each needs.
SWITCHES: dict[str, tuple[str, str]] = {
    "calendar_read": (SCOPE_CALENDAR, "Calendar: read events"),
    "calendar_write": (SCOPE_CALENDAR, "Calendar: add, move and delete events (each needs your approval)"),
    "gmail_read": (SCOPE_GMAIL_READ, "Gmail: search and read mail"),
    "gmail_drafts": (SCOPE_GMAIL_COMPOSE, "Gmail: save drafts (each needs your approval; Dom never sends)"),
}

CLIENT_ID_ENV, CLIENT_SECRET_ENV = "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"
CALLBACK_PATH = "/api/integrations/google/callback"
BINDING_COOKIE = "dom_google_binding"
SETTINGS_PATH = "/app/system"
NAMESPACE, TOKEN_KEY = "google", "oauth"
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
        self._refreshing: asyncio.Task[str] | None = None
        self._calls: deque[float] = deque()
        self._reconnect_sent = False  # set before the first await, so two failing calls send one notice
        self.clock = monotonic  # tests replace these two
        self.wall = time.time
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

    async def set_switch(self, switch: str, on: bool) -> dict[str, Any]:
        if switch not in SWITCHES:
            raise ValueError("unknown switch")
        state = await self.state()
        if on and (state["status"] != "connected" or SWITCHES[switch][0] not in state["scopes"]):
            raise ValueError("Google has not granted that permission. Connect Google again and tick it on the consent screen.")
        switches = {**state["switches"], switch: on}
        await self._save(switches=switches)
        self.agenda = None
        await self.app.store.record_owner_event(f"Google switch '{switch}' turned {'on' if on else 'off'}")
        return await self.view()

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
            "switches": [
                {
                    "id": name, "label": label, "on": state["switches"][name],
                    "granted": scope in state["scopes"] and state["status"] == "connected",
                }
                for name, (scope, label) in SWITCHES.items()
            ],
        }

    # ---- connect ------------------------------------------------------------------------------------------------

    def begin(self) -> tuple[str, str]:
        """Start an authorization: the Google consent URL and the binding value for the browser's cookie."""
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
            "scope": " ".join(SCOPES), "state": state, "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true",
        })
        return f"{AUTH_URL}?{query}", binding

    async def finish(self, *, state: str | None, binding: str | None, code: str | None, error: str | None) -> None:
        """Complete the callback. The state is spent first, so a replay or a second tab finds nothing."""
        pending = self._pending.pop(_sha(state), None) if state else None
        if pending is None or pending.expires <= self.clock():
            raise GoogleError("This connect link has expired or was already used. Start again from Settings.")
        if not binding or not hmac.compare_digest(pending.binding_hash, _sha(binding)):
            raise GoogleError("This connect link was not started from this browser. Start again from Settings.")
        if error:
            reason = error if _ERROR_CODE.fullmatch(error) else "error"
            raise GoogleError(f"Google did not connect ({reason}).")
        if not code:
            raise GoogleError("Google sent no code. Start again from Settings.")
        credentials, redirect = self.client_credentials(), self.redirect_uri()
        if credentials is None or redirect is None:
            raise GoogleError("Google is not configured.")
        data = await self._token_call({
            "grant_type": "authorization_code", "code": code, "code_verifier": pending.verifier,
            "client_id": credentials[0], "client_secret": credentials[1], "redirect_uri": redirect,
        })
        refresh, access = data.get("refresh_token"), data.get("access_token")
        scopes = [scope for scope in str(data.get("scope", "")).split() if scope in SCOPES]
        if not (isinstance(refresh, str) and refresh and isinstance(access, str) and access):
            raise GoogleError("Google did not return a refresh token. Remove Dom from your Google account's connected apps and try again.")
        if not scopes:
            raise GoogleError("Google granted none of the permissions Dom asked for.")
        await self.app.secrets.put(NAMESPACE, TOKEN_KEY, {
            "refresh_token": refresh, "access_token": access, "expires_at": self.wall() + _lifetime(data),
        })
        state_now = await self.state()
        switches = {name: state_now["switches"][name] and SWITCHES[name][0] in scopes for name in SWITCHES}
        await self._save(
            status="connected", scopes=scopes, notified=False, last_error="", switches=switches,
            connected_at=datetime.now(timezone.utc).isoformat(),
        )
        self._reconnect_sent = False
        self.agenda = None
        await self.app.store.record_owner_event("Connected Google (" + ", ".join(sorted(s.rsplit("/", 1)[-1] for s in scopes)) + ")")

    async def record_failure(self, message: str) -> None:
        await self._save(last_error=message[:200])

    async def disconnect(self) -> bool:
        """Revoke the token at Google and delete it here. Returns whether Google confirmed the revoke."""
        tokens = await self.app.secrets.get(NAMESPACE, TOKEN_KEY)
        revoked = False
        if isinstance(tokens, dict) and isinstance(tokens.get("refresh_token"), str):
            try:
                response = await self.http.post(REVOKE_URL, data={"token": tokens["refresh_token"]})
                revoked = response.is_success
            except httpx.HTTPError as exc:
                log.warning("Google revoke failed (%s)", type(exc).__name__)
            if not revoked:
                log.warning("Google did not confirm the revoke; the token is deleted here anyway")
        await self.app.secrets.delete(NAMESPACE, TOKEN_KEY)
        switches = {name: False for name in SWITCHES}
        await self._save(status="disconnected", scopes=[], notified=False, last_error="", switches=switches, connected_at=None)
        self._reconnect_sent = False
        self.agenda = None
        await self.app.store.record_owner_event("Disconnected Google" + ("" if revoked else " (Google did not confirm the revoke)"))
        return revoked

    # ---- tokens -------------------------------------------------------------------------------------------------

    async def _token_call(self, data: dict[str, str]) -> dict[str, Any]:
        """One call to Google's token endpoint with the backoff every Google call gets. Never logs the form."""
        response = await self._send("POST", TOKEN_URL, data=data, authorized=False)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.is_success:
            return body
        code = body.get("error") if isinstance(body.get("error"), str) else ""
        if code == "invalid_grant" and data.get("grant_type") == "refresh_token":
            await self._revoked()
            raise GoogleError("Google access expired or was revoked. Reconnect it in Settings.")
        reason = code if _ERROR_CODE.fullmatch(code) else f"status {response.status_code}"
        raise GoogleError(f"Google refused the request ({reason}).")

    async def access_token(self, *, force: bool = False) -> str:
        state = await self.state()
        if state["status"] != "connected":
            raise GoogleError("Google is not connected.")
        tokens = await self.app.secrets.get(NAMESPACE, TOKEN_KEY)
        if not isinstance(tokens, dict) or not isinstance(tokens.get("refresh_token"), str):
            raise GoogleError("Google is not connected.")
        access = tokens.get("access_token")
        if not force and isinstance(access, str) and access and float(tokens.get("expires_at") or 0) - self.wall() > REFRESH_MARGIN:
            return access
        if self._refreshing is None:  # single flight: callers share one refresh and nothing is locked across it
            self._refreshing = asyncio.create_task(self._refresh(tokens["refresh_token"]))
            self._refreshing.add_done_callback(self._refresh_done)
        return await asyncio.shield(self._refreshing)

    def _refresh_done(self, task: asyncio.Task) -> None:
        self._refreshing = None
        if not task.cancelled():
            task.exception()  # retrieved here: each waiter sees it, nobody leaves it unretrieved

    async def _refresh(self, refresh_token: str) -> str:
        credentials = self.client_credentials()
        if credentials is None:
            raise GoogleError("Google is not configured.")
        data = await self._token_call({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": credentials[0], "client_secret": credentials[1],
        })
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise GoogleError("Google returned no access token.")
        rotated = data.get("refresh_token") if isinstance(data.get("refresh_token"), str) and data.get("refresh_token") else refresh_token
        await self.app.secrets.put(NAMESPACE, TOKEN_KEY, {
            "refresh_token": rotated, "access_token": access, "expires_at": self.wall() + _lifetime(data),
        })
        return access

    async def _revoked(self) -> None:
        """The refresh token is dead: drop it, mark the connection and tell the owner once."""
        if self._reconnect_sent:
            return
        self._reconnect_sent = True
        state = await self.state()
        await self.app.secrets.delete(NAMESPACE, TOKEN_KEY)
        already = state["status"] == "needs_reconnect" and state["notified"]
        await self._save(status="needs_reconnect", notified=True, last_error="Google access expired or was revoked.")
        self.agenda = None
        if already or self.app.owner_id is None:
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
        authorized: bool = True,
    ) -> httpx.Response:
        """One HTTP call, retried on 429, 5xx and network errors with backoff. A 401 refreshes the token once."""
        refreshed = False
        for attempt in range(MAX_TRIES):
            await self._throttle()
            headers = {"Authorization": f"Bearer {await self.access_token()}"} if authorized else {}
            try:
                response = await self.http.request(method, url, params=params, json=body, data=data, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("Google %s %s failed (%s)", method, urlsplit(url).path, type(exc).__name__)
                if attempt == MAX_TRIES - 1:
                    raise GoogleError("Google is not answering.") from None
                await self.app.sleep(min(2.0 ** attempt, BACKOFF_CAP))
                continue
            status = response.status_code
            if status == 401 and authorized and not refreshed:
                refreshed = True
                await self.access_token(force=True)
                continue
            if status == 429 or status >= 500:
                log.warning("Google %s %s answered %s", method, urlsplit(url).path, status)
                if attempt == MAX_TRIES - 1:
                    raise GoogleError("Google is busy. Try again later.")
                await self.app.sleep(min(_retry_after(response) or 2.0 ** attempt, BACKOFF_CAP))
                continue
            return response
        raise GoogleError("Google is not answering.")

    async def call(self, method: str, url: str, *, params: dict | None = None, body: Any = None) -> Any:
        """An authorized API call returning the parsed JSON (None for an empty answer)."""
        response = await self._send(method, url, params=params, body=body)
        if response.is_success:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                raise GoogleError("Google sent an answer Dom could not read.") from None
        log.warning("Google %s %s answered %s", method, urlsplit(url).path, response.status_code)
        if response.status_code == 403:
            raise GoogleError("Google refused: this permission was not granted. Reconnect Google and tick it.")
        if response.status_code == 404:
            raise GoogleError("Google could not find that.")
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


def _lifetime(data: dict[str, Any]) -> float:
    value = data.get("expires_in")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 3000.0


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
