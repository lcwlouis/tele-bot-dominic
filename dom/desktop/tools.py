"""Function schemas and sandbox tool implementations for the desktop worker."""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from dom.desktop.client import SandboxClient, url_host

from dom.desktop.files import MAX_RESULT_FILES
from dom.skills import BODY_LIMIT, DESCRIPTION_LIMIT, NAME_LIMIT

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
    "save_screenshot": ("Save a screenshot of the browser as a PNG in the sandbox and return its path, "
                        "so finish can send it to the owner.", {"name": _STRING}),
    "run_python": ("Run a Python script in the sandbox and return its output and exit code.", {"code": _STRING}),
    "run_shell": ("Run a shell command inside the sandbox.", {"command": _STRING}),
    "read_file": ("Read a text file inside the sandbox.", {"path": _STRING}),
    "write_file": ("Write a text file inside the sandbox.", {"path": _STRING, "content": _STRING}),
    "handoff": ("Pause for the owner at a login, captcha or payment.", {"reason": _STRING}),
    "skill_view": ("Read an owner-approved skill by name.", {"name": _STRING}),
    "propose_skill": ("Draft a reusable public skill once, after the work succeeds and before finish.", {
        "name": {"type": "string", "maxLength": NAME_LIMIT},
        "description": {"type": "string", "maxLength": DESCRIPTION_LIMIT},
        "body": {"type": "string", "maxLength": BODY_LIMIT},
    }),
    "finish": ("Finish with a concise result and optional sandbox files to send.", {
        "result": _STRING, "files": {"type": "array", "items": _STRING, "maxItems": MAX_RESULT_FILES},
    }),
}

_METHODS = {
    "browse": "navigate", "read_page": "markdown", "list_elements": "elements",
    "click": "click", "fill": "fill", "press": "press_key", "scroll": "scroll",
    "run_python": "run_python", "run_shell": "bash", "read_file": "read_file", "write_file": "write_file",
}


NAMES = tuple(_TOOLS)
BROWSER = ("browse", "read_page", "list_elements", "click", "fill", "press", "scroll", "look", "save_screenshot")
# What a self-directed task (W13) may plan to use. No handoff (no logins), no skills, no skill proposals.
OWN = (*BROWSER, "run_python", "run_shell", "read_file", "write_file")
SEARCH_TEXT_LIMIT = 120
_BLANK = ("about:blank", "chrome://newtab/", "chrome://new-tab-page/")
_SITE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
_PRIVATE_SUFFIXES = (
    ".local", ".lan", ".home", ".internal", ".intranet", ".corp", ".arpa", ".localhost", ".test", ".invalid",
)
_LOGIN_WORDS = ("pass", "login", "user", "email", "card", "otp")


def site(value: object) -> str | None:
    """A planned site as a lowercase public hostname, or None. IP literals and LAN-style names are refused."""
    if not isinstance(value, str):
        return None
    host = value.strip().lower().removeprefix("www.").rstrip(".")
    if not _SITE.fullmatch(host) or host.endswith(_PRIVATE_SUFFIXES):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


def on_plan(host: str | None, sites: tuple[str, ...]) -> bool:
    """Whether `host` is one of the plan's sites or a subdomain of one."""
    return bool(host) and any(host == entry or host.endswith("." + entry) for entry in sites)


def on_plan_url(url: object, sites: tuple[str, ...]) -> bool:
    return url in _BLANK or on_plan(url_host(url), sites)


def search_text(selector: str, text: str) -> bool:
    """A self-directed task may type only short search text, never into a login-looking field."""
    return len(text) <= SEARCH_TEXT_LIMIT and not any(word in selector.lower() for word in _LOGIN_WORDS)


def offline(command: str) -> str:
    """Run `command` in its own network namespace with no interfaces up: it has no network at all.
    If the sandbox cannot create the namespace, the command fails instead of running online."""
    return "unshare --net --map-root-user -- sh -c " + shlex.quote(command)


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
    if name == "browse" and not _web_url(args["url"]):
        raise ValueError("invalid tool arguments")
    return name, args


def _web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def untrusted(value: Any, *, limit: int = RESULT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    opening, closing = "<untrusted_tool_result>\n", "\n</untrusted_tool_result>"
    return opening + escape(text)[:limit - len(opening) - len(closing)] + closing


async def invoke(client: SandboxClient, name: str, args: dict) -> Any:
    result = await getattr(client, _METHODS[name])(**args)
    if name == "read_page" and isinstance(result, dict):
        return result.get("markdown", result)
    if name == "list_elements" and isinstance(result, list):
        return result[:80]
    if name == "read_file" and isinstance(result, dict):
        return result.get("content", result)
    return result
