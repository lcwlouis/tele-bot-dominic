"""Stdlib-only helper mounted as /plugin/_dom/__init__.py."""

import json
import sys

_tools = {}


def tool(function):
    _tools[function.__name__] = function
    return function


def run():
    try:
        request = json.loads(sys.stdin.readline())
        result = _tools[request["tool"]](**request["args"])
        if not isinstance(result, str):
            raise TypeError("The plugin tool must return text.")
        response = {"ok": True, "result": result}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
    print(json.dumps(response, ensure_ascii=True))
