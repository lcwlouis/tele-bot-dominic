"""Describe the capabilities available to Dom in the current chat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dom.models import Scope
from dom.tools import START_DESKTOP_TASK, Tool, core_tools

if TYPE_CHECKING:
    from dom.telegram.handlers import App


def desktop_status(app: App, scope: Scope) -> str:
    if scope != "owner_dm" or app.config.desktop_mode == "off":
        return "off"
    if app.sandbox is None:
        return "unavailable (desktop is not configured)"
    status = app.sandbox.status()
    if status == "error":
        return f"unavailable ({app.sandbox.error})"
    return {"stopped": "available (starts on use)", "starting": "available (starting)"}.get(status, status)


def describe(app: App, scope: Scope, offered: list[Tool] | None = None) -> str:
    if offered is None:
        offered = core_tools(app.search_client)
        if app.desktop_worker is not None and app.desktop_worker.available():
            offered.insert(0, START_DESKTOP_TASK)
    tools = "; ".join(
        f"{tool.name} ({tool.description.split('. ', 1)[0].rstrip('.')})"
        for tool in offered if scope in tool.scopes
    )
    media = ", photos, GIFs, video, voice" if app.media_describer is not None else ""
    t3_status = ""
    if scope == "owner_dm":
        bridge = app.t3
        if bridge is None:
            t3_status = "\nT3 bridge: off"
        else:
            count = bridge.reachable if bridge.reachable is not None else "unknown"
            t3_status = f"\nT3 bridge: on, reachable machines: {count}"
    rest = (
        f"\nunderstands: text{media}\ndesktop: {desktop_status(app, scope)}{t3_status}\n"
        "cannot see read receipts, see deleted messages, or read chats he isn't in"
    )
    return f"tools: {tools or 'none'}"[:600 - len(rest)] + rest
