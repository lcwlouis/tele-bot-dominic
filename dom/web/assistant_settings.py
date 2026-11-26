"""Assistant mode's settings (v0.8.5 W6): the schedule, and when each message was last sent or why not.

GET /api/assistant returns AssistantView: the switch, and for the morning plan and the evening wrap-up its local time
(empty: off) and the outcome of its last day (sent, waiting because Dom is asleep, held by a cap, nothing to say,
skipped for being too late, or failed). The switch itself is the `assistant` entry of /api/permissions.
POST /api/assistant/schedule takes AssistantSchedule (both times, with the GET revision), writes config.toml like every
other settings save and applies at once; it returns AssistantView. Owner only, same-origin JSON.

`set_enabled` is the one place the switch is written, for the dashboard's switch and the `/assistant` command alike.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dom.config import PLAN_TIME
from dom.web.model_settings import REVISION, Snapshot, _input, _save_now, _snapshot, save_values
from dom.web.shared import owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
ASSISTANT_PATH = "/api/assistant"
SCHEDULE_PATH = ASSISTANT_PATH + "/schedule"
JSON_POSTS = {SCHEDULE_PATH}
ENABLED = ("assistant", "enabled")


class LastView(BaseModel):
    day: str
    at: str
    state: str
    label: str


class MessageView(BaseModel):
    id: str
    time: str
    last: LastView | None


class AssistantView(BaseModel):
    revision: str
    timezone: str
    enabled: bool
    messages: list[MessageView]


class AssistantSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    morning: str
    evening: str

    @field_validator("morning", "evening")
    @classmethod
    def _a_time_or_nothing(cls, value: str) -> str:
        if value and not PLAN_TIME.fullmatch(value):
            raise ValueError("Enter a time such as 08:00, or leave it empty to turn that message off.")
        return value


async def _view(app: App, snapshot: Snapshot) -> AssistantView:
    return AssistantView(
        revision=snapshot.revision, timezone=app.config.timezone, **await app.assistant.view(),
    )


async def set_enabled(app: App, on: bool) -> None:
    """Turn assistant mode on or off in config.toml and the running config. Raises HTTPException when the file can't
    be read or written."""
    snapshot = await asyncio.to_thread(_snapshot, app.config.home)
    await _save_now(app, snapshot.revision, ENABLED, on)


@router.get(ASSISTANT_PATH)
async def read_assistant(request: Request) -> AssistantView:
    app = owner_app(request)
    return await _view(app, await asyncio.to_thread(_snapshot, app.config.home))


@router.post(SCHEDULE_PATH)
async def save_schedule(request: Request) -> AssistantView:
    """Save both times. The scheduler reads them from the running config on every tick, so a save applies at once."""
    app = owner_app(request)
    edit = await _input(request, AssistantSchedule)
    saved = await save_values(
        app, edit.revision, {("assistant", "morning"): edit.morning, ("assistant", "evening"): edit.evening},
        lambda config: {"assistant": config.assistant},
        "Could not save the schedule. Check config.toml and its permissions.",
    )
    return await _view(app, saved)
