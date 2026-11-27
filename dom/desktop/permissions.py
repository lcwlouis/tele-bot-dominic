"""Desktop browser permissions (R4), saved logins (R5), and owner-DM approval cards (E100)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.desktop import tools as desktop_tools
from dom.desktop.sandbox import DEFAULT
from dom.notice import REVIEW_LABEL, link_button, send_notice
from dom.secrets import SECRET_VALUE_LIMIT, mask_username

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

DesktopAction = Literal[
    "visit_site", "fill_login", "download", "upload", "background_work",
    "calendar_create", "calendar_add", "calendar_move", "calendar_delete", "gmail_draft", "booking_confirm",
    "doc_create", "doc_append", "sheet_create", "sheet_append", "sheet_update",
]
PermissionPolicy = Literal["ask", "allow", "never"]
ApprovalDecision = Literal["once", "always", "reject"]

ACTIONS: tuple[DesktopAction, ...] = (
    "visit_site", "fill_login", "download", "upload", "background_work",
    "calendar_create", "calendar_add", "calendar_move", "calendar_delete", "gmail_draft", "booking_confirm",
    "doc_create", "doc_append", "sheet_create", "sheet_append", "sheet_update",
)
POLICIES: tuple[PermissionPolicy, ...] = ("ask", "allow", "never")
SITE_SCOPED_ACTIONS: frozenset[str] = frozenset(("visit_site", "fill_login", "download", "upload"))
# Actions with one global policy, so Allow always flips that policy. The Google ones (W4) are owner-DM tool calls.
GOOGLE_ACTIONS: frozenset[str] = frozenset((
    "calendar_create", "calendar_add", "calendar_move", "calendar_delete", "gmail_draft", "booking_confirm",
    "doc_create", "doc_append", "sheet_create", "sheet_append", "sheet_update",
))
# Making a new calendar is asked every time (Reject, Allow once): no Allow always, and Allow is not a policy for it.
ASK_EVERY_TIME: frozenset[str] = frozenset(("calendar_create",))
# A booking card offers Allow once and Reject only: a friend's request is never approved for good from a tap. The owner can
# still set the policy to Allow on the dashboard, which is the one way to book without a card.
NO_ALLOW_ALWAYS: frozenset[str] = frozenset(("booking_confirm",))
GLOBAL_ACTIONS: frozenset[str] = frozenset(("background_work", *(GOOGLE_ACTIONS - ASK_EVERY_TIME)))
APPROVAL_TIMEOUT = 900.0
CARD_LINES = 8  # lines on a Google approval card; the card never cuts a material detail, it goes dashboard-only instead
CARD_LINE_LIMIT = 200
DETAIL_LIMIT = 240
GOOGLE_DETAIL_LIMIT = 60_000  # a Google request keeps its complete details; past this it is not asked at all

DEFAULT_POLICIES: dict[DesktopAction, PermissionPolicy] = {
    "visit_site": "allow",
    "fill_login": "ask",
    "download": "ask",
    "upload": "ask",
    "background_work": "allow",
    "calendar_create": "ask",
    "calendar_add": "ask",
    "calendar_move": "ask",
    "calendar_delete": "ask",
    "gmail_draft": "ask",
    "doc_create": "ask",
    "doc_append": "ask",
    "sheet_create": "ask",
    "sheet_append": "ask",
    "sheet_update": "ask",
    "booking_confirm": "ask",
}

ACTION_META: dict[DesktopAction, dict[str, str]] = {
    "visit_site": {
        "label": "Visit unfamiliar sites",
        "description": "Opening a website that has no per-site rule yet during an owner-requested desktop task.",
    },
    "fill_login": {
        "label": "Fill saved logins",
        "description": "Filling a saved username and password into a matching HTTPS login form via VNC keystrokes.",
    },
    "download": {
        "label": "Download files from desktop",
        "description": "Pulling non-screenshot files out of the sandbox desktop and sending them to Telegram.",
    },
    "upload": {
        "label": "Upload files",
        "description": "Uploading a sandbox file into a website form (Never also blocks staging /desktop files into the sandbox).",
    },
    "background_work": {
        "label": "Background work",
        "description": "Dom's self-directed desktop research tasks (Own work). Ask requires your tap before each task starts.",
    },
    "calendar_create": {
        "label": "Create a Dom calendar",
        "description": "Dom makes a new calendar of his own in your Google account. It is always asked, once per calendar.",
    },
    "calendar_add": {
        "label": "Add a calendar event",
        "description": "Dom adds an event to one of his own calendars. The card shows the title, time and attendees.",
    },
    "calendar_move": {
        "label": "Move a calendar event",
        "description": "Dom changes the time of an event on one of his own calendars. The card shows the old and new time.",
    },
    "calendar_delete": {
        "label": "Delete a calendar event",
        "description": "Dom deletes an event from one of his own calendars. The card shows what will be removed.",
    },
    "booking_confirm": {
        "label": "Confirm a booking",
        "description": "A friend picked a time Dom offered inside your meeting windows. The card shows who, where, when and why; "
        "Allow once books it on Dom's booking calendar. Allow skips the card.",
    },
    "gmail_draft": {
        "label": "Save a Gmail draft",
        "description": "Dom saves a draft in Gmail. The card shows the recipients, subject and the start of the text. Dom never sends mail.",
    },
    "doc_create": {
        "label": "Create a Google Doc",
        "description": "Dom makes a new document in his own Drive folder. The card shows the name and the start of the text.",
    },
    "doc_append": {
        "label": "Add to a Google Doc",
        "description": "Dom adds text to the end of a document he made or one you handed him. The card shows the file name and the text.",
    },
    "sheet_create": {
        "label": "Create a Google Sheet",
        "description": "Dom makes a new spreadsheet in his own Drive folder. The card shows the name and the first rows.",
    },
    "sheet_append": {
        "label": "Add rows to a Google Sheet",
        "description": "Dom adds rows below the data of a spreadsheet he made or one you handed him. The card shows the file name and the rows.",
    },
    "sheet_update": {
        "label": "Replace cells in a Google Sheet",
        "description": "Dom overwrites cells in a spreadsheet he made or one you handed him. The card shows the file name, the cells and what was there before.",
    },
}

_DECISION_STATUS = {
    "once": "allowed_once",
    "always": "allowed_always",
    "reject": "rejected",
}
_SETTLED_LABELS = {
    "allowed_once": "✅ Allowed once",
    "allowed_always": "✅ Allowed always",
    "rejected": "❌ Rejected",
    "expired": "Expired",
    "cancelled": "Cancelled",
}


def normalize_site(raw: object) -> str | None:
    """Canonical public site hostname (lowercase, no www., no IP/LAN/public suffix), or None."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            return None
    return desktop_tools.site(text)


