"""Native Anthropic Messages API adapter (/v1/messages and /v1/models)."""

from __future__ import annotations

import json
from typing import Any

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 6144,
    "high": 12288,
    "xhigh": 16384,
    "max": 24576,
}


def anthropic_host(host: str | None) -> bool:
    if not host:
        return False
    clean = host.rstrip(".").lower()
    return clean == "api.anthropic.com" or clean.endswith(".anthropic.com")


def headers(api_key: str | None) -> dict[str, str]:
    out = {"anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        out["x-api-key"] = api_key
    return out


def _image_block(url: str) -> dict[str, Any] | None:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media_type = head[5:].partition(";")[0].strip().lower()
        if media_type and data:
            return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
        return None
    if url.startswith(("https://", "http://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            img = part.get("image_url")
            url = img.get("url") if isinstance(img, dict) else None
            if isinstance(url, str) and (block := _image_block(url)) is not None:
                blocks.append(block)
    return blocks


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                blocks.append({"type": "text", "text": part["text"]})
    for tc in message.get("tool_calls") or ():
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                parsed = {}
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or f"call_{len(blocks)}"),
            "name": name,
            "input": parsed,
        })
    return blocks


def _convert_tools(tools: list[dict]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        entry: dict[str, Any] = {
            "name": fn["name"],
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        if isinstance(fn.get("description"), str) and fn["description"]:
            entry["description"] = fn["description"]
        converted.append(entry)
    return converted


def build_request(
    model: str,
    wire_messages: list[dict],
    tools: list[dict],
    *,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Convert OpenAI-style messages and tools into an Anthropic `/v1/messages` request body."""
    system_blocks: list[dict[str, Any]] = []
    out_messages: list[dict[str, Any]] = []

    def append_turn(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out_messages and out_messages[-1]["role"] == role:
            out_messages[-1]["content"].extend(blocks)
        else:
            out_messages.append({"role": role, "content": list(blocks)})

    for msg in wire_messages:
        role = msg.get("role")
        if role == "system":
            text = msg.get("content")
            if isinstance(text, str) and text:
                system_blocks.append({"type": "text", "text": text})
        elif role == "user":
            append_turn("user", _user_blocks(msg.get("content")))
        elif role == "assistant":
            append_turn("assistant", _assistant_blocks(msg))
        elif role == "tool":
            content = msg.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            append_turn("user", [{
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or ""),
                "content": text,
            }])

    if system_blocks:
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

    budget = THINKING_BUDGETS.get(reasoning_effort) if reasoning_effort and reasoning_effort != "none" else None
    tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS
    if budget is not None and tokens <= budget:
        tokens = budget + max(1024, tokens)

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": tokens,
        "messages": out_messages or [{"role": "user", "content": [{"type": "text", "text": " "}]}],
    }
    if system_blocks:
        body["system"] = system_blocks
    if converted_tools := _convert_tools(tools):
        body["tools"] = converted_tools
    if budget is not None:
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return body


def parse_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic `/v1/messages` response into an OpenAI-style assistant message dict."""
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in payload.get("content") or ():
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "tool_use" and isinstance(block.get("name"), str):
            args = block.get("input")
            tool_calls.append({
                "id": str(block.get("id") or f"call_{len(tool_calls)}"),
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False),
                },
            })
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message
