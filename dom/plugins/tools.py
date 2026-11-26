"""Expose approved plugin tools and the owner-only draft tool."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from dom.desktop.tools import untrusted
from dom import guard
from dom.guard import check_local_private_text
from dom.models import Scope
from dom.plugins.manifest import Plugin, ToolManifest
from dom.plugins.runner import MAX_OUTPUT, PluginError
from dom.skills import BODY_LIMIT
from dom.tools import Tool, TurnCtx
from dom.usage import ModelCallSkipped, check_model_allowed, model_request

log = logging.getLogger(__name__)
MAX_ARGUMENT_BYTES = 4 * 1024
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
        "Request a Python plugin draft for owner review. Describe what it should do, the agent tools, Telegram "
        "commands or dashboard panels it needs, and whether it needs owner-only offline storage or network access. "
        "Use only public information. Leave out private notes, chat history, memory and credentials."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,31}$"},
            "brief": {"type": "string", "maxLength": BODY_LIMIT},
        },
        "required": ["name", "brief"],
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


async def _held_by_leak_check(app: App, ctx: TurnCtx, plugin: Plugin, args: dict) -> bool:
    """Arguments to an online plugin leave the machine, so they get the check outgoing messages get."""
    if not plugin.manifest.network or app.jev is None or app.config.leak_cutoff is None:
        return False
    text = "\n".join(_argument_texts(args))

    async def allowed() -> bool:
        return ctx.model_allowed is None or await ctx.model_allowed()

    with model_request("guard", ctx.chat_id, allowed):
        await check_model_allowed()
        score = await guard.leak_score(app.jev, guard.private_terms(app.home), text)
    if score is None:
        log.warning("leak check unavailable for plugin %s; running", plugin.manifest.name)
        return False
    cutoff = app.config.leak_cutoff
    if cutoff is None or score < cutoff:
        return False
    await guard.notify_held(
        app, f"Held back a call to plugin {plugin.manifest.name} that looked like it leaked private info\n\n{text}",
    )
    return True


def _tool(app: App, plugin: Plugin, tool: ToolManifest) -> Tool:
    async def call(args: dict, ctx: TurnCtx) -> str:
        if ctx.scope not in tool.scopes:
            return "error: tool not available here"
        try:
            encoded = json.dumps(args, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return "error: plugin arguments must be JSON"
        if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            return "error: plugin arguments are limited to 4 KB"
        try:
            check_local_private_text(app.config, app.home, _argument_texts(args))
            try:
                if await _held_by_leak_check(app, ctx, plugin, args):
                    return "error: arguments held back because they looked like private information"
            except ModelCallSkipped:
                return "error: plugin call skipped"
            # The registry lock covers only the approval check. The runner pins the hash again and refuses
            # to queue behind another plugin, so a slow plugin never stalls approvals or scans.
            with app.plugin_runner.active(plugin.manifest.name):
                folder = await app.plugins.approved_folder(plugin)
                if folder is None:
                    return "error: plugin changed or is no longer approved"
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
