"""The agent loop: call the model with tools until it returns the JSON actions object."""

from __future__ import annotations

import logging

import json
import re

from pydantic import ValidationError

from dom import turn_log
from dom.actions import Actions
from dom.llm import ChatClient
from dom.tools import Tool, TurnCtx

CORRECTION = (
    "Your last reply was not the required JSON object. Answer again with ONLY the JSON object "
    "described in the instructions, and nothing else."
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


log = logging.getLogger("dom.agent")

def unfence(text: str | None) -> str:
    """The model's reply without a surrounding ```json fence."""
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    return fenced.group(1) if fenced else text


def parse_actions(text: str | None) -> Actions | None:
    text = unfence(text)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data:  # at least one of no_response / react / bubbles
            return Actions.model_validate(data)
    except (ValueError, ValidationError):
        pass
    return None


def _well_formed(calls: object) -> bool:
    return isinstance(calls, list) and all(
        isinstance(c, dict) and isinstance(c.get("function"), dict) and isinstance(c["function"].get("name"), str)
        for c in calls
    )


async def _run_tool_call(call: dict, offered: dict[str, Tool], ctx: TurnCtx) -> str:
    function = call["function"]
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
    trace: list[str] = []  # one entry per model call, logged once per turn

    for _ in range(max_steps):
        reply = await client.complete(messages, schemas)
        calls = reply.get("tool_calls")
        if calls and _well_formed(calls):
            trace.append("tools:" + ",".join(str((c.get("function") or {}).get("name")) for c in calls))
            for name in dict.fromkeys(call["function"]["name"] for call in calls):
                if name in offered:
                    turn_log.step(name)
            if not any(call["function"]["name"] in offered for call in calls):
                turn_log.step("unavailable tool")
            messages.append(reply)
            for call in calls:
                result = await _run_tool_call(call, offered, ctx)
                messages.append({"role": "tool", "tool_call_id": str(call.get("id") or ""), "content": result})
            continue
        if not calls:
            messages.append(reply)
            actions = parse_actions(reply.get("content"))
            if actions is not None:
                trace.append("final")
                turn_log.step("final")
                log.info("turn chat=%s scope=%s calls=%d [%s]", ctx.chat_id, ctx.scope, len(trace), " ".join(trace))
                return actions
        trace.append("malformed")
        turn_log.step("malformed")
        # else: malformed tool calls are kept out of the history and handled like a malformed final
        if corrected:
            break
        corrected = True
        messages.append({"role": "user", "content": CORRECTION})
    log.info("turn chat=%s scope=%s calls=%d [%s] -> no_response", ctx.chat_id, ctx.scope, len(trace), " ".join(trace))
    return Actions(no_response=True)
