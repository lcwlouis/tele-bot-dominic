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


def tool(function):
    _tools[function.__name__] = function
    return function


def command(function):
    _commands[function.__name__] = function
    return function


def panel(function):
    _panels[function.__name__] = function
    return function


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
    try:
        response = {"ok": True, "result": _call(json.loads(sys.stdin.readline()))}
        line = json.dumps(response, ensure_ascii=True, allow_nan=False)
    except Exception as error:
        line = json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True)
    print(line)
