"""JSON API for connecting Google Calendar and Gmail (v0.8.5 W4), under Settings → Services.

GET  /api/integrations/google             the card: credentials set or not (never their values), status, the four switches.
POST /api/integrations/google/connect     starts the OAuth flow and returns Google's consent URL; it also sets the
                                          short-lived binding cookie that ties the flow to this browser.
GET  /api/integrations/google/callback    where Google sends the browser back. It is a top-level cross-site navigation,
                                          so it carries no Origin, a `Sec-Fetch-Site: cross-site` and no bearer
                                          header. Behind Cloudflare Access the browser sends its Access cookie, so it
                                          needs an Access identity like any request; on a token-only dashboard the
                                          middleware lets this one GET path through without one. Either way it
                                          authenticates itself: the single-use, unexpired `state` must match the
                                          HttpOnly binding cookie that only an authenticated owner's connect request
                                          set. It is not a `fetch_site` guarded read.
POST /api/integrations/google/disconnect  revokes the token at Google and deletes it here.
POST /api/integrations/google/switch      turns one of the switches on or off.
GET  /api/integrations/google/calendars   Dom's own calendars (v0.8.6): name, colour and the number of upcoming events,
                                          read from Google on request so the card itself never waits on it.
POST /api/integrations/google/calendars/remove  deletes one of Dom's calendars at Google (the dashboard asked the owner
                                          to confirm first). Only a calendar Dom made can be named.
GET  /api/integrations/google/files       Dom's Drive folder and the files he may see, names only (v0.8.6 W4), read from
                                          Google on request.
POST /api/integrations/google/files/picked  the Picker page reports what the owner picked; Google is asked whether Dom
                                          can now see each file, and the owner's DM history is told about those he can.

Owner only. The POSTs are same-origin JSON, in JSON_POSTS like every other dashboard write.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from dom.google_files import confirm_picked, overview
from dom.google_tools import calendar_rows
from dom.google import BINDING_COOKIE, CALLBACK_PATH, SETTINGS_PATH, GoogleError, InvalidCallback
from dom.web.model_settings import _input
from dom.web.shared import owner_app

log = logging.getLogger("dom.web")
router = APIRouter()
PREFIX = "/api/integrations/google"
JSON_POSTS = {f"{PREFIX}/connect", f"{PREFIX}/disconnect", f"{PREFIX}/switch", f"{PREFIX}/calendars/remove", f"{PREFIX}/files/picked"}
SwitchId = Literal["calendar_read", "calendar_write", "gmail_read", "gmail_drafts", "drive_files", "contacts_read"]


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectInput(BaseModel):
    """`also`: the switches whose scopes this connect should ask for besides the base ones ("Reconnect to allow Files")."""

    model_config = ConfigDict(extra="forbid")
    also: list[SwitchId] = Field(default_factory=list, max_length=6)


class SwitchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: SwitchId
    on: bool


class PickedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=10)


class CalendarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)


@router.get(PREFIX)
async def read_google(request: Request) -> dict:
    return await owner_app(request).google.view()


@router.post(f"{PREFIX}/connect")
async def connect(request: Request):
    app = owner_app(request)
    body = await _input(request, ConnectInput)
    try:
        url, binding = app.google.begin(body.also)
    except GoogleError as exc:
        raise HTTPException(409, str(exc)) from None
    response = JSONResponse({"url": url})
    response.set_cookie(
        BINDING_COOKIE, binding, max_age=600, httponly=True, secure=True, samesite="lax", path=CALLBACK_PATH,
    )
    return response


@router.get(CALLBACK_PATH)
async def callback(request: Request):
    app = owner_app(request)
    # A navigation, not a script's request: refuse anything a page could fire with fetch, an image or a form.
    if request.headers.get("Sec-Fetch-Mode", "navigate") != "navigate":
        raise HTTPException(403, "Forbidden")
    query = request.query_params
    try:
        await app.google.finish(
            state=query.get("state"), binding=request.cookies.get(BINDING_COOKIE),
            code=query.get("code"), error=query.get("error"),
        )
    except InvalidCallback as exc:
        # Not a flow this server started for this browser (anyone can link the owner here): change nothing at all.
        log.warning("Google callback ignored: %s", exc)
        return RedirectResponse(f"{SETTINGS_PATH}?google=invalid", status_code=303)
    except GoogleError as exc:
        log.warning("Google connect failed: %s", exc)
        await app.google.record_failure(str(exc))
        outcome = "failed"
    except Exception:
        log.exception("Google connect failed unexpectedly")
        await app.google.record_failure("Connecting Google failed. Check Dom's log.")
        outcome = "failed"
    else:
        outcome = "connected"
    # The code and state are in this request's URL only; the redirect target carries neither.
    response = RedirectResponse(f"{SETTINGS_PATH}?google={outcome}", status_code=303)
    response.delete_cookie(BINDING_COOKIE, path=CALLBACK_PATH)
    return response


@router.post(f"{PREFIX}/disconnect")
async def disconnect(request: Request) -> dict:
    app = owner_app(request)
    await _input(request, Empty)
    revoked = await app.google.disconnect()
    return {"revoked": revoked, **await app.google.view()}


@router.post(f"{PREFIX}/switch")
async def switch(request: Request) -> dict:
    app = owner_app(request)
    body = await _input(request, SwitchInput)
    try:
        return await app.google.set_switch(body.id, body.on)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get(f"{PREFIX}/calendars")
async def read_calendars(request: Request) -> dict:
    g = owner_app(request).google
    return {"calendars": await calendar_rows(g) if (await g.view())["own_calendars_granted"] else []}


@router.post(f"{PREFIX}/calendars/remove")
async def remove_calendar(request: Request) -> dict:
    g = owner_app(request).google
    body = await _input(request, CalendarInput)
    try:
        await g.remove_calendar(body.id)
    except GoogleError as exc:
        raise HTTPException(409, str(exc)) from None
    return await g.view()


@router.get(f"{PREFIX}/files")
async def read_files(request: Request) -> dict:
    g = owner_app(request).google
    if not await g.granted("drive_files"):
        return {"folder": None, "files": [], "more": False}
    try:
        return await overview(g)
    except GoogleError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post(f"{PREFIX}/files/picked")
async def files_picked(request: Request) -> dict:
    g = owner_app(request).google
    body = await _input(request, PickedInput)
    try:
        return {"files": await confirm_picked(g, body.ids)}
    except GoogleError as exc:
        raise HTTPException(409, str(exc)) from None
