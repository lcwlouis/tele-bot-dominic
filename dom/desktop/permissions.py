"""Desktop browser permissions (R4), saved logins (R5), and owner-DM approval cards (E100)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
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

DesktopAction = Literal["visit_site", "fill_login", "download", "upload", "background_work"]
PermissionPolicy = Literal["ask", "allow", "never"]
ApprovalDecision = Literal["once", "always", "reject"]

ACTIONS: tuple[DesktopAction, ...] = ("visit_site", "fill_login", "download", "upload", "background_work")
POLICIES: tuple[PermissionPolicy, ...] = ("ask", "allow", "never")
SITE_SCOPED_ACTIONS: frozenset[str] = frozenset(("visit_site", "fill_login", "download", "upload"))

DEFAULT_POLICIES: dict[DesktopAction, PermissionPolicy] = {
    "visit_site": "allow",
    "fill_login": "ask",
    "download": "ask",
    "upload": "ask",
    "background_work": "allow",
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
    """Strict HTTPS origin match for filling a saved login."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not parts.hostname:
        return False
    if "@" in parts.netloc or "\\" in url:
        return False
    host = parts.hostname.strip().lower().rstrip(".")
    canonical = site.strip().lower().removeprefix("www.").rstrip(".")
    if not canonical:
        return False
    if host == canonical or host == f"www.{canonical}":
        return True
    if match_subdomains and host.endswith("." + canonical):
        return True
    return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DesktopPermissions:
    """R4 permission engine + R5 saved-login manager + E100 owner-DM approval cards."""

    def __init__(self, app: App):
        self.app = app
        # request_id -> (token_hash, asyncio.Future[str])
        self._waiters: dict[int, tuple[str, asyncio.Future[str]]] = {}

    def _now(self) -> str:
        return self.app.dispatch.now().astimezone(timezone.utc).isoformat()

    def has_pending(self) -> bool:
        return any(not fut.done() for _, fut in self._waiters.values())

    # ---- Global & per-site policies -------------------------------------------------------------------------------

    async def get_policies(self) -> dict[str, str]:
        stored = await self.app.store.desktop_action_policies()
        return {
            action: stored[action] if stored.get(action) in POLICIES else DEFAULT_POLICIES[action]
            for action in ACTIONS
        }

    async def set_policy(self, action: str, policy: str) -> dict[str, str]:
        if action not in ACTIONS:
            raise ValueError("unknown desktop action")
        if policy not in POLICIES:
            raise ValueError("policy must be ask, allow, or never")
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
            detail=detail[:240],
            status=status,
            decided_via=decided_via,
            at=self._now(),
            task_id=task_id,
            self_task_id=self_task_id,
        )

    async def request_approval(
        self,
        *,
        action: DesktopAction,
        site: str,
        detail: str,
        task_id: int | None = None,
        self_task_id: int | None = None,
        container: str = DEFAULT,
        timeout_seconds: float = 900.0,
    ) -> str:
        """Send an owner-DM approval card (Reject / Allow once / Allow always) and wait for the decision.

        Returns one of: 'allowed_once', 'allowed_always', 'rejected', 'expired'.
        """
        app = self.app
        if app.owner_id is None:
            await self.record_decision(
                action=action, site=site, detail=detail, status="rejected", decided_via="policy",
                task_id=task_id, self_task_id=self_task_id,
            )
            return "rejected"
        token = secrets.token_hex(10)
        token_hash = _hash_token(token)
        request_id = await app.store.create_desktop_permission_request(
            action=action,
            site=site,
            detail=detail[:240],
            token_hash=token_hash,
            created_at=self._now(),
            task_id=task_id,
            self_task_id=self_task_id,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._waiters[request_id] = (token_hash, future)
        try:
            sent = await self._send_card(request_id, action, site, detail, token, container)
            if not sent:
                row = await app.store.decide_desktop_permission_request(request_id, "expired", "timeout", self._now())
                if row:
                    await self._settle_card(row.get("card_message_id"), "Expired")
                return "expired"
            async with asyncio.timeout(timeout_seconds):
                return await future
        except (TimeoutError, asyncio.CancelledError) as exc:
            row = await app.store.decide_desktop_permission_request(request_id, "expired", "timeout", self._now())
            if row:
                await self._settle_card(row.get("card_message_id"), "Expired")
            if isinstance(exc, asyncio.CancelledError):
                raise
            return "expired"
        finally:
            self._waiters.pop(request_id, None)

    async def _send_card(
        self,
        request_id: int,
        action: DesktopAction,
        site: str,
        detail: str,
        token: str,
        container: str,
    ) -> bool:
        app = self.app
        assert app.owner_id is not None
        meta = ACTION_META[action]
        where = "" if container == DEFAULT else f" ({container})"
        title = f"{meta['label']}{where}"
        lines = []
        if site:
            lines.append(f"Site: {site}")
        if detail:
            lines.append(detail[:80])
        buttons = [
            InlineKeyboardButton(text="Allow once", callback_data=f"dperm:once:{request_id}:{token}"),
            InlineKeyboardButton(text="Allow always", callback_data=f"dperm:always:{request_id}:{token}"),
            InlineKeyboardButton(text="Reject", callback_data=f"dperm:reject:{request_id}:{token}"),
        ]
        try:
            sent = await send_notice(
                app, app.owner_id, title, lines,
                path="/app/waiting", label=REVIEW_LABEL,
                tag="desktop permission",
                buttons=buttons,
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
        if not message_id or app.owner_id is None:
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
        status = _DECISION_STATUS[decision]
        now = self._now()
        updated = await self.app.store.decide_desktop_permission_request(request_id, status, via, now)
        if updated is None:
            return False, "already handled"

        action = updated["action"]
        site = updated["site"]
        if decision == "always":
            if action == "background_work":
                await self.app.store.set_desktop_action_policy("background_work", "allow", now)
            elif action in SITE_SCOPED_ACTIONS and (canonical := normalize_site(site)):
                await self.app.store.set_desktop_site_permission(canonical, action, "allow", now)

        label = _SETTLED_LABELS[status]
        await self._settle_card(updated.get("card_message_id"), label)
        waiter = self._waiters.get(request_id)
        if waiter is not None and not waiter[1].done():
            waiter[1].set_result(status)
        target_desc = f" on {site}" if site else ""
        await self.app.store.record_owner_event(
            f"Desktop permission #{request_id} ({action}{target_desc}): {status.replace('_', ' ')} via {via}"
        )
        return True, label

    async def pending_requests(self) -> list[dict[str, Any]]:
        rows = await self.app.store.pending_desktop_permission_requests()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "self_task_id": row["self_task_id"],
                "action": row["action"],
                "action_label": ACTION_META.get(row["action"], {}).get("label", row["action"]),
                "site": row["site"],
                "detail": row["detail"],
                "created_at": row["created_at"],
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
