"""Stdlib-only helper mounted as /plugin/_dom/__init__.py.

Decorate functions, then call run():
- tool: def name(**args) -> str, an agent tool; args match the tool's JSON schema.
- command: def name(text, scope) -> str, a slash command; text follows /name, scope is the chat kind.
- panel: def id() -> dict, a dashboard panel with only "title", "paragraphs", "items" and "table".
"""

import json
import sys

_tools = {}
_commands = {}
_panels = {}
_self_test = None
_storage_enabled = False
_storage_changed = False
_storage = None


def tool(function):
    _tools[function.__name__] = function
    return function


def command(function):
    _commands[function.__name__] = function
    return function


def panel(function):
    _panels[function.__name__] = function
    return function


def self_test(function):
    global _self_test
    _self_test = function
    return function


def load_storage():
    if not _storage_enabled:
        raise ValueError("Enable storage in plugin.toml first.")
    return _storage


def save_storage(value):
    global _storage, _storage_changed
    load_storage()
    _storage = value
    _storage_changed = True


def _call(request):
    if "command" in request:
        result = _commands[request["command"]](request["args"], request["scope"])
    elif "panel" in request:
        result = _panels[request["panel"]]()
        if not isinstance(result, dict):
            raise TypeError("The plugin panel must return a dict.")
        return result
    else:
        result = _tools[request["tool"]](**request["args"])
    if not isinstance(result, str):
        raise TypeError("The plugin must return text.")
    return result


def run():
    global _storage_enabled, _storage_changed, _storage
    stored = None
    try:
        request = json.loads(sys.stdin.readline())
        _storage_enabled = "storage" in request
        _storage = request.get("storage")
        _storage_changed = False
        if request.get("trial"):
            if _self_test is None:
                result = {"output": _call(request), "self_test": False}
            else:
                output = _self_test()
                if output is not None and not isinstance(output, str):
                    raise TypeError("The self-test must return text or None.")
                result = {"output": output, "self_test": True}
        else:
            result = _call(request)
        response = {"ok": True, "result": result}
        line = json.dumps(response, ensure_ascii=True, allow_nan=False)
        if _storage_changed:
            stored = json.dumps(_storage, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stored.encode("utf-8")
    except Exception as error:
        line = json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True)
        stored = None
    print(line)
    if stored is not None:
        print(stored)
