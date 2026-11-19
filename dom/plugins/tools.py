"""Expose approved plugin tools and the owner-only draft tool."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from dom.desktop.tools import untrusted
from dom.guard import check_local_private_text
from dom.models import Scope
from dom.plugins.manifest import MAX_BYTES, MAX_FILES, Plugin, ToolManifest
from dom.plugins.runner import MAX_OUTPUT, PluginError
from dom.tools import Tool, TurnCtx

log = logging.getLogger(__name__)
WRAPPED_OUTPUT_LIMIT = 5 * MAX_OUTPUT + 64

if TYPE_CHECKING:
    from dom.telegram.handlers import App


async def _draft(args: dict, ctx: TurnCtx) -> str:
    app = ctx.app
    if app is None or not ctx.is_owner_dm():
        return "error: plugins can only be drafted in the owner's DM"
    try:
        proposal_id = await app.proposals.create("plugin", "", args, "draft_plugin")
    except (OSError, ValueError) as error:
        return f"error: {error}"
    return json.dumps({"id": proposal_id})


DRAFT_PLUGIN_TOOL = Tool(
    name="draft_plugin",
    description=(
        "Draft a Python tool for owner review. Provide plugin.toml separately and include main.py in files. "
        "Use only public information. Never include private notes, chat history, memory, or credentials. "
        "The tool receives only explicitly supplied arguments and returns one JSON line."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,31}$"},
            "description": {"type": "string", "maxLength": 200},
            "files": {"type": "object", "maxProperties": MAX_FILES - 1, "additionalProperties": {"type": "string"}},
            "manifest_toml": {"type": "string", "maxLength": MAX_BYTES},
        },
        "required": ["name", "description", "files", "manifest_toml"],
    },
    handler=_draft,
    scopes={"owner_dm"},
)


def _argument_texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _argument_texts(item)]
    if isinstance(value, dict):
        return [*value.keys(), *(text for item in value.values() for text in _argument_texts(item))]
    return []


def _tool(app: App, plugin: Plugin, tool: ToolManifest) -> Tool:
    name = plugin.manifest.name

    async def call(args: dict, ctx: TurnCtx) -> str:
        if ctx.scope not in tool.scopes:
            return "error: tool not available here"
        try:
            check_local_private_text(app.config, app.home, _argument_texts(args))
            # The registry lock covers only the approval check. The runner pins the hash again and refuses
            # to queue behind another plugin, so a slow plugin never stalls approvals or scans.
            async with app.plugins.lock:
                if not any(p.manifest.name == name and p.hash == plugin.hash for p in await app.plugins._approved()):
                    return "error: plugin changed or is no longer approved"
                folder = app.home.scoped(allow_private=False)._regular_path(f"plugins/{name}")
            result = await asyncio.to_thread(
                app.plugin_runner.run, folder, tool.name, args, plugin.manifest, approved_hash=plugin.hash,
            )
        except PluginError as error:
            return untrusted(f"error: {error}", limit=WRAPPED_OUTPUT_LIMIT)
        except (OSError, ValueError, RuntimeError):
            return "error: plugin could not run with these arguments"
        return untrusted(result, limit=WRAPPED_OUTPUT_LIMIT)

    return Tool(plugin.tool_name(tool), tool.description, tool.parameters, call, set(tool.scopes))


async def offered(app: App, scope: Scope) -> list[Tool]:
    try:
        entries = await app.plugins.entries(scope)
        if not entries or not await asyncio.to_thread(app.plugin_runner.self_test):
            return []
        return [_tool(app, plugin, tool) for plugin, tool in entries]
    except Exception:
        # Plugins are optional; a failed scan or probe must not break the turn.
        log.exception("could not offer plugin tools")
        return []
