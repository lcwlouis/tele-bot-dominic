"""Regression tests from the PR #4 review: cheap media hashing and no double-counted nested calls."""

from dom import turn_log
from dom.turn_log import TurnTrace


def test_media_call_does_not_hash_its_attachment():
    trace = TurnTrace()
    huge = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1000}}]}]
    with turn_log.track(trace):
        with turn_log.model_call("media-model", "media", huge, []):
            pass
        with turn_log.model_call("chat-model", "chat", [{"role": "user", "content": "hi"}], []):
            pass
    media, chat = trace.model_calls
    assert media.user_hash is None
    assert chat.user_hash


def test_guard_call_inside_a_timed_tool_is_listed_but_not_counted_twice():
    trace = TurnTrace()
    with turn_log.track(trace):
        with turn_log.timed("plugin_example"):
            with turn_log.model_call("jev", "guard", [{"role": "user", "content": "x"}], []):
                pass
        with turn_log.model_call("jev", "guard", [{"role": "user", "content": "y"}], []):
            pass
    names = [entry.split("@")[0] for entry in trace.timings]
    assert names == ["plugin_example", "guard"]  # the nested guard is inside the tool's time; the outer one counts
    assert [call.kind for call in trace.model_calls] == ["guard", "guard"]
