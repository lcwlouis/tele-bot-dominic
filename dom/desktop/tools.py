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
# Public suffixes a planned site may not be, because unrelated owners share them (a plan naming one would allow
# every tenant). A small built-in list rather than a Public Suffix List dependency: a plan names a few sites, and
# anything missed here still faces the LAN guard. Country second levels (co.uk, com.au, ne.jp...) are matched by
# _CC_SECOND_LEVEL; the list holds the common multi-tenant hosting suffixes.
_SHARED_SUFFIXES = frozenset((
    "github.io", "gitlab.io", "pages.dev", "workers.dev", "r2.dev", "vercel.app", "netlify.app", "web.app",
    "firebaseapp.com", "herokuapp.com", "appspot.com", "blogspot.com", "wordpress.com", "tumblr.com",
    "substack.com", "medium.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com", "s3.amazonaws.com",
    "fly.dev", "onrender.com", "glitch.me", "replit.app", "repl.co", "surge.sh", "ngrok.io", "ngrok-free.app",
    "trycloudflare.com", "duckdns.org", "no-ip.org", "ddns.net", "dyndns.org", "myshopify.com", "wixsite.com",
    "weebly.com", "neocities.org", "codeberg.page", "translate.goog", "sourceforge.io", "readthedocs.io",
))
_CC_SECOND_LEVEL = re.compile(r"(co|com|net|org|gov|edu|ac|or|ne|go|gob|mil|nic|ltd|plc|sch)\.[a-z]{2}")
_LOGIN_WORDS = ("pass", "login", "user", "email", "card", "otp")
_SEARCH_FIELD = re.compile(r"search|\bq\b|query", re.IGNORECASE)
WEB_PORTS = (80, 443)
# Keys a self-directed task may press: scrolling and closing things. Enter only right after a search fill.
SAFE_KEYS = frozenset(("PageDown", "PageUp", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"))
SUBMIT_KEYS = frozenset(("Enter",))
# The only places a self-directed task may read or write files with read_file/write_file.
OWN_FILE_ROOTS = ("/tmp", "/home/gem/outbox")


def site(value: object) -> str | None:
    """A planned site as a lowercase public hostname, or None. IP literals, LAN-style names and public
    suffixes are refused."""
    if not isinstance(value, str):
        return None
    host = value.strip().lower().removeprefix("www.").rstrip(".")
    if not _SITE.fullmatch(host) or host.endswith(_PRIVATE_SUFFIXES):
        return None
    if host in _SHARED_SUFFIXES or _CC_SECOND_LEVEL.fullmatch(host):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


def own_host(url: object) -> str | None:
    """The host of a URL a self-directed task may open, or None. Stricter than urlsplit: Chrome reads some
    authorities differently (`http://a.example\\@b.example/` goes to a.example), so anything ambiguous is refused:
    backslashes, `@`, `%`, whitespace or control characters in the authority, a netloc other than exactly the
    host (and port), a scheme other than http or https, and any port but 80 or 443."""
    if not isinstance(url, str) or "\\" in url or any(ord(char) < 33 or ord(char) == 127 for char in url):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    netloc = parts.netloc.lower()
    if parts.scheme not in ("http", "https") or not host or any(char in netloc for char in "@%"):
        return None
    if netloc not in (host, f"{host}:{port}") or (port is not None and port not in WEB_PORTS):
        return None
    return url_host(url)


def on_plan(host: str | None, sites: tuple[str, ...]) -> bool:
    """Whether `host` is one of the plan's sites or a subdomain of one."""
    return bool(host) and any(host == entry or host.endswith("." + entry) for entry in sites)


def on_plan_url(url: object, sites: tuple[str, ...]) -> bool:
    return url in _BLANK or on_plan(own_host(url), sites)


def search_text(selector: str, text: str) -> bool:
    """A self-directed task may type only short text into a search field, never into a login-looking one."""
    lowered = selector.lower()
    return (
        len(text) <= SEARCH_TEXT_LIMIT and bool(_SEARCH_FIELD.search(selector))
        and not any(word in lowered for word in _LOGIN_WORDS)
    )


def link_selector(selector: str) -> bool:
    """Whether a CSS selector's last compound names a link (`a`, `a.title`, `nav > a[href]`)."""
    last = re.split(r"[\s>+~]+", selector.strip())[-1] if selector.strip() else ""
    return bool(re.fullmatch(r"a(?:[.#\[:].*)?", last))


def link_element(element: object) -> bool:
    """Whether an element from the browser's element list is plainly a link. Anything it cannot tell is not."""
    if not isinstance(element, dict):
        return False
    tag = next((element[key] for key in ("tag", "tag_name", "tagName") if isinstance(element.get(key), str)), "")
    role = element.get("role") if isinstance(element.get("role"), str) else ""
    kind = element.get("type") if isinstance(element.get("type"), str) else ""
    return (tag.lower() == "a" or role.lower() == "link") and kind.lower() not in ("submit", "button", "image", "reset")


def offline(command: str) -> str:
    """Run `command` in its own network namespace with no interfaces up: it has no network at all. Absolute
    paths, so no command found earlier in PATH stands in for them; /bin/sh -c reads no startup files. If the
    sandbox cannot create the namespace, the command fails instead of running online."""
    return "/usr/bin/unshare --net --map-root-user -- /bin/sh -c " + shlex.quote(command)


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
