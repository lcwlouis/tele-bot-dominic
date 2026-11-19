"""The agent loop: call the model with tools until it returns the JSON actions object."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from dom.actions import Actions
from dom.llm import ChatClient
from dom.tools import Tool, TurnCtx

CORRECTION = (
    "Your last reply was not the required JSON object. Answer again with ONLY the JSON object "
    "described in the instructions, and nothing else."
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_actions(text: str | None) -> Actions | None:
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return Actions.model_validate(data)
    except (ValueError, ValidationError):
        pass
    return None


async def _run_tool_call(call: dict, offered: dict[str, Tool], ctx: TurnCtx) -> str:
    function = call.get("function", {})
    tool = offered.get(function.get("name", ""))
    if tool is None:
        return "tool not available here"
    try:
        args = json.loads(function.get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        return await tool.handler(args, ctx)
    except Exception as exc:  # a failing tool is reported to the model, not fatal to the turn
        return f"error: {exc}"


async def run_turn(
    client: ChatClient,
    messages: list[dict],
    tools: list[Tool],
    ctx: TurnCtx,
    max_steps: int = 6,
) -> Actions:
    """Run one turn. Dom never sends raw model text: anything but valid actions becomes no_response."""
    offered = {t.name: t for t in tools if ctx.scope in t.scopes}
    schemas = [t.openai_schema() for t in offered.values()]
    messages = list(messages)
    corrected = False

    for _ in range(max_steps):
        reply = await client.complete(messages, schemas)
        messages.append(reply)
        calls = reply.get("tool_calls")
        if calls:
            for call in calls:
                result = await _run_tool_call(call, offered, ctx)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            continue
        actions = parse_actions(reply.get("content"))
        if actions is not None:
            return actions
        if corrected:
            break
        corrected = True
        messages.append({"role": "user", "content": CORRECTION})
    return Actions(no_response=True)
