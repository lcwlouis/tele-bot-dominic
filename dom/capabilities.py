"""Describe the capabilities available to Dom in the current chat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dom.models import Scope
from dom.tools import START_DESKTOP_TASK, core_tools

if TYPE_CHECKING:
    from dom.telegram.handlers import App


def describe(app: App, scope: Scope) -> str:
    offered = core_tools(app.search_client)
    if app.desktop_worker is not None and app.desktop_worker.available():
        offered.insert(0, START_DESKTOP_TASK)
    tools = "; ".join(
        f"{tool.name} ({tool.description.split('. ', 1)[0].rstrip('.')})"
        for tool in offered if scope in tool.scopes
    )
    media = ", photos, GIFs, video, voice" if app.media_describer is not None else ""
    desktop = "off"
    if scope == "owner_dm" and app.config.desktop_mode != "off":
        if app.sandbox is None:
            desktop = "unavailable (desktop is not configured)"
        else:
            status = app.sandbox.status()
            desktop = {
                "stopped": "available (starts on use)",
                "starting": "available (starting)",
                "error": f"unavailable ({app.sandbox.error})",
            }.get(status, status)
    rest = (
        f"\nunderstands: text{media}\ndesktop: {desktop}\n"
        "cannot see read receipts, see deleted messages, or read chats he isn't in"
    )
    return f"tools: {tools or 'none'}"[:600 - len(rest)] + rest