def host_matches_site(url: object, site: str, *, match_subdomains: bool = False) -> bool:
    """Strict HTTPS origin match (default port 443 only) for filling a saved login."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not parts.hostname:
        return False
    if port is not None and port != 443:
        return False
    if "@" in parts.netloc or "\\" in url:
        return False
    host = parts.hostname.strip().lower().rstrip(".")
    canonical = site.strip().lower().removeprefix("www.").rstrip(".")
    if not canonical:
        return False
    if host == canonical or host == f"www.{canonical}":
        return True
    if match_subdomains:
        # Never allow subdomain matching against a shared public suffix or invalid site.
        if normalize_site(canonical) is None:
            return False
        if host.endswith("." + canonical):
            return True
    return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Request:
    id: int
    token: str
    future: asyncio.Future[str]


def _detail_limit(action: str) -> int:
    return GOOGLE_DETAIL_LIMIT if action in GOOGLE_ACTIONS else DETAIL_LIMIT


class DesktopPermissions:
    """R4 permission engine + R5 saved-login manager + E100 owner-DM approval cards."""

    def __init__(self, app: App):
        self.app = app
        # request_id -> (token_hash, asyncio.Future[str])
        self._waiters: dict[int, tuple[str, asyncio.Future[str]]] = {}
        # Requests whose card could not show every material detail: only the dashboard may approve them.
        self._dashboard_only: set[int] = set()
        # Requests that belong to something a later change can take away (a calendar being removed): request id -> tag.
        self._tags: dict[int, str] = {}

    def _now(self) -> str:
        return self.app.dispatch.now().astimezone(timezone.utc).isoformat()

    def has_pending(self) -> bool:
        return any(not fut.done() for _, fut in self._waiters.values())

    # ---- Global & per-site policies -------------------------------------------------------------------------------

    async def get_policies(self) -> dict[str, str]:
        stored = await self.app.store.desktop_action_policies()
        policies = {
            action: stored[action] if stored.get(action) in POLICIES else DEFAULT_POLICIES[action]
            for action in ACTIONS
        }
        return {action: "ask" if action in ASK_EVERY_TIME and policy == "allow" else policy for action, policy in policies.items()}

    async def set_policy(self, action: str, policy: str) -> dict[str, str]:
        if action not in ACTIONS:
            raise ValueError("unknown desktop action")
        if policy not in POLICIES:
            raise ValueError("policy must be ask, allow, or never")
        if action in ASK_EVERY_TIME and policy == "allow":
            raise ValueError("This one is always asked: choose Ask or Never.")
        await self.app.store.set_desktop_action_policy(action, policy, self._now())
        await self.app.store.record_owner_event(f"Set desktop permission '{action}' to {policy}")
        return await self.get_policies()

    async def list_site_rules(self) -> list[dict[str, Any]]:
        return await self.app.store.list_desktop_site_permissions()

    async def set_site_rule(self, site: str, action: str, policy: str | None) -> list[dict[str, Any]]:
        canonical = normalize_site(site)
        if canonical is None:
            raise ValueError("Enter a public site hostname like example.org.")
        if action not in SITE_SCOPED_ACTIONS:
            raise ValueError("Per-site rules apply to visit_site, fill_login, download, or upload.")
        if policy is None:
            await self.app.store.delete_desktop_site_permission(canonical, action)
            await self.app.store.record_owner_event(f"Removed site rule for {canonical} ({action})")
        else:
            if policy not in POLICIES:
                raise ValueError("policy must be ask, allow, or never")
            await self.app.store.set_desktop_site_permission(canonical, action, policy, self._now())
            await self.app.store.record_owner_event(f"Set site rule {canonical} ({action}) to {policy}")
        return await self.list_site_rules()

    async def clear_site_allows(self, action: str) -> int:
        if action not in SITE_SCOPED_ACTIONS:
            raise ValueError("unknown site-scoped action")
        removed = await self.app.store.clear_desktop_site_allows(action)
        if removed:
            await self.app.store.record_owner_event(f"Cleared {removed} per-site Allow rule(s) for {action}")
        return removed

    async def resolve(self, action: DesktopAction, site: str | None = None) -> PermissionPolicy:
        """Resolve effective policy for `(action, site)`: per-site rule overrides global default."""
        if action not in ACTIONS:
            raise ValueError("unknown desktop action")
        canonical = normalize_site(site) if site else None
        if canonical and action in SITE_SCOPED_ACTIONS:
            override = await self.app.store.get_desktop_site_permission(canonical, action)
            if override in POLICIES:
                return override  # type: ignore[return-value]
        policies = await self.get_policies()
        return policies[action]  # type: ignore[return-value]

    # ---- Saved logins (R5) ----------------------------------------------------------------------------------------

    async def list_logins(self, site: str | None = None) -> list[dict[str, Any]]:
        """List non-secret saved-login metadata only. Passwords are never returned; usernames are masked."""
        canonical = normalize_site(site) if site else None
        rows = await self.app.store.list_saved_logins(site=canonical)
        return [
            {
                "id": row["id"],
                "site": row["site"],
                "label": row["label"],
                "username_hint": row["username_masked"],
                "has_username": bool(row["has_username"]),
                "has_password": bool(row["has_password"]),
                "match_subdomains": bool(row["match_subdomains"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def get_login_meta(self, login_id: int) -> dict[str, Any] | None:
        row = await self.app.store.get_saved_login(login_id)
        if row is None:
            return None
        return {
            "id": row["id"],
            "site": row["site"],
            "label": row["label"],
            "username_hint": row["username_masked"],
            "has_username": bool(row["has_username"]),
            "has_password": bool(row["has_password"]),
            "match_subdomains": bool(row["match_subdomains"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def save_login(
        self,
        *,
        site: str,
        label: str = "",
        username: str = "",
        password: str,
        match_subdomains: bool = False,
        login_id: int | None = None,
    ) -> dict[str, Any]:
        """Create or update a saved login. Secrets are encrypted in SecretStore('login', str(id))."""
        canonical = normalize_site(site)
        if canonical is None:
            raise ValueError("Enter a public site hostname like example.org (no IPs, LAN names, or shared suffixes).")
        clean_label = " ".join((label or "").split()).strip()[:80]
        clean_user = (username or "").strip()
        if len(clean_user) > 320:
            raise ValueError("Username can be at most 320 characters.")
        if login_id is None:
            if not isinstance(password, str) or not password or len(password) > SECRET_VALUE_LIMIT:
                raise ValueError(f"Enter a password (1 to {SECRET_VALUE_LIMIT} characters).")
            now = self._now()
            masked = mask_username(clean_user)
            new_id = await self.app.store.create_saved_login(
                site=canonical,
                label=clean_label or canonical,
                username_masked=masked,
                has_username=bool(clean_user),
                has_password=True,
                match_subdomains=bool(match_subdomains),
                now=now,
            )
            try:
                await self.app.secrets.put("login", str(new_id), {"username": clean_user, "password": password})
            except Exception:
                await self.app.store.delete_saved_login(new_id)
                raise
            await self.app.store.record_owner_event(f"Saved desktop login #{new_id} for {canonical}")
            meta = await self.get_login_meta(new_id)
            assert meta is not None
            return meta

        existing_meta = await self.app.store.get_saved_login(login_id)
        if existing_meta is None:
            raise ValueError("Saved login not found.")
        existing_secret = await self.app.secrets.get("login", str(login_id))
        if not isinstance(existing_secret, dict):
            existing_secret = {}
        final_user = clean_user if clean_user else str(existing_secret.get("username") or "")
        if password:
            if len(password) > SECRET_VALUE_LIMIT:
                raise ValueError(f"Password can be at most {SECRET_VALUE_LIMIT} characters.")
            final_pass = password
        else:
            final_pass = str(existing_secret.get("password") or "")
            if not final_pass:
                raise ValueError("Password is required.")
        now = self._now()
        await self.app.secrets.put("login", str(login_id), {"username": final_user, "password": final_pass})
        await self.app.store.update_saved_login(
            login_id,
            site=canonical,
            label=clean_label or canonical,
            username_masked=mask_username(final_user),
            has_username=bool(final_user),
            has_password=True,
            match_subdomains=bool(match_subdomains),
            now=now,
        )
        await self.app.store.record_owner_event(f"Updated saved desktop login #{login_id} for {canonical}")
        meta = await self.get_login_meta(login_id)
        assert meta is not None
        return meta

    async def delete_login(self, login_id: int) -> bool:
        existing = await self.app.store.get_saved_login(login_id)
        if existing is None:
            return False
        await self.app.secrets.delete("login", str(login_id))
        deleted = await self.app.store.delete_saved_login(login_id)
        if deleted:
            await self.app.store.record_owner_event(f"Deleted saved desktop login #{login_id} ({existing['site']})")
        return deleted

    async def decrypt_login(self, login_id: int) -> tuple[dict[str, Any], str, str]:
        """Internal-only decryption for VNC login filling. Never return to models, APIs, or logs."""
        meta = await self.get_login_meta(login_id)
        if meta is None:
            raise ValueError("Unknown saved login.")
        payload = await self.app.secrets.get("login", str(login_id))
        if not isinstance(payload, dict) or not isinstance(payload.get("password"), str) or not payload["password"]:
            raise ValueError("Saved login secret could not be loaded.")
        username = payload.get("username") if isinstance(payload.get("username"), str) else ""
        return meta, username, payload["password"]

    # ---- Approval cards & history (R4, E100) ----------------------------------------------------------------------

    async def record_decision(
        self,
        *,
        action: DesktopAction,
        site: str,
        detail: str,
        status: str,
        decided_via: str,
        task_id: int | None = None,
        self_task_id: int | None = None,
    ) -> int:
        return await self.app.store.record_desktop_permission_event(
            action=action,
            site=site,
            detail=detail[:_detail_limit(action)],
            status=status,
            decided_via=decided_via,
            at=self._now(),
            task_id=task_id,
            self_task_id=self_task_id,
        )

    async def open_request(
        self, *, action: DesktopAction, site: str, detail: str, task_id: int | None = None,
        self_task_id: int | None = None, dashboard_only: bool = False,
    ) -> _Request:
        """Register an approval request: its row, its waiter and its dashboard-only flag. Nothing is sent yet. Callers
        that act later register first, so a cancel can never miss a request that is only scheduled."""
        limit = _detail_limit(action)
        if action in GOOGLE_ACTIONS and len(detail) > limit:
            raise ValueError("too many attendees or recipients to review here, so nothing was asked.")
        token = secrets.token_hex(10)
        token_hash = _hash_token(token)
        request_id = await self.app.store.create_desktop_permission_request(
            action=action, site=site, detail=detail[:limit], token_hash=token_hash, created_at=self._now(),
            task_id=task_id, self_task_id=self_task_id,
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = (token_hash, future)
        if dashboard_only:
            self._dashboard_only.add(request_id)
        return _Request(request_id, token, future)

    async def request_approval(
        self,
        *,
        action: DesktopAction,
        site: str,
        detail: str,
        task_id: int | None = None,
        self_task_id: int | None = None,
        container: str = DEFAULT,
        timeout_seconds: float = APPROVAL_TIMEOUT,
        lines: Sequence[str] = (),
        dashboard_only: bool = False,
    ) -> str:
        """Send an owner-DM approval card (Reject / Allow once / Allow always) and wait for the decision.

        Returns one of: 'allowed_once', 'allowed_always', 'rejected', 'expired', 'cancelled'.
        """
        app = self.app
        if app.owner_id is None:
            await self.record_decision(
                action=action, site=site, detail=detail, status="rejected", decided_via="policy",
                task_id=task_id, self_task_id=self_task_id,
            )
            return "rejected"
        request = await self.open_request(
            action=action, site=site, detail=detail, task_id=task_id, self_task_id=self_task_id,
            dashboard_only=dashboard_only,
        )
        return await self.ask(request, action, site, detail, container, lines, timeout_seconds, dashboard_only)

    async def ask(
        self, request: _Request, action: DesktopAction, site: str, detail: str, container: str,
        lines: Sequence[str], timeout_seconds: float, dashboard_only: bool,
    ) -> str:
        """Send the card of a registered request and wait for its decision."""
        app, request_id = self.app, request.id
        try:
            if request.future.done():  # cancelled between registering and here: no card for a request that is over
                return request.future.result()
            sent = await self._send_card(request_id, action, site, detail, request.token, container, lines, dashboard_only)
            if not sent:
                row = await app.store.decide_desktop_permission_request(request_id, "expired", "timeout", self._now())
                if row:
                    await self._settle_card(row.get("card_message_id"), "Expired")
                return "expired"
            if (current := await app.store.get_desktop_permission_request(request_id)) and current["status"] != "pending":
                # decided (cancelled) while the card was going out: settle the card that just arrived
                await self._settle_card(current.get("card_message_id"), _SETTLED_LABELS.get(current["status"], "Cancelled"))
            # The shield keeps a timeout from cancelling the future: a decision committed just before it still counts.
            return await asyncio.wait_for(asyncio.shield(request.future), timeout_seconds)
        except TimeoutError:
            return await self._expire_or_committed(request_id)
        except asyncio.CancelledError:
            row = await app.store.decide_desktop_permission_request(request_id, "expired", "timeout", self._now())
            if row:
                await self._settle_card(row.get("card_message_id"), "Expired")
            raise
        finally:
            self._waiters.pop(request_id, None)
            self._dashboard_only.discard(request_id)

    async def _expire_or_committed(self, request_id: int) -> str:
        """The wait ran out: expire the request, unless a decision committed first, which then stands and runs."""
        app = self.app
        row = await app.store.decide_desktop_permission_request(request_id, "expired", "timeout", self._now())
        if row:
            await self._settle_card(row.get("card_message_id"), "Expired")
            return "expired"
        committed = await app.store.get_desktop_permission_request(request_id)
        status = committed["status"] if committed else "expired"
        return status if status in ("allowed_once", "allowed_always", "rejected", "cancelled") else "expired"

    async def _send_card(
        self,
        request_id: int,
        action: DesktopAction,
        site: str,
        detail: str,
        token: str,
        container: str,
        lines: Sequence[str] = (),
        dashboard_only: bool = False,
    ) -> bool:
        app = self.app
        assert app.owner_id is not None
        meta = ACTION_META[action]
        where = "" if container == DEFAULT else f" ({container})"
        title = f"{meta['label']}{where}"
        shown = list(lines)
        if not shown:
            if site:
                shown.append(f"Site: {site}")
            if detail:
                shown.append(detail[:80])
        buttons = []
        if not dashboard_only:  # a card that cannot show everything can only be rejected here
            buttons.append(InlineKeyboardButton(text="Allow once", callback_data=f"dperm:once:{request_id}:{token}"))
            if action not in NO_ALLOW_ALWAYS and (action in GLOBAL_ACTIONS or normalize_site(site) is not None):
                buttons.append(InlineKeyboardButton(text="Allow always", callback_data=f"dperm:always:{request_id}:{token}"))
        buttons.append(InlineKeyboardButton(text="Reject", callback_data=f"dperm:reject:{request_id}:{token}"))
        try:
            sent = await send_notice(
                app, app.owner_id, title, shown,
                path="/app/waiting", label=REVIEW_LABEL,
                tag="Google approval" if action in GOOGLE_ACTIONS else "desktop permission",
                bullets=bool(lines),
                buttons=buttons,
                **({"max_lines": CARD_LINES, "line_limit": CARD_LINE_LIMIT} if lines else {}),
            )
            if isinstance(message_id := getattr(sent, "message_id", None), int):
                await app.store.set_desktop_permission_card(request_id, message_id)
                return True
            return False
        except Exception:
            log.exception("could not send desktop permission card #%s", request_id)
            return False

    async def _settle_card(self, message_id: int | None, label: str) -> None:
        app = self.app
        if not message_id or message_id < 0 or app.owner_id is None:  # no card, or one the old bot sent
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="noop")]])
        try:
            from dom.telegram.executor import retry_telegram

            await retry_telegram(
                lambda: app.bot.edit_message_reply_markup(
                    chat_id=app.owner_id, message_id=message_id, reply_markup=markup,
                ),
                sleep=app.sleep,
            )
        except TelegramAPIError:
            log.info("could not settle desktop permission card %s", message_id)

    async def decide(
        self,
        request_id: int,
        decision: ApprovalDecision,
        *,
        via: Literal["telegram", "dashboard"],
        token: str | None = None,
    ) -> tuple[bool, str]:
        """Apply owner decision to a pending desktop permission request."""
        if decision not in _DECISION_STATUS:
            return False, "invalid decision"
        row = await self.app.store.get_desktop_permission_request(request_id)
        if row is None or row["status"] != "pending":
            return False, "already handled"
        if via == "telegram":
            if not token or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
                return False, "invalid token"
        action = row["action"]
        site = row["site"]
        waiter = self._waiters.get(request_id)
        if decision == "always" and (action in ASK_EVERY_TIME or action in NO_ALLOW_ALWAYS):
            return False, "this one is always asked"
        if decision != "reject":
            if via == "telegram" and request_id in self._dashboard_only:
                return False, "approve this one on the dashboard"
            if action in GOOGLE_ACTIONS and (waiter is None or waiter[1].done()):
                # Nothing is waiting to run it (Dom restarted, or the wait ran out): an approval that cannot run is
                # never recorded, and no Allow always is saved for it.
                expired = await self.app.store.decide_desktop_permission_request(request_id, "expired", "timeout", self._now())
                if expired:
                    await self._settle_card(expired.get("card_message_id"), "Expired")
                return False, "expired"
        canonical = normalize_site(site) if action in SITE_SCOPED_ACTIONS else None
        downgraded_always = decision == "always" and action in SITE_SCOPED_ACTIONS and canonical is None
        status = "allowed_once" if downgraded_always else _DECISION_STATUS[decision]
        now = self._now()
        updated = await self.app.store.decide_desktop_permission_request(request_id, status, via, now)
        if updated is None:
            return False, "already handled"
        # The decision is committed: wake whatever waits on it now, before any further network work, so a timeout in
        # between can never drop a committed approval.
        waiter = self._waiters.get(request_id)
        if waiter is not None and not waiter[1].done():
            waiter[1].set_result(status)

        # A Google action saves its Allow always itself, once it has actually run.
        if decision == "always" and not downgraded_always and action not in GOOGLE_ACTIONS:
            if action in GLOBAL_ACTIONS:
                await self.app.store.set_desktop_action_policy(action, "allow", now)
            elif action in SITE_SCOPED_ACTIONS and canonical is not None:
                await self.app.store.set_desktop_site_permission(canonical, action, "allow", now)

        label = "✅ Allowed once (no site rule saved)" if downgraded_always else _SETTLED_LABELS[status]
        await self._settle_card(updated.get("card_message_id"), label)
        target_desc = f" on {site}" if site else ""
        await self.app.store.record_owner_event(
            f"Desktop permission #{request_id} ({action}{target_desc}): {status.replace('_', ' ')} via {via}"
        )
        return True, label

    async def run_governed(
        self,
        action: DesktopAction,
        *,
        detail: str,
        lines: Sequence[str],
        perform: Callable[[bool], Awaitable[str]],
        guard: Callable[[], Awaitable[str | None]],
        dashboard_only: bool = False,
        tag: str = "",
        declined: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """Run an owner-DM tool action under its policy and return what the tool tells the model.

        `perform(background)` does the work; `guard()` says why it may no longer run (a switch turned off, Google
        disconnected or connected to another account), and is checked immediately before it. Allow runs it now.
        Never refuses. Ask sends the approval card and returns at once: the wait for the tap happens in the
        background, so the reply never blocks on the owner. On approval it runs and the outcome is written to the
        owner's DM history, where Dom sees it on his next turn; a refusal, expiry, cancellation or failure is written
        there too, so nothing is lost silently. `declined()` is awaited when a card ends without the action running (rejected,
        expired, cancelled, or approved but no longer allowed), for an action somebody else is waiting on."""
        label = ACTION_META[action]["label"]
        policy = await self.resolve(action)
        if policy == "never":
            await self.record_decision(action=action, site="", detail=detail, status="blocked", decided_via="policy")
            return f"error: {label} is set to Never in the dashboard."
        if policy == "allow":
            if (problem := await guard()) is not None:
                return problem
            await self.record_decision(action=action, site="", detail=detail, status="auto_allowed", decided_via="policy")
            return await perform(False)
        if self.app.owner_id is None:
            return "error: there is no owner to ask."
        try:  # registered here, before anything is scheduled, so a cancel or a switch change always sees it
            request = await self.open_request(action=action, site="", detail=detail, dashboard_only=dashboard_only)
        except ValueError as exc:
            return f"error: {exc}"
        if tag:
            self._tags[request.id] = tag
        self.app.dispatch._spawn(self._ask_then_perform(request, action, detail, lines, perform, guard, dashboard_only, declined))
        return (
            f"Asked the owner to approve: {label.lower()}. Nothing has happened yet. "
            "You will see the outcome in the chat history; do not repeat the request."
        )

    async def _ask_then_perform(
        self, request: _Request, action: DesktopAction, detail: str, lines: Sequence[str],
        perform: Callable[[bool], Awaitable[str]], guard: Callable[[], Awaitable[str | None]], dashboard_only: bool,
        declined: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        store, label = self.app.store, ACTION_META[action]["label"]
        outcome = await self.ask(request, action, "", detail, DEFAULT, lines, APPROVAL_TIMEOUT, dashboard_only)
        self._tags.pop(request.id, None)
        if outcome not in ("allowed_once", "allowed_always"):
            said = {"expired": "timed out", "cancelled": "was cancelled"}.get(outcome, "was rejected")
            # The detail names things others wrote (an invite's title, a draft's subject): fenced as untrusted, as
            # desktop task text is, so the DM's history never reads it as Dom's own record.
            await store.record_owner_event(f"{label} {said}; nothing changed. " + desktop_tools.untrusted(detail, limit=400))
            await self._declined(declined, action)
            return
        if (problem := await guard()) is not None:  # the switch or the account changed while it waited
            await store.record_owner_event(f"{label} was approved but not run; nothing changed. {problem.removeprefix('error: ')}"[:400])
            await self._declined(declined, action)
            return
        try:
            result = await perform(True)
        except Exception:
            log.exception("approved %s failed", action)
            result = "error: it failed unexpectedly"
        failed = result.startswith(("error", "uncertain"))
        if outcome == "allowed_always" and not failed:
            await self.app.store.set_desktop_action_policy(action, "allow", self._now())
        await store.record_owner_event(f"{label} approved. " + desktop_tools.untrusted(result, limit=600))
        if failed:
            try:
                await send_notice(self.app, self.app.owner_id, f"{label}: not confirmed", [result.split(": ", 1)[-1]])
            except Exception:
                log.warning("could not tell the owner that %s failed", action)

    @staticmethod
    async def _declined(declined: Callable[[], Awaitable[None]] | None, action: str) -> None:
        if declined is None:
            return
        try:
            await declined()
        except Exception:
            log.exception("could not settle a declined %s", action)

    async def cancel_pending(self, actions: Iterable[str], *, tag: str | None = None) -> int:
        """Cancel the waiting requests of these actions (their switch went off, or the account changed): the waiter is
        woken first, then the card says Cancelled. With a `tag`, only the requests made under it."""
        wanted, count = set(actions), 0
        for row in await self.app.store.pending_desktop_permission_requests():
            if row["action"] not in wanted or (tag is not None and self._tags.get(row["id"]) != tag):
                continue
            done = await self.app.store.decide_desktop_permission_request(row["id"], "cancelled", "policy", self._now())
            if done is None:
                continue
            count += 1
            waiter = self._waiters.get(row["id"])
            if waiter is not None and not waiter[1].done():
                waiter[1].set_result("cancelled")
            await self._settle_card(done.get("card_message_id"), "Cancelled")
        return count

    async def pending_requests(self) -> list[dict[str, Any]]:
        rows = await self.app.store.pending_desktop_permission_requests()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "self_task_id": row["self_task_id"],
                "action": row["action"],
                "action_label": ACTION_META.get(row["action"], {}).get("label", row["action"]),
                "google": row["action"] in GOOGLE_ACTIONS,
                "site": row["site"],
                "detail": row["detail"],
                "created_at": row["created_at"],
                "can_allow_always": row["action"] in GLOBAL_ACTIONS or normalize_site(row["site"]) is not None,
                "dashboard_only": row["id"] in self._dashboard_only,
            }
            for row in rows
        ]

    async def history(self, *, action: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.app.store.list_desktop_permission_history(action=action, status=status, limit=limit)
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "self_task_id": row["self_task_id"],
                "action": row["action"],
                "action_label": ACTION_META.get(row["action"], {}).get("label", row["action"]),
                "google": row["action"] in GOOGLE_ACTIONS,
                "site": row["site"],
                "detail": row["detail"],
                "status": row["status"],
                "decided_via": row["decided_via"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
            }
            for row in rows
        ]

    async def expire_pending(self) -> int:
        expired = await self.app.store.expire_desktop_permission_requests(self._now())
        for row in expired:
            await self._settle_card(row.get("card_message_id"), "Expired")
            waiter = self._waiters.pop(row["id"], None)
            if waiter is not None and not waiter[1].done():
                waiter[1].set_result("expired")
        return len(expired)
