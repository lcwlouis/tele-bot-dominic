import json

import httpx
import pytest

from dom import anthropic_api, model_lists
from dom.config import ProviderEndpoint
from dom.home import Home
from dom.llm import ChatClient
from dom.prompt import build_messages
from dom.usage import token_counts


async def test_chat_client_speaks_native_anthropic_messages_and_tools():
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking that."},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "durian"}},
            ],
            "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 80},
        })

    ep = ProviderEndpoint(
        base_url="https://api.anthropic.com/v1", model="claude-fable-5-2",
        api_key_env="ANTHROPIC_API_KEY", reasoning_effort="medium",
    )
    messages = [
        {"role": "system", "content": "You are Dom."},
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "toolu_0", "type": "function", "function": {"name": "ping", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_0", "content": "pong"},
    ]
    tools = [{"type": "function", "function": {"name": "search", "description": "Web search", "parameters": {"type": "object"}}}]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ChatClient(ep, "sk-ant-test", http)
        reply = await client.complete(messages, tools, once=True)

    url, hdrs, body = seen[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert hdrs["x-api-key"] == "sk-ant-test"
    assert hdrs["anthropic-version"] == anthropic_api.ANTHROPIC_VERSION
    assert "authorization" not in hdrs
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 6144}
    assert body["max_tokens"] > 6144
    assert body["tools"][0]["name"] == "search" and "input_schema" in body["tools"][0]
    assert body["messages"][0]["content"][1]["source"]["type"] == "base64"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"

    assert reply["role"] == "assistant"
    assert reply["content"] == "Checking that."
    assert reply["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"q": "durian"}'}
    assert token_counts({"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 80}) == (120, 30, 80)


async def test_model_lists_fetch_uses_anthropic_headers_and_parses_display_name():
    seen_headers = {}

    def handler(request: httpx.Request):
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"data": [
            {
                "id": "claude-fable-5-2",
                "display_name": "Claude Fable 5.2",
                "capabilities": {"image_input": {"supported": True}},
            },
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        models = await model_lists.fetch(http, "https://api.anthropic.com/v1", "sk-ant-test", secrets=())
    assert seen_headers["x-api-key"] == "sk-ant-test"
    assert seen_headers["anthropic-version"] == anthropic_api.ANTHROPIC_VERSION
    assert models[0].id == "claude-fable-5-2"
    assert models[0].name == "Claude Fable 5.2"
    assert models[0].input == ("text", "image")


def test_chat_tagged_memory_md_lines_appear_in_their_chat_and_owner_dm_only(tmp_path):
    (tmp_path / "SOUL.md").write_text("---\nname: Dom\n---\nSoul.\n")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/MEMORY.md").write_text(
        "- Global fact\n- Durian secret {chat -1001}\n- Other secret {chat -2002}\n"
    )
    (tmp_path / "memory/HABITS.md").write_text("")
    home = Home(tmp_path)

    durian = build_messages(home, "group", -1001, [], "<chat_window/>", "note")
    other = build_messages(home, "group", -2002, [], "<chat_window/>", "note")
    owner = build_messages(home, "owner_dm", 111, [], "<chat_window/>", "note")

    # Shared system prefix stays identical across chats (cache-stable)
    assert durian[0] == other[0] == owner[0]
    assert "Durian secret" not in durian[0]["content"]
    # Per-chat user message surfaces the matching chat-tagged line
    assert "Durian secret" in durian[1]["content"] and "Other secret" not in durian[1]["content"]
    assert "Other secret" in other[1]["content"] and "Durian secret" not in other[1]["content"]
    assert "Durian secret" in owner[1]["content"] and "Other secret" in owner[1]["content"]
