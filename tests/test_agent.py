import json

import httpx
import pytest
from pydantic import ValidationError

from dom.actions import Actions, Bubble
from dom.agent import run_turn
from dom.config import ModelEndpoint
from dom.home import Home
from dom.llm import ChatClient
from dom.models import StoredMessage
from dom.store import Store
from dom.tools import Tool, TurnCtx, core_tools

OWNER = 111
ENDPOINT = ModelEndpoint(base_url="https://llm.test/v1", model="test/model", api_key_env=None)
FINAL = {"no_response": False, "react": None, "bubbles": [{"text": "hey", "reply_to": None, "delete_after": None}]}


def reply(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Script:
    """Scripted OpenAI-compatible endpoint; records every request body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.last_headers = request.headers
        return self.responses.pop(0)


def make_client(script, sleeps=None, api_key="k"):
    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(script))
    return ChatClient(ENDPOINT, api_key, http, sleep=fake_sleep)


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path, owner_id=OWNER)
    home = Home(tmp_path)
    yield tmp_path, home, store
    store.close()


def ctx_for(env, scope="group", chat_id=-1009, sender=222):
    _, home, store = env
    return TurnCtx(scope=scope, chat_id=chat_id, sender_id=sender, home=home, store=store, owner_id=OWNER)


async def test_tool_call_then_final_actions(env):
    _, _, store = env
    await store.record_new_message(StoredMessage(
        chat_id="-1009", msg_id=1, text="banana bread", visibility_scope="chat:-1009",
        created_at="2026-11-18T10:00:00+00:00"))
    script = Script(
        reply(tool_calls=[call("search_history", {"query": "banana"})]),
        reply(content=json.dumps(FINAL)),
    )
    actions = await run_turn(make_client(script), [{"role": "user", "content": "hi"}], core_tools(), ctx_for(env))
    assert actions == Actions(bubbles=[Bubble(text="hey")])
    second = script.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"
    assert "banana bread" in second[-1]["content"]
    assert second[-2]["tool_calls"][0]["id"] == "c1"
    assert script.requests[0]["model"] == "test/model"
    assert script.last_headers["authorization"] == "Bearer k"


@pytest.mark.parametrize("scope", ["owner_dm", "friend_dm", "group"])
async def test_replies_cannot_write_notes(env, scope):
    root, _, _ = env
    assert {t.name for t in core_tools()} == {"search_history", "extend_online", "remind"}
    script = Script(
        reply(tool_calls=[call("memory", {"file": "MEMORY.md", "op": "add", "text": "pwned"})]),
        reply(content=json.dumps(FINAL)),
    )
    await run_turn(make_client(script), [], core_tools(), ctx_for(env, scope=scope, chat_id=OWNER, sender=OWNER))
    expected = {"search_history", "extend_online"}
    if scope == "owner_dm":
        expected.add("remind")
    assert {t["function"]["name"] for t in script.requests[0]["tools"]} == expected
    assert script.requests[1]["messages"][-1]["content"] == "tool not available here"
    assert not (root / "memory").exists() and not (root / "private").exists()


async def test_fenced_json_accepted(env):
    script = Script(reply(content="```json\n" + json.dumps(FINAL) + "\n```"))
    actions = await run_turn(make_client(script), [], core_tools(), ctx_for(env))
    assert actions.bubbles[0].text == "hey"


async def test_malformed_final_falls_back_to_no_response(env):
    script = Script(reply(content="sure thing, here is my answer!"), reply(content="still just prose"))
    actions = await run_turn(make_client(script), [{"role": "user", "content": "hi"}], core_tools(), ctx_for(env))
    assert actions == Actions(no_response=True)
    assert actions.bubbles == []
    assert len(script.requests) == 2
    correction = script.requests[1]["messages"][-1]
    assert correction["role"] == "user" and "JSON" in correction["content"]


async def test_malformed_then_valid_is_accepted(env):
    script = Script(reply(content="oops"), reply(content=json.dumps(FINAL)))
    actions = await run_turn(make_client(script), [], core_tools(), ctx_for(env))
    assert actions.bubbles[0].text == "hey"


async def test_invalid_actions_object_counts_as_malformed(env):
    bad = {"bubbles": [{"text": "x", "delete_after": 999}]}
    script = Script(reply(content=json.dumps(bad)), reply(content=json.dumps(bad)))
    assert await run_turn(make_client(script), [], core_tools(), ctx_for(env)) == Actions(no_response=True)


async def test_unoffered_tool_call_gets_error_result(env):
    seen = []

    async def handler(args, ctx):
        seen.append(args)
        return "ran"

    owner_only = Tool("desk", "owner only", {"type": "object", "properties": {}}, handler, {"owner_dm"})
    script = Script(reply(tool_calls=[call("desk", {})]), reply(content=json.dumps(FINAL)))
    await run_turn(make_client(script), [], [*core_tools(), owner_only], ctx_for(env, scope="group"))
    assert seen == []
    assert "desk" not in {t["function"]["name"] for t in script.requests[0]["tools"]}
    assert script.requests[1]["messages"][-1]["content"] == "tool not available here"


async def test_unknown_tool_and_bad_arguments_get_error_results(env):
    bad_args = {"id": "c2", "type": "function", "function": {"name": "search_history", "arguments": "{not json"}}
    script = Script(
        reply(tool_calls=[call("nope", {}, "c1"), bad_args]),
        reply(content=json.dumps(FINAL)),
    )
    await run_turn(make_client(script), [], core_tools(), ctx_for(env))
    tool_msgs = [m for m in script.requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert tool_msgs[0]["content"] == "tool not available here"
    assert "error" in tool_msgs[1]["content"].lower()


async def test_handler_exception_becomes_error_result(env):
    async def boom(args, ctx):
        raise RuntimeError("kaput")

    t = Tool("boom", "x", {"type": "object", "properties": {}}, boom, {"group"})
    script = Script(reply(tool_calls=[call("boom", {})]), reply(content=json.dumps(FINAL)))
    await run_turn(make_client(script), [], [t], ctx_for(env))
    assert "kaput" in script.requests[1]["messages"][-1]["content"]


async def test_max_steps_exhausted_returns_no_response(env):
    script = Script(*[reply(tool_calls=[call("search_history", {"query": "x"})]) for _ in range(3)])
    actions = await run_turn(make_client(script), [], core_tools(), ctx_for(env), max_steps=3)
    assert actions == Actions(no_response=True)
    assert len(script.requests) == 3


async def test_search_history_scoping(env):
    _, _, store = env

    async def add(chat_id, msg_id, text):
        vis = store.visibility_for(chat_id)
        await store.record_new_message(StoredMessage(
            chat_id=str(chat_id), msg_id=msg_id, text=text, visibility_scope=vis,
            created_at="2026-11-18T10:00:00+00:00"))

    await add(-1009, 1, "banana in the group")
    await add(222, 1, "banana in a friend dm")
    await add(OWNER, 1, "banana in owner dm")
    search = next(t for t in core_tools() if t.name == "search_history")

    async def texts(**kw):
        out = json.loads(await search.handler({"query": "banana"}, ctx_for(env, **kw)))
        return sorted(r["text"] for r in out)

    assert await texts(scope="group", chat_id=-1009) == ["banana in the group"]
    assert await texts(scope="friend_dm", chat_id=222) == ["banana in a friend dm"]
    assert len(await texts(scope="owner_dm", chat_id=OWNER, sender=OWNER)) == 3


async def test_retries_on_429(env):
    sleeps = []
    script = Script(httpx.Response(429), httpx.Response(503), reply(content=json.dumps(FINAL)))
    client = make_client(script, sleeps)
    msg = await client.complete([{"role": "user", "content": "x"}], [])
    assert msg["content"] == json.dumps(FINAL)
    assert sleeps == [1, 3]
    assert len(script.requests) == 3
    assert "tools" not in script.requests[0]


async def test_gives_up_after_two_retries(env):
    script = Script(httpx.Response(500), httpx.Response(500), httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await make_client(script, []).complete([], [])
    assert len(script.requests) == 3


async def test_client_error_is_not_retried(env):
    script = Script(httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await make_client(script, []).complete([], [])
    assert len(script.requests) == 1


def test_actions_validation_limits():
    Actions(bubbles=[Bubble(text="a")] * 6)
    with pytest.raises(ValidationError):
        Actions(bubbles=[Bubble(text="a")] * 7)
    with pytest.raises(ValidationError):
        Bubble(text="")
    with pytest.raises(ValidationError):
        Bubble(text="x" * 4001)
    Bubble(text="x" * 4000)
    for bad in (0.5, 31):
        with pytest.raises(ValidationError):
            Bubble(text="a", delete_after=bad)
    assert Bubble(text="a", delete_after=1).delete_after == 1
    assert Bubble(text="a", delete_after=30).delete_after == 30
    assert Actions().no_response is False and Actions().bubbles == []


async def _turn(env, *responses):
    _, _, store = env
    script = Script(*responses)
    actions = await run_turn(make_client(script), [{"role": "user", "content": "hi"}], core_tools(), ctx_for(env))
    return actions, script


@pytest.mark.parametrize("final", [
    {"answer": "x"},
    {},
    {"bubbles": [{"text": "hi"}], "extra": 1},
    {"bubbles": [{"text": "hi", "mood": "x"}]},
])
async def test_wrong_shaped_final_object_is_malformed(env, final):
    actions, script = await _turn(env, reply(json.dumps(final)), reply(json.dumps(final)))
    assert actions == Actions(no_response=True)
    assert len(script.requests) == 2  # one correction, then give up


async def test_partial_final_object_is_accepted(env):
    actions, script = await _turn(env, reply(json.dumps({"bubbles": [{"text": "hi"}]})))
    assert [b.text for b in actions.bubbles] == ["hi"]
    assert len(script.requests) == 1


@pytest.mark.parametrize("bad_call", [
    {"id": "c1", "type": "function", "function": None},
    {"id": "c1", "type": "function"},
    {"id": "c1", "type": "function", "function": {"arguments": "{}"}},
    "not even an object",
])
async def test_malformed_tool_calls_never_raise(env, bad_call):
    actions, script = await _turn(env, reply(tool_calls=[bad_call]), reply(json.dumps(FINAL)))
    assert actions.bubbles[0].text == "hey"
    assert len(script.requests) == 2
    assert all("tool_calls" not in m for m in script.requests[1]["messages"])  # bad call kept out of history


async def test_malformed_tool_calls_twice_fall_back_to_no_response(env):
    bad = {"id": "c1", "type": "function", "function": None}
    actions, _ = await _turn(env, reply(tool_calls=[bad]), reply(tool_calls=[bad]))
    assert actions == Actions(no_response=True)
