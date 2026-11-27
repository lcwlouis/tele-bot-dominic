"""JSON API for saved logins (R5), desktop browser permissions & approvals history (R4), and E101 secret requests.

Every route sits behind the dashboard's auth middleware in dom/web/app.py. Every POST is in JSON_POSTS, requiring
same-origin Origin and a JSON body. Saved login passwords and E101 secret values are strictly write-only: GET
responses return only masked hints and non-secret metadata.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from dom.desktop.permissions import (
    ACTION_META,
    ACTIONS,
    ASK_EVERY_TIME,
    POLICIES,
    SITE_SCOPED_ACTIONS,
    ApprovalDecision,
    DesktopAction,
    PermissionPolicy,
)
from dom.secrets import SECRET_VALUE_LIMIT, SecretStoreError
from dom.web.model_settings import _input
from dom.web.shared import owner_app

router = APIRouter()
PREFIX = "/api/access"
SECRET_PREFIX = "/api/secrets/request"

JSON_POSTS = {
    f"{PREFIX}/logins/save",
    f"{PREFIX}/logins/delete",
    f"{PREFIX}/secrets/delete",
    f"{PREFIX}/rotate-key",
    f"{PREFIX}/policy",
    f"{PREFIX}/site-rule",
    f"{PREFIX}/site-rules/clear-allows",
    f"{PREFIX}/decide",
    f"{SECRET_PREFIX}/lookup",
    f"{SECRET_PREFIX}/submit",
    f"{SECRET_PREFIX}/cancel",
}

ID = Field(gt=0, lt=2**63)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveLoginInput(Strict):
    id: int | None = Field(default=None, gt=0, lt=2**63)
    site: str = Field(min_length=1, max_length=253)
    label: str = Field(default="", max_length=80)
    username: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=SECRET_VALUE_LIMIT)
    match_subdomains: bool = False


class DeleteLoginInput(Strict):
    id: int = ID


class DeleteSecretInput(Strict):
    key: str = Field(min_length=1, max_length=128)


class RotateKeyInput(Strict):
    pass


class PolicyInput(Strict):
    action: DesktopAction
    policy: PermissionPolicy


class SiteRuleInput(Strict):
    site: str = Field(min_length=1, max_length=253)
    action: Literal["visit_site", "fill_login", "download", "upload"]
    policy: PermissionPolicy | None = None


class ClearSiteAllowsInput(Strict):
    action: Literal["visit_site", "fill_login", "download", "upload"]


class DecidePermissionInput(Strict):
    id: int = ID
    decision: ApprovalDecision


class SecretLookupInput(Strict):
    token: str | None = Field(default=None, max_length=256)
    request_id: int | None = Field(default=None, gt=0, lt=2**63)


class SecretSubmitInput(Strict):
    token: str | None = Field(default=None, max_length=256)
    request_id: int | None = Field(default=None, gt=0, lt=2**63)
    value: str | None = Field(default=None, max_length=SECRET_VALUE_LIMIT)
    site: str | None = Field(default=None, max_length=253)
    username: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=SECRET_VALUE_LIMIT)


class SecretCancelInput(Strict):
    token: str | None = Field(default=None, max_length=256)
    request_id: int | None = Field(default=None, gt=0, lt=2**63)


async def _access_view(app, *, action: str = "", status: str = "") -> dict:
    perms = app.desktop_permissions
    policies = await perms.get_policies()
    site_rules = await perms.list_site_rules()
    allow_counts = {
        act: sum(1 for row in site_rules if row["action"] == act and row["policy"] == "allow")
        for act in ACTIONS
    }
    return {
        "key_status": await app.secrets.key_status(),
        "logins": await perms.list_logins(),
        "owner_secrets": await app.store.list_owner_secrets(),
        "actions": [
            {
                "id": act,
                "label": ACTION_META[act]["label"],
                "description": ACTION_META[act]["description"],
                "site_scoped": act in SITE_SCOPED_ACTIONS,
                "asks_every_time": act in ASK_EVERY_TIME,
                "policy": policies[act],
                "site_allow_count": allow_counts[act],
            }
            for act in ACTIONS
        ],
        "policies": policies,
        "site_rules": site_rules,
        "pending_permissions": await perms.pending_requests(),
        "history": await perms.history(action=action, status=status, limit=100),
        "pending_secret_requests": await app.secret_requests.pending_list(),
    }


@router.get(PREFIX)
async def read_access(
    request: Request,
    action: str = Query(default="", max_length=40),
    status: str = Query(default="", max_length=40),
) -> dict:
    app = owner_app(request)
    if action and action not in ACTIONS:
        action = ""
    return await _access_view(app, action=action, status=status)


@router.post(PREFIX + "/logins/save")
async def save_login(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SaveLoginInput)
    try:
        saved = await app.desktop_permissions.save_login(
            site=body.site,
            label=body.label,
            username=body.username,
            password=body.password,
            match_subdomains=body.match_subdomains,
            login_id=body.id,
        )
    except (ValueError, SecretStoreError) as exc:
        raise HTTPException(422, str(exc)) from None
    verb = "Updated" if body.id is not None else "Saved"
    return {"ok": True, "message": f"{verb} login for {saved['site']}.", **await _access_view(app)}


@router.post(PREFIX + "/logins/delete")
async def delete_login(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, DeleteLoginInput)
    deleted = await app.desktop_permissions.delete_login(body.id)
    if not deleted:
        raise HTTPException(404, "Saved login not found.")
    return {"ok": True, "message": "Deleted saved login.", **await _access_view(app)}


@router.post(PREFIX + "/secrets/delete")
async def delete_owner_secret(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, DeleteSecretInput)
    try:
        await app.secrets.delete("owner_secret", body.key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    deleted = await app.store.delete_owner_secret(body.key)
    if not deleted:
        raise HTTPException(404, "Secret not found.")
    await app.store.record_owner_event(f"Deleted stored secret '{body.key}'")
    return {"ok": True, "message": f"Deleted secret '{body.key}'.", **await _access_view(app)}


@router.post(PREFIX + "/rotate-key")
async def rotate_key(request: Request) -> dict:
    app = owner_app(request)
    await _input(request, RotateKeyInput)
    try:
        new_kid, count = await app.secrets.rotate_key()
    except (OSError, ValueError, SecretStoreError) as exc:
        raise HTTPException(409, str(exc)) from None
    await app.store.record_owner_event(f"Rotated DOM_MASTER_KEY ({new_kid}); re-encrypted {count} secret(s)")
    return {
        "ok": True,
        "message": f"Encryption key rotated ({new_kid}); re-encrypted {count} secret(s).",
        **await _access_view(app),
    }


@router.post(PREFIX + "/policy")
async def set_policy(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, PolicyInput)
    if body.policy not in POLICIES:
        raise HTTPException(422, "Invalid policy.")
    try:
        await app.desktop_permissions.set_policy(body.action, body.policy)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {
        "ok": True,
        "message": f"{ACTION_META[body.action]['label']}: {body.policy}.",
        **await _access_view(app),
    }


@router.post(PREFIX + "/site-rule")
async def set_site_rule(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SiteRuleInput)
    try:
        await app.desktop_permissions.set_site_rule(body.site, body.action, body.policy)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    msg = (
        f"Removed rule for {body.site}."
        if body.policy is None
        else f"Set {body.site} ({ACTION_META[body.action]['label']}) to {body.policy}."
    )
    return {"ok": True, "message": msg, **await _access_view(app)}


@router.post(PREFIX + "/site-rules/clear-allows")
async def clear_site_allows(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, ClearSiteAllowsInput)
    removed = await app.desktop_permissions.clear_site_allows(body.action)
    return {
        "ok": True,
        "message": f"Cleared {removed} per-site Allow rule(s).",
        **await _access_view(app),
    }


@router.post(PREFIX + "/decide")
async def decide_permission(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, DecidePermissionInput)
    ok, label = await app.desktop_permissions.decide(body.id, body.decision, via="dashboard")
    return {"ok": ok, "message": label, **await _access_view(app)}


# ---- E101: One-time secret entry form endpoints ------------------------------------------------------------------


@router.post(SECRET_PREFIX + "/lookup")
async def lookup_secret_request(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SecretLookupInput)
    if not body.token and body.request_id is None:
        raise HTTPException(422, "Provide a request token or id.")
    info = await app.secret_requests.lookup(token=body.token, request_id=body.request_id)
    if info is None:
        raise HTTPException(410, "This secret request has expired or was already used.")
    return {"ok": True, "request": info}


@router.post(SECRET_PREFIX + "/submit")
async def submit_secret_request(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SecretSubmitInput)
    if not body.token and body.request_id is None:
        raise HTTPException(422, "Provide a request token or id.")
    try:
        result = await app.secret_requests.submit(
            token=body.token,
            request_id=body.request_id,
            value=body.value,
            site=body.site,
            username=body.username,
            password=body.password,
        )
    except ValueError as exc:
        msg = str(exc)
        status_code = 410 if "expired" in msg or "already used" in msg else 422
        raise HTTPException(status_code, msg) from None
    except SecretStoreError as exc:
        raise HTTPException(500, str(exc)) from None
    return result


@router.post(SECRET_PREFIX + "/cancel")
async def cancel_secret_request(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SecretCancelInput)
    if not body.token and body.request_id is None:
        raise HTTPException(422, "Provide a request token or id.")
    cancelled = await app.secret_requests.cancel(token=body.token, request_id=body.request_id)
    return {"ok": cancelled, "message": "Cancelled." if cancelled else "Already handled."}
