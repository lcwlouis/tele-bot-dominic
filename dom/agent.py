"""The agent loop: call the model with tools until it returns the JSON actions object."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING

from pydantic import ValidationError

from dom import turn_log
from dom.actions import Actions
from dom.llm import ChatBudgetExhausted, ChatClient, FallbackClient
from dom.salvage import Salvaged
from dom.tools import Tool, TurnCtx
from dom.usage import ModelCallSkipped

if TYPE_CHECKING:
    from dom.escalation import EscalationTurn

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
# Names the chat model gives to media as if they were tools, with the final-answer field each means. Media goes out
# only as a field of the final JSON, so a call to one of these names is answered with where it belongs.
MEDIA_FIELDS = {
    "image": "image", "images": "image", "photo": "image", "photos": "image", "selfie": "image", "picture": "image",
    "sendphoto": "image", "sendimage": "image", "takephoto": "image", "takeselfie": "image", "generateimage": "image",
    "voice": "voice", "voicenote": "voice", "sendvoice": "voice", "speak": "voice", "tts": "voice",
    "videonote": "video_note", "video": "video_note", "sendvideonote": "video_note", "sendvideo": "video_note",
    "special": "video_note",
}
MEDIA_FIELD_SHAPES = {
    "image": '"image": "<scene>"',
    "voice": '"voice": "<spoken text>"',
    "video_note": '"video_note": {"text": "<spoken text>", "scene": "<where you are>"}',
}
MEDIA_NAMES = {"image": "photo", "voice": "voice note", "video_note": "video note"}
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
Rescue = Callable[[str | None], Awaitable[Salvaged | None]]  # plain prose in, bubbles out (dom.salvage), or None
ToolRunner = Callable[[dict, dict[str, Tool], TurnCtx], Awaitable[str]]


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


def malformed_shape(reply: dict | None) -> str:
    """Why a reply is not usable actions, by its shape only (never its text, which may quote private chats)."""
    if reply is None:
        return "no reply"
    if reply.get("tool_calls"):
        return "tool calls" if _well_formed(reply["tool_calls"]) else "malformed tool calls"
    text = unfence(reply.get("content"))
    if not text:
        return "empty"
    objects = [value for value in _objects(text) if isinstance(value, dict)]
    if not objects:
        return f"no JSON object in {len(text)} chars"
    last = objects[-1]
    try:
        Actions.model_validate(last)
    except ValidationError as error:
        problems = ", ".join(f"{'.'.join(map(str, e['loc']))}:{e['type']}" for e in error.errors()[:4])
        return f"invalid actions ({problems}) keys={sorted(map(str, last))[:8]} in {len(text)} chars"
    return f"{len(objects)} objects in {len(text)} chars"


def prompt_fingerprint(messages: list[dict], schemas: list[dict]) -> str:
    """Short hashes of the prompt parts in front of the chat window, which must stay the same from turn to turn for
    the home endpoint's prefix cache to hold. When a turn's first call is slow, the part whose hash moved is why."""

    def short(value: object) -> str:
        return turn_log.prompt_hash(value, digest_size=3)

    system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
    user = next((m.get("content") or "" for m in messages if m.get("role") == "user"), "")
    head, _, window = user.partition("<chat_window")
    first = re.search(r'<msg id="([^"]*)"', window)
    return (
        f"tools={short(schemas)} system={short(system)} head={short(head)} "
        f"window_from={first.group(1) if first else '-'}"
    )


def _well_formed(calls: object) -> bool:
    return isinstance(calls, list) and all(
        isinstance(c, dict) and isinstance(c.get("function"), dict) and isinstance(c["function"].get("name"), str)
        for c in calls
    )


def unavailable_tool_result(name: str) -> str:
    """What the model is told for a call to a tool that is not offered. A media action named as a tool is told where
    it belongs, since the model otherwise concludes the camera is broken."""
    field = MEDIA_FIELDS.get(re.sub(r"[^a-z]", "", name.lower()))
    if field is None:
        return "tool not available here"
    return (
        f'"{name}" is not a tool. Media are fields of your final JSON answer, not tools: if it is on offer this turn, '
        f"add {MEDIA_FIELD_SHAPES[field]} to that answer and it will be sent; if it is not on offer, say so in your own words."
    )


async def execute_tool_call(call: dict, offered: dict[str, Tool], ctx: TurnCtx) -> str:
    function = call["function"]
    tool = offered.get(function.get("name", ""))
    if tool is None:
        return unavailable_tool_result(str(function.get("name", "")))
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


def media_promise_request(unkept: set[str], media_offered: frozenset[str]) -> str:
    """What the model is told when a bubble promises media that its fields do not carry."""
    now = sorted(unkept & media_offered)
    never = sorted(unkept - media_offered)
    parts = ["Your reply promises media that it does not carry, so nothing would be sent."]
    if now:
        shapes = " or ".join(MEDIA_FIELD_SHAPES[field] for field in now)
        parts.append(f"To send the {' and '.join(MEDIA_NAMES[f] for f in now)}, add {shapes} to your answer; otherwise take the promise out of your bubbles.")
    if never:
        parts.append(f"You can't send a {' or '.join(MEDIA_NAMES[f] for f in never)} right now, so take that promise out of your bubbles.")
    return " ".join(parts) + " Do not call tools. Answer again with ONLY the JSON object described in the instructions, and nothing else."


async def run_turn(
    client: ChatClient | FallbackClient,
    messages: list[dict],
    tools: list[Tool],
    ctx: TurnCtx,
    max_steps: int = 6,
    rescue: Rescue | None = None,
    run_tool: ToolRunner = execute_tool_call,
    escalation: EscalationTurn | None = None,
    media_offered: frozenset[str] | None = None,
) -> Actions:
    """Run one turn. Dom never sends raw model text: only valid actions leave this function.

    With `media_offered` (the media fields this turn's note offers; None skips the check), a bubble that promises
    media its field does not carry is put back to the model once, to set the field or drop the promise.

    A reply that is not valid actions (or a turn that runs out of steps) is asked again once without
    tools, then put to the fallback model; if that fails too, NoFinalAnswer is raised so the caller can
    tell a lost reply from a chosen silence. Before that, plain prose that Jev judges a finished message is
    sent as it is (`rescue`), which saves the second model call."""
    offered = {t.name: t for t in tools if ctx.scope in t.scopes}
    schemas = [t.openai_schema() for t in offered.values()]
    log.info("prompt chat=%s %s", ctx.chat_id, prompt_fingerprint(messages, schemas))
    messages = list(messages)
    trace: list[str] = []  # one entry per model call, logged once per turn

    async def complete(prompt: list[dict], tools: list[dict], *, on_fallback: bool = False) -> dict:
        if escalation is not None:
            return await escalation.complete(client, prompt, tools, on_fallback=on_fallback)
        if on_fallback and isinstance(client, FallbackClient):
            return await client.complete_on_fallback(prompt, tools)
        return await client.complete(prompt, tools)

    def succeed(actions: Actions) -> Actions:
        trace.append("final")
        turn_log.step("final")
        log.info("turn chat=%s scope=%s calls=%d [%s]", ctx.chat_id, ctx.scope, len(trace), " ".join(trace))
        return actions

    async def settle(actions: Actions, reply: dict | None) -> Actions:
        """The actions, after one nudge when a bubble promises media that was never set."""
        unkept = actions.unkept_media_promises() if media_offered is not None and not actions.no_response else set()
        if not unkept:
            return succeed(actions)
        trace.append("media promise")
        turn_log.step("media promise")
        log.info("media promised in chat %s without its field: %s", ctx.chat_id, ", ".join(sorted(unkept)))
        nudge = [*messages, {"role": "assistant", "content": (reply or {}).get("content") or ""},
                 {"role": "user", "content": media_promise_request(unkept, media_offered or frozenset())}]
        try:
            fixed = _final(await complete(nudge, []))
        except (ModelCallSkipped, ChatBudgetExhausted):
            raise  # a spend or chat cap stops the turn, as it does in every other round
        except Exception as exc:  # the promise is a lesser fault than losing the reply
            log.warning("media promise nudge failed in chat %s (%s)", ctx.chat_id, type(exc).__name__)
            fixed = None
        result = actions if fixed is None else fixed
        if result.unkept_media_promises():  # one repair only: a promise still standing is cut out, never sent
            turn_log.step("media promise cut")
            result = result.without_unkept_media_promises()
        return succeed(result)

    for _ in range(max_steps):
        reply = await complete(messages, schemas)
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
                result = await run_tool(call, offered, ctx)
                messages.append({"role": "tool", "tool_call_id": str(call.get("id") or ""), "content": result})
            # After tool results the model tends to answer in prose, so say the format again here.
            messages.append({"role": "user", "content": TOOLS_DONE})
            continue
        if not calls and (actions := parse_actions(reply.get("content"))) is not None:
            return await settle(actions, reply)
        if not calls and rescue is not None and (saved := await rescue(reply.get("content"))) is not None:
            trace.append("salvaged")
            turn_log.step("salvaged")
            log.info(
                "salvaged prose reply in chat %s: %d bubbles, jev confidence %.2f",
                ctx.chat_id, len(saved.actions.bubbles), saved.confidence,
            )
            return await settle(saved.actions, reply)
        trace.append("malformed")
        if escalation is not None:
            escalation.failed()
        turn_log.step("malformed")
        log.info("malformed reply in chat %s: %s", ctx.chat_id, malformed_shape(reply))
        settled = list(messages)
        if not calls:  # malformed tool calls are kept out of the history and handled like a malformed final
            messages.append(reply)
        break
    else:
        trace.append("max_steps")
        settled = list(messages)
        reply = None

    # Ask again without tools: with them offered the model may call one more instead of answering.
    # The shape of what was wrong (keys and error kinds, never text) lets the model fix that instead of repeating it.
    request = f"{CORRECTION} What was wrong: {malformed_shape(reply)}." if reply is not None else FINAL_REQUEST
    retry = await complete([*messages, {"role": "user", "content": request}], [])
    if (actions := _final(retry)) is not None:
        return await settle(actions, retry)
    trace.append("malformed")
    if escalation is not None:
        escalation.failed()
    turn_log.step("malformed")
    log.info("malformed retry in chat %s: %s", ctx.chat_id, malformed_shape(retry))

    has_fallback = isinstance(client, FallbackClient) and client.fallback is not None
    if has_fallback:
        turn_log.step("fallback")
        try:
            reply = await complete([*settled, {"role": "user", "content": FINAL_REQUEST}], [], on_fallback=has_fallback)
        except ModelCallSkipped:
            raise
        except Exception as exc:  # the last resort failing is reported below, not raised as a transport error
            trace.append("fallback:error")
            log.warning("fallback model could not answer in chat %s (%s)", ctx.chat_id, type(exc).__name__)
        else:
            if (actions := _final(reply)) is not None:
                trace.append("fallback")
                return await settle(actions, reply)
            trace.append("fallback:malformed")
            log.info("malformed fallback reply in chat %s: %s", ctx.chat_id, malformed_shape(reply))
    log.warning(
        "turn chat=%s scope=%s calls=%d [%s] -> no usable final answer", ctx.chat_id, ctx.scope, len(trace), " ".join(trace),
    )
    raise NoFinalAnswer(" ".join(trace))
