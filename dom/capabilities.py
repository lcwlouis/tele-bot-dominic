"""Describe the capabilities available to Dom in the current chat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dom.models import Scope
from dom.tools import core_tools

if TYPE_CHECKING:
    from dom.telegram.handlers import App


def describe(app: App, scope: Scope) -> str:
    tools = "; ".join(
        f"{tool.name} ({tool.description.split('. ', 1)[0].rstrip('.')})"
        for tool in core_tools(app.search_client) if scope in tool.scopes
    )
    media = ", photos, GIFs, video, voice" if app.media_describer is not None else ""
    desktop = "off" if app.config.desktop_mode == "off" else "unavailable"
    rest = (
        f"\nunderstands: text{media}\ndesktop: {desktop}\n"
        "cannot see read receipts, see deleted messages, or read chats he isn't in"
    )
    return f"tools: {tools or 'none'}"[:600 - len(rest)] + rest
