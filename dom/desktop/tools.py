"""Function schemas and sandbox tool implementations for the desktop worker."""

from __future__ import annotations

import json
from typing import Any
from xml.sax.saxutils import escape

from dom.desktop.client import SandboxClient

from dom.desktop.files import MAX_RESULT_FILES

RESULT_LIMIT = 6000
_STRING = {"type": "string"}
_TOOLS = {
    "browse": ("Navigate to a URL.", {"url": _STRING}),
    "read_page": ("Read the current page as Markdown.", {}),
    "list_elements": ("List interactive elements on the current page.", {}),
    "click": ("Click a selector or an element index.", {
        "selector_or_index": {"anyOf": [_STRING, {"type": "integer"}]},
    }),
    "fill": ("Fill a selector with text.", {"selector": _STRING, "text": _STRING}),
    "press": ("Press a browser key.", {"key": _STRING}),
    "scroll": ("Scroll the current page.", {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}}),
    "look": ("Describe a screenshot. Use only when the text tools cannot show what you need.", {}),
    "run_python": ("Execute Python in Jupyter.", {"code": _STRING}),
    "run_shell": ("Run a shell command inside the sandbox.", {"command": _STRING}),
    "read_file": ("Read a text file inside the sandbox.", {"path": _STRING}),
    "write_file": ("Write a text file inside the sandbox.", {"path": _STRING, "content": _STRING}),
    "handoff": ("Pause for the owner at a login, captcha or payment.", {"reason": _STRING}),
    "finish": ("Finish with a concise result and optional sandbox files to send.", {
        "result": _STRING, "files": {"type": "array", "items": _STRING, "maxItems": MAX_RESULT_FILES},
    }),
}

_METHODS = {
    "browse": "navigate", "read_page": "markdown", "list_elements": "elements",
    "click": "click", "fill": "fill", "press": "press_key", "scroll": "scroll",
    "run_python": "jupyter", "run_shell": "bash", "read_file": "read_file", "write_file": "write_file",
}


def schemas() -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": [key for key in properties if key != "files"],
                           "additionalProperties": False},
        },
    } for name, (description, properties) in _TOOLS.items()]


def arguments(function: dict) -> tuple[str, dict]:
    name = function["name"]
    if name not in _TOOLS:
        raise ValueError("unknown desktop tool")
    args = json.loads(function.get("arguments") or "{}")
    properties = _TOOLS[name][1]
    required = properties.keys() - {"files"}
    if not isinstance(args, dict) or not required <= args.keys() <= properties.keys():
        raise ValueError("invalid tool arguments")
    for key, value in args.items():
        if key == "files":
            if isinstance(value, list) and len(value) <= MAX_RESULT_FILES and all(isinstance(item, str) for item in value):
                continue
            raise ValueError("invalid files")
        if key == "selector_or_index" and type(value) is int and value >= 0:
            continue
        if not isinstance(value, str):
            raise ValueError("invalid tool arguments")
        if "enum" in properties[key] and value not in properties[key]["enum"]:
            raise ValueError("invalid tool arguments")
    return name, args


def untrusted(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    opening, closing = "<untrusted_tool_result>\n", "\n</untrusted_tool_result>"
    return opening + escape(text)[:RESULT_LIMIT - len(opening) - len(closing)] + closing


async def invoke(client: SandboxClient, name: str, args: dict) -> Any:
    result = await getattr(client, _METHODS[name])(**args)
    if name == "read_page" and isinstance(result, dict):
        return result.get("markdown", result)
    if name == "list_elements" and isinstance(result, list):
        return result[:80]
    if name == "read_file" and isinstance(result, dict):
        return result.get("content", result)
    return result
