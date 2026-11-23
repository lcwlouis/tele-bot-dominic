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
    fleet = app.desktop_worker
    if fleet is None:
        return "unavailable (desktop is not configured)"
    main = fleet.lane(fleet.default).sandbox
    status = main.status()
    if status == "error":
        return f"unavailable ({main.error})"
    text = {"stopped": "available (starts on use)", "starting": "available (starting)"}.get(status, status)
    others = [f"{lane.name} {lane.status()}" for lane in fleet.containers() if lane.name != fleet.default]
    return text + (f"; other containers: {', '.join(others)}" if others else "")


def desktop_hosts(app: App) -> list[dict]:
    """Each desktop host with its limits and the containers it holds, for self_inspect."""
    fleet = app.desktop_worker
    if fleet is None:
        return []
    return [{
        "name": host.config.name, "mode": host.config.mode,
        "max_containers": host.config.max_containers, "min_free_memory_mb": host.config.memory_floor_mb,
        "containers": [
            {"name": lane.name, "state": lane.status(), **({"error": lane.sandbox.error} if lane.sandbox.error else {})}
            for lane in fleet.containers() if lane.host is host
        ],
    } for host in fleet.hosts]


def describe(app: App, scope: Scope, offered: list[Tool] | None = None) -> str:
    if offered is None:
        offered = core_tools(app.search_client)
        if app.desktop_worker is not None and app.desktop_worker.available():
            offered.append(START_DESKTOP_TASK)
    names = [tool.name for tool in offered if scope in tool.scopes]
    media = ", photos, GIFs, video, voice" if app.media_describer is not None else ""
    can_send = ""
    if app.studio is not None and app.studio.ready and app.config.studio.enabled:
        config = app.config.studio
        features = [
            label for label, active in (
                ("voice notes", config.voice),
                ("round videos", config.video_notes or (scope == "owner_dm" and config.special)),
                ("photos", config.images),
            ) if active
        ]
        if features:
            can_send = f"\ncan send: {', '.join(features)}"
    t3_status = ""
    if scope == "owner_dm":
        bridge = app.t3
        if bridge is None:
            t3_status = "\nT3 bridge: off"
        else:
            count = bridge.reachable if bridge.reachable is not None else "unknown"
            t3_status = f"\nT3 bridge: on, reachable machines: {count}"
    rest = (
        f"\nunderstands: text{media}\ndesktop: {desktop_status(app, scope)}{t3_status}{can_send}\n"
        "cannot see read receipts, see deleted messages, or read chats he isn't in"
    )
    return _tool_line(names, 600 - len(rest)) + rest


def _tool_line(names: list[str], budget: int) -> str:
    """`tools: a, b, c` within `budget` characters. Whole names only; the model has each tool's schema already."""
    if not names:
        return "tools: none"
    for shown in range(len(names), 0, -1):
        line = "tools: " + ", ".join(names[:shown]) + (f" (+{len(names) - shown} more)" if shown < len(names) else "")
        if len(line) <= budget:
            return line
    return f"tools: {len(names)} offered"
