"""Client for the AIO Sandbox HTTP API."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx


class SandboxError(RuntimeError):
    pass


JSON_LIMIT = 2_000_000
SCREENSHOT_LIMIT = 5_000_000
EVALUATE_LIMIT = 4096
DEFAULT_RESOLUTION = "1280x1024"
PHONE_RESOLUTION = "800x600"
# Allowed resolutions from agent-infra/sandbox BrowserConfigRequest (POST /v1/browser/config).
VIEWER_RESOLUTIONS: dict[str, tuple[int, int]] = {
    "640x480": (640, 480),
    "800x600": (800, 600),
    "1024x768": (1024, 768),
    "1280x720": (1280, 720),
    "1280x800": (1280, 800),
    "1280x960": (1280, 960),
    "1280x1024": (1280, 1024),
    "1360x768": (1360, 768),
    "1400x1050": (1400, 1050),
    "1600x1200": (1600, 1200),
    "1680x1050": (1680, 1050),
    "1920x1080": (1920, 1080),
    "1920x1200": (1920, 1200),
}
_HOST = re.compile(r"[a-z0-9.:-]{1,253}")
_PASSWORD_FOCUS_JS = (
    "(() => {"
    " let el = document.activeElement;"
    " for (let i = 0; i < 8 && el; i++) {"
    "   if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; }"
    "   else if (String(el.tagName || '').toUpperCase() === 'IFRAME') {"
    "     try {"
    "       const next = el.contentDocument && el.contentDocument.activeElement;"
    "       if (!next || next === el) break;"
    "       el = next;"
    "     } catch { break; }"
    "   } else { break; }"
    " }"
    " return Boolean(el && String(el.tagName || '').toUpperCase() === 'INPUT'"
    " && String(el.type || '').toLowerCase() === 'password');"
    "})()"
)
# Derive DISPLAY from the environment or the active Xvnc Unix socket (/tmp/.X11-unix/X<n>),
# then clear both CLIPBOARD and PRIMARY selections and fail non-zero if no tool succeeds.
_CLEAR_CLIPBOARD_SH = (
    'd="${DISPLAY:-}"; '
    'if [ -z "$d" ]; then '
    'for sock in /tmp/.X11-unix/X*; do '
    'if [ -S "$sock" ]; then d=":${sock##*/X}"; break; fi; '
    'done; '
    'fi; '
    '[ -n "$d" ] || { echo "no X11 display found" >&2; exit 2; }; '
    'if command -v xsel >/dev/null 2>&1; then '
    'DISPLAY="$d" xsel -bc && DISPLAY="$d" xsel -pc; '
    'elif command -v xclip >/dev/null 2>&1; then '
    'printf "" | DISPLAY="$d" xclip -selection clipboard -in >/dev/null 2>&1 '
    '&& printf "" | DISPLAY="$d" xclip -selection primary -in >/dev/null 2>&1; '
    'else echo "neither xsel nor xclip is installed" >&2; exit 127; fi'
)


def url_host(url: object) -> str | None:
    """Only the host of a plain http(s) URL: no credentials, port, path, query or fragment."""
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        host = parts.hostname if parts is not None and parts.scheme in ("http", "https") else None
    except ValueError:
        return None
    return host if host and _HOST.fullmatch(host) else None


class SandboxClient:
    def __init__(self, base_url: str, api_key: str | None, http: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.http = http

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def redact(self, text: str) -> str:
        return text.replace(self._api_key, "[redacted]") if self._api_key else text

    def _check(self, response: httpx.Response, body: bytes, *, envelope: bool = True) -> Any:
        data = None
        if envelope or response.is_error or response.headers.get("content-type", "").startswith("application/json"):
            try:
                data = json.loads(body)
            except ValueError:
                pass
        if isinstance(data, dict):
            message = data.get("message")
            if (response.is_error or data.get("success") is False) and isinstance(message, str):
                raise SandboxError(self.redact(message))
            if data.get("success") is False and not envelope:
                raise SandboxError("sandbox reported an error")
        if not response.is_success:
            raise SandboxError(f"HTTP {response.status_code}")
        if envelope:
            if not isinstance(data, dict) or data.get("success") is not True or "data" not in data:
                raise SandboxError("invalid sandbox response")
            return data["data"]
        return body

    async def _fetch(
        self, method: str, path: str, *, limit: int | None, timeout: int = 30, **kwargs,
    ) -> tuple[httpx.Response, bytes]:
        """Read the body as a stream so a hostile sandbox cannot make Dom buffer more than `limit`.

        Only identity-encoded responses are read, so the limit counts the bytes on the wire: the HTTP client
        would otherwise inflate a compressed body far past it."""
        try:
            async with self.http.stream(
                method, f"{self.base_url}/v1/{path}", headers={**self.auth_headers(), "Accept-Encoding": "identity"},
                timeout=timeout, follow_redirects=False, **kwargs,
            ) as response:
                encodings = [value.strip().lower() for value in response.headers.get_list("content-encoding") if value.strip()]
                if any(value != "identity" for value in encodings):
                    raise SandboxError("sandbox sent a compressed response")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if limit is not None and len(body) + len(chunk) > limit:
                        raise SandboxError(f"sandbox response exceeds {limit} bytes")
                    body.extend(chunk)
                return response, bytes(body)
        except httpx.HTTPError as exc:
            raise SandboxError(f"sandbox request failed ({type(exc).__name__})") from None

    async def _json(self, path: str, body: dict | None = None, *, timeout: int = 30, **kwargs) -> Any:
        response, content = await self._fetch(
            "POST" if body is not None or "files" in kwargs else "GET", path,
            limit=JSON_LIMIT, timeout=timeout, json=body, **kwargs,
        )
        return self._check(response, content)

    async def _bytes(self, path: str, *, params: dict | None = None, max_bytes: int | None = None) -> bytes:
        response, content = await self._fetch("GET", path, limit=max_bytes, params=params)
        return self._check(response, content, envelope=False)

    async def health(self) -> str | None:
        return await self._json("sandbox")

    async def navigate(self, url: str) -> dict | None:
        return await self._json("browser/page/navigate", {"url": url}, timeout=45)

    async def markdown(self) -> dict | None:
        return await self._json("browser/page/markdown")

    async def elements(self) -> list | None:
        return await self._json("browser/page/elements")

    async def click(self, selector_or_index: str | int) -> Any:
        field = "index" if isinstance(selector_or_index, int) else "selector"
        return await self._json("browser/page/click", {field: selector_or_index})

    async def fill(self, selector: str, text: str) -> Any:
        return await self._json("browser/page/fill", {"selector": selector, "text": text})

    async def type(self, text: str) -> Any:
        return await self._json("browser/page/type", {"text": text})

    async def press_key(self, key: str) -> Any:
        return await self._json("browser/page/press_key", {"key": key})

    async def scroll(self, direction: str) -> Any:
        return await self._json("browser/page/scroll", {"direction": direction})

    async def browser_tabs(self) -> Any:
        return await self._json("browser/tabs")

    async def set_browser_resolution(self, resolution: str, *, timeout: int = 10) -> Any:
        dims = VIEWER_RESOLUTIONS.get(resolution)
        if dims is None:
            raise ValueError(f"unsupported browser resolution: {resolution}")
        width, height = dims
        return await self._json("browser/config", {"resolution": {"width": width, "height": height}}, timeout=timeout)

    async def is_password_focused(self, *, timeout: int = 5) -> bool:
        """Whether the active browser tab currently focuses an input[type=password].
        Treats the sandbox response as hostile input: only a literal JSON true returns True."""
        response, content = await self._fetch(
            "POST", "browser/page/evaluate",
            limit=EVALUATE_LIMIT, timeout=timeout, json={"expression": _PASSWORD_FOCUS_JS},
        )
        data = self._check(response, content)
        if data is True:
            return True
        if isinstance(data, dict):
            return data.get("result") is True or data.get("value") is True
        return False

    async def clear_clipboard(self, *, timeout: int = 5) -> None:
        """Clear X11 clipboard and primary selections inside the sandbox; raise SandboxError if the clear fails."""
        result = await self._json("bash/exec", {"command": _CLEAR_CLIPBOARD_SH}, timeout=timeout)
        if not isinstance(result, dict) or result.get("exit_code") != 0:
            code = result.get("exit_code") if isinstance(result, dict) else None
            stderr = result.get("stderr") if isinstance(result, dict) else None
            detail = self.redact(stderr.strip())[:160] if isinstance(stderr, str) and stderr.strip() else ""
            suffix = f": {detail}" if detail else ""
            raise SandboxError(f"clipboard clear failed (exit code {code!r}{suffix})")

    async def current_url(self) -> str | None:
        """The URL of the browser's active tab, or None when no tab is marked active or the sandbox does not
        say. The sandbox reports tabs as {index, url, title, is_active} (checked 2026-11-19)."""
        tabs = await self.browser_tabs()
        return next((
            tab["url"] for tab in (tabs if isinstance(tabs, list) else ())
            if isinstance(tab, dict) and tab.get("type", "page") == "page" and tab.get("is_active") is True
            and isinstance(tab.get("url"), str)
        ), None)

    async def screenshot(self) -> bytes:
        return await self._bytes("browser/screenshot", max_bytes=SCREENSHOT_LIMIT)

    async def captcha_detect(self) -> Any:
        return await self._json("browser/captcha/detect")

    async def bash(self, command: str) -> dict | None:
        return await self._json("bash/exec", {"command": command}, timeout=120)

    async def run_python(self, code: str) -> dict:
        path = f"/tmp/{secrets.token_hex(16)}.py"
        await self.write_file(path, code)
        # Stop the script before the Bash request's 120-second HTTP timeout.
        result = await self.bash(f"trap 'rm -f -- {path}' EXIT; timeout --kill-after=5s 110s python3 {path}")
        if not isinstance(result, dict):
            raise SandboxError("invalid sandbox bash response")
        return {key: result.get(key) for key in ("exit_code", "stdout", "stderr")}

    async def read_file(self, path: str) -> dict | None:
        return await self._json("file/read", {"file": path})

    async def write_file(self, path: str, content: str) -> dict | None:
        return await self._json("file/write", {"file": path, "content": content})

    async def upload_file(self, path: str, content: bytes) -> dict | None:
        return await self._json(
            "file/upload", data={"path": path}, files={"file": (PurePosixPath(path).name, content)},
        )

    async def download_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        return await self._bytes("file/download", params={"path": path}, max_bytes=max_bytes)
