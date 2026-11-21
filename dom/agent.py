"""The agent loop: call the model with tools until it returns the JSON actions object."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

from pydantic import ValidationError

from dom import turn_log
from dom.actions import Actions
from dom.llm import ChatClient, FallbackClient
from dom.tools import Tool, TurnCtx
from dom.usage import ModelCallSkipped

CORRECTION = (
    "Your last reply was not the required JSON object. Do not call tools. Answer again with ONLY the "
    "JSON object described in the instructions, and nothing else."
)
TOOLS_DONE = (
    "(Reminder: when you are done with tools, answer with ONLY the JSON object described in the "
    "instructions, and nothing else.)"
)
FINAL_REQUEST = (
    "Do not call tools. Answer now with ONLY the JSON object described in the instructions, "
    "and nothing else."
)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


log = logging.getLogger("dom.agent")


class NoFinalAnswer(Exception):
    """No model produced a usable final answer, so the turn has no actions (not even a deliberate silence)."""


def unfence(text: str | None) -> str:
    """The model's reply without a surrounding ```json fence."""
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    return fenced.group(1) if fenced else text


def _actions(data: object) -> Actions | None:
    if isinstance(data, dict) and data:  # at least one of no_response / react / bubbles
        try:
            return Actions.model_validate(data)
        except ValidationError:
            pass
    return None


def _objects(text: str) -> Iterator[object]:
    """The top-level JSON values embedded in text, in order."""
    decoder = json.JSONDecoder()
    at = text.find("{")
    while at != -1:
        try:
            value, end = decoder.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)
            continue
        yield value
        at = text.find("{", end)


def parse_actions(text: str | None) -> Actions | None:
    """The actions in a reply: the whole reply as JSON, else the last valid actions object inside it
    (the model sometimes wraps its answer in prose, a stray fence or tags). Anything else is dropped."""
    text = unfence(text)
    try:
        return _actions(json.loads(text))
    except ValueError:
        pass
    found = None
    for value in _objects(text):
        found = _actions(value) or found
    return found


def _final(reply: dict) -> Actions | None:
    return None if reply.get("tool_calls") else parse_actions(reply.get("content"))


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
        with turn_log.timed(tool.name):
            result = await tool.handler(args, ctx)
    except Exception as exc:  # a failing tool is reported to the model, not fatal to the turn
        result = f"error: {exc}"
    if result.startswith("error:"):
        # The turn log keeps only the step; the journal says why, so a refused call is not mistaken for a done one.
        turn_log.step(f"{tool.name} refused")
        log.info("tool %s refused in chat %s: %s", tool.name, ctx.chat_id, result[6:].strip()[:200])
    return result


async def run_turn(
    client: ChatClient | FallbackClient,
    messages: list[dict],
    tools: list[Tool],
    ctx: TurnCtx,
    max_steps: int = 6,
) -> Actions:
    """Run one turn. Dom never sends raw model text: only valid actions leave this function.

    A reply that is not valid actions (or a turn that runs out of steps) is asked again once without
    tools, then put to the fallback model; if that fails too, NoFinalAnswer is raised so the caller can
    tell a lost reply from a chosen silence."""
    offered = {t.name: t for t in tools if ctx.scope in t.scopes}
    schemas = [t.openai_schema() for t in offered.values()]
    messages = list(messages)
    trace: list[str] = []  # one entry per model call, logged once per turn

    def succeed(actions: Actions) -> Actions:
        trace.append("final")
        turn_log.step("final")
        log.info("turn chat=%s scope=%s calls=%d [%s]", ctx.chat_id, ctx.scope, len(trace), " ".join(trace))
        return actions

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
            # After tool results the model tends to answer in prose, so say the format again here.
            messages.append({"role": "user", "content": TOOLS_DONE})
            continue
        if not calls and (actions := parse_actions(reply.get("content"))) is not None:
            return succeed(actions)
        trace.append("malformed")
        turn_log.step("malformed")
        settled = list(messages)
        if not calls:  # malformed tool calls are kept out of the history and handled like a malformed final
            messages.append(reply)
        break
    else:
        trace.append("max_steps")
        settled = list(messages)
        reply = None

    # Ask again without tools: with them offered the model may call one more instead of answering.
    request = CORRECTION if reply is not None else FINAL_REQUEST
    retry = await client.complete([*messages, {"role": "user", "content": request}], [])
    if (actions := _final(retry)) is not None:
        return succeed(actions)
    trace.append("malformed")
    turn_log.step("malformed")

    if isinstance(client, FallbackClient) and client.fallback is not None:
        turn_log.step("fallback")
        try:
            reply = await client.complete_on_fallback([*settled, {"role": "user", "content": FINAL_REQUEST}], [])
        except ModelCallSkipped:
            raise
        except Exception as exc:  # the last resort failing is reported below, not raised as a transport error
            trace.append("fallback:error")
            log.warning("fallback model could not answer in chat %s (%s)", ctx.chat_id, type(exc).__name__)
        else:
            if (actions := _final(reply)) is not None:
                trace.append("fallback")
                return succeed(actions)
            trace.append("fallback:malformed")
    log.warning(
        "turn chat=%s scope=%s calls=%d [%s] -> no usable final answer", ctx.chat_id, ctx.scope, len(trace), " ".join(trace),
    )
    raise NoFinalAnswer(" ".join(trace))
