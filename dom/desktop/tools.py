"""Function schemas and sandbox tool implementations for the desktop worker."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from dom.desktop.client import SandboxClient, url_host

from dom.desktop.files import MAX_RESULT_FILES
from dom.skills import BODY_LIMIT, DESCRIPTION_LIMIT, NAME_LIMIT

RESULT_LIMIT = 6000
PAGE_PART = 4000  # read_page text per part, measured after escaping, so a part never reaches RESULT_LIMIT
_STRING = {"type": "string"}
_TOOLS = {
    "browse": ("Navigate to a URL.", {"url": _STRING}),
    "read_page": (
        "Read the current page as Markdown. A long page comes in parts: the first line says which part of how many; "
        "pass part to read further. Scrolling does not change what this returns.",
        {"part": {"type": "integer", "minimum": 1}},
    ),
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
    "saved_logins": (
        "List non-secret metadata for saved site logins (id, site, label, masked username hint; never passwords). "
        "Pass an optional site hostname to filter.",
        {"site": _STRING},
    ),
    "fill_saved_login": (
        "Fill a saved login into the current HTTPS login page and submit it without exposing the password to the model. "
        "Requires the active browser tab to match the saved login's site and may pause for owner approval.",
        {
            "login_id": {"type": "integer", "minimum": 1},
            "username_selector": _STRING,
            "password_selector": _STRING,
        },
    ),
    "upload_file": (
        "Upload a file from the sandbox into a page's file input. May pause for owner approval.",
        {"selector": _STRING, "path": _STRING},
    ),
    "skill_view": ("Read an owner-approved skill by name.", {"name": _STRING}),
    "propose_skill": ("Only when asked to remember how, or when success needed a workaround found in this run: draft a "
                      "reusable public skill once, after the work succeeds and before finish.", {
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
# What a self-directed task (W13) may plan to use. No handoff (no logins), no skills, no skill proposals, and no
# shell or Python: those would share the user, files and X display with the browser and could drive it directly.
OWN = (*BROWSER, "read_file", "write_file")
# What a task for a group or a friend may use: the same read-only browser as a self-directed task, as text only: no
# files (nothing it makes is sent anywhere), no screenshots, and no `look`, which is a paid vision call of its own.
GUEST = tuple(name for name in BROWSER if name not in ("save_screenshot", "look"))
SEARCH_TEXT_LIMIT = 120
_BLANK = ("about:blank", "chrome://newtab/", "chrome://new-tab-page/")
_SITE = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
_PRIVATE_SUFFIXES = (
    ".local", ".lan", ".home", ".internal", ".intranet", ".corp", ".arpa", ".localhost", ".test", ".invalid",
)
# Wildcard-DNS services: any `<anything>.nip.io` resolves to an address the caller picks (often 127.0.0.1 or a LAN
# host), so they get the same refusal as an IP literal or a `.local` name.
_WILDCARD_DNS = ("nip.io", "sslip.io", "xip.io", "lvh.me", "localtest.me", "traefik.me")
# Public suffixes a planned site may not be, because unrelated owners share them (a plan naming one would allow
# every tenant). A small built-in list rather than a Public Suffix List dependency: a plan names a few sites, and
# anything missed here still faces the LAN guard. Country second levels (co.uk, com.au, ne.jp...) are matched by
# _CC_SECOND_LEVEL, a few provider patterns by _SHARED_PATTERNS; the list holds the rest.
_SHARED_SUFFIXES = frozenset((
    "github.io", "gitlab.io", "pages.dev", "workers.dev", "r2.dev", "vercel.app", "netlify.app", "web.app",
    "firebaseapp.com", "herokuapp.com", "appspot.com", "wordpress.com", "tumblr.com",
    "substack.com", "medium.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com", "cloudapp.net",
    "azureedge.net", "elasticbeanstalk.com", "fly.dev", "onrender.com", "glitch.me", "replit.app", "repl.co",
    "surge.sh", "ngrok.io", "ngrok-free.app", "trycloudflare.com", "duckdns.org", "no-ip.org", "ddns.net",
    "dyndns.org", "myshopify.com", "wixsite.com", "weebly.com", "neocities.org", "codeberg.page", "translate.goog",
    "sourceforge.io", "readthedocs.io", "pythonanywhere.com",
    "webflow.io", "notion.site", "github.dev", "carrd.co", "framer.app", "my.id",
    "vercel.dev", "deno.dev", "railway.app", "hf.space", "streamlit.app", "replit.dev",
    "bitbucket.io", "pantheonsite.io", "bubbleapps.io", "wixstudio.io",
    # CentralNic and similar second levels sold to the public.
    "eu.org", "uk.com", "us.com", "eu.com", "gb.com", "gb.net", "uk.net", "de.com", "jpn.com", "cn.com", "sa.com",
    "za.com", "br.com", "ru.com", "kr.com", "qc.com", "us.org",
    # Government and school second levels whose first label would also be a normal word.
    "go.jp", "go.kr", "go.th", "go.id", "go.ke", "go.tz", "go.ug", "go.cr", "go.ci",
))
_CC_SECOND_LEVEL = re.compile(r"(co|com|net|org|gov|edu|ac|or|ne|gob|mil|nic|ltd|plc|sch|me|ed|lg|gr|ad)\.[a-z]{2}")
_SHARED_PATTERNS = re.compile(
    r"s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com|[a-z0-9-]+\.(?:compute|elb)\.amazonaws\.com"
    r"|blogspot\.[a-z]{2,3}(?:\.[a-z]{2})?"
)
_LOGIN_WORDS = ("pass", "login", "user", "email", "card", "otp")
_SEARCH_FIELD = re.compile(r"search|\bq\b|query", re.IGNORECASE)
# One compound selector: an optional tag, then ids, classes and attribute tests. No `,`, `(`, `:`, spaces or
# combinators, so it cannot name a second element as well.
_SIMPLE_SELECTOR = re.compile(r"[a-z]*(?:#[\w-]+|\.[\w-]+|\[[\w-]+(?:[~|^$*]?=(?:\"[\w-]*\"|'[\w-]*'|[\w-]*))?\])*", re.IGNORECASE)
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
    if any(host == name or host.endswith("." + name) for name in _WILDCARD_DNS):
        return None
    if host in _SHARED_SUFFIXES or _CC_SECOND_LEVEL.fullmatch(host) or _SHARED_PATTERNS.fullmatch(host):
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


def web_host(host: str | None) -> bool:
    """Whether `host` is a public web name: not an IP literal, a LAN-style name or a wildcard-DNS service. Unlike
    `site`, shared suffixes (`github.io`) are fine: this is a page to read, not a site to trust."""
    if not host or not _SITE.fullmatch(host) or host.endswith(_PRIVATE_SUFFIXES):
        return False
    return not any(host == name or host.endswith("." + name) for name in _WILDCARD_DNS)


def on_plan(host: str | None, sites: tuple[str, ...] | None) -> bool:
    """Whether `host` is one of the plan's sites or a subdomain of one. With no plan (a guest task browses the open
    web), any public web name."""
    if sites is None:
        return web_host(host)
    return bool(host) and any(host == entry or host.endswith("." + entry) for entry in sites)


def on_plan_url(url: object, sites: tuple[str, ...] | None) -> bool:
    return url in _BLANK or on_plan(own_host(url), sites)


def search_text(selector: str, text: str) -> bool:
    """A self-directed task may type only short text, through one simple selector (no lists, pseudo-classes or
    combinators) that reads as a search field and not as a login field."""
    lowered = selector.lower()
    return (
        len(text) <= SEARCH_TEXT_LIMIT and bool(_SIMPLE_SELECTOR.fullmatch(selector))
        and bool(_SEARCH_FIELD.search(selector)) and not any(word in lowered for word in _LOGIN_WORDS)
    )


def _field(element: dict, key: str) -> str:
    value = element.get(key)
    return value.lower() if isinstance(value, str) else ""


def search_box(element: object) -> bool:
    """Whether an element from the browser's element list is a search box: a text or search input that says so
    by its role, type or placeholder."""
    if not isinstance(element, dict) or _field(element, "tag") != "input" or _field(element, "type") not in ("", "search", "text"):
        return False
    return _field(element, "role") == "searchbox" or _field(element, "type") == "search" or bool(
        _SEARCH_FIELD.search(_field(element, "placeholder")),
    )


def link_element(element: object) -> bool:
    """Whether an element from the browser's element list (fields tag, role, type, href) is plainly a link to a
    web page. Anything it cannot tell is not."""
    if not isinstance(element, dict):
        return False
    href = _field(element, "href")
    is_link = _field(element, "tag") == "a" or _field(element, "role") == "link"
    plain = _field(element, "type") not in ("submit", "button", "image", "reset")
    # A web address or a relative one; never javascript:, data: or another scheme.
    web = href.startswith(("http://", "https://", "/", "#", "?"))
    return is_link and plain and web


def under_own_roots(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in OWN_FILE_ROOTS)


_OPTIONAL_ARGS = frozenset(("files", "part", "site", "username_selector", "password_selector"))


def schemas() -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": [key for key in properties if key not in _OPTIONAL_ARGS],
                           "additionalProperties": False},
        },
    } for name, (description, properties) in _TOOLS.items()]


def arguments(function: dict) -> tuple[str, dict]:
    name = function["name"]
    if name not in _TOOLS:
        raise ValueError("unknown desktop tool")
    args = json.loads(function.get("arguments") or "{}")
    properties = _TOOLS[name][1]
    required = properties.keys() - _OPTIONAL_ARGS
    if not isinstance(args, dict) or not required <= args.keys() <= properties.keys():
        raise ValueError("invalid tool arguments")
    for key, value in args.items():
        if key == "files":
            if isinstance(value, list) and len(value) <= MAX_RESULT_FILES and all(isinstance(item, str) for item in value):
                continue
            raise ValueError("invalid files")
        if key == "selector_or_index" and type(value) is int and value >= 0:
            continue
        if key in ("part", "login_id"):
            if type(value) is int and value >= 1:
                continue
            raise ValueError("invalid tool arguments")
        if not isinstance(value, str):
            raise ValueError("invalid tool arguments")
        if "enum" in properties[key] and value not in properties[key]["enum"]:
            raise ValueError("invalid tool arguments")
    if name == "browse" and not _web_url(args["url"]):
        raise ValueError("invalid tool arguments")
    return name, args


_ESCAPED = {"&": 5, "<": 4, ">": 4}  # how long xml escape() makes each character that it changes


def _page_parts(text: str) -> list[str]:
    """The page cut so that each part, once escaped, is at most PAGE_PART characters."""
    parts, start, size = [], 0, 0
    for index, char in enumerate(text):
        width = _ESCAPED.get(char, 1)
        if size + width > PAGE_PART:
            parts.append(text[start:index])
            start, size = index, 0
        size += width
    return [*parts, text[start:]]


def _page_part(text: Any, part: int) -> Any:
    """One part of a long page, headed "Part n of m."; a page that fits in one part comes back as it is."""
    if not isinstance(text, str):
        return text
    parts = _page_parts(text)
    if len(parts) == 1:
        return text
    if part > len(parts):
        return f"There are only {len(parts)} parts."
    return f"Part {part} of {len(parts)}.\n" + parts[part - 1]


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
    part = args.pop("part", 1) if name == "read_page" else None
    result = await getattr(client, _METHODS[name])(**args)
    if name == "read_page" and isinstance(result, dict):
        return _page_part(result.get("markdown", result), part)
    if name == "list_elements" and isinstance(result, list):
        return result[:80]
    if name == "read_file" and isinstance(result, dict):
        return result.get("content", result)
    return result
