"""Client for the AIO Sandbox HTTP API."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import httpx


class SandboxError(RuntimeError):
    pass


class SandboxClient:
    def __init__(self, base_url: str, api_key: str | None, http: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.http = http

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _check(self, response: httpx.Response, *, envelope: bool = True) -> Any:
        data = None
        if envelope or response.is_error:
            try:
                data = response.json()
            except ValueError:
                pass
        if isinstance(data, dict):
            message = data.get("message")
            if (response.is_error or data.get("success") is False) and isinstance(message, str):
                if self._api_key:
                    message = message.replace(self._api_key, "[redacted]")
                raise SandboxError(message)
        if not response.is_success:
            raise SandboxError(f"HTTP {response.status_code}")
        if envelope:
            if not isinstance(data, dict) or data.get("success") is not True or "data" not in data:
                raise SandboxError("invalid sandbox response")
            return data["data"]

    async def _json(self, path: str, body: dict | None = None, *, timeout: int = 30, **kwargs) -> Any:
        try:
            response = await self.http.request(
                "POST" if body is not None or "files" in kwargs else "GET",
                f"{self.base_url}/v1/{path}", headers=self.auth_headers(), json=body,
                timeout=timeout, follow_redirects=False, **kwargs,
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"sandbox request failed ({type(exc).__name__})") from None
        return self._check(response)

    async def _bytes(self, path: str, *, params: dict | None = None, max_bytes: int | None = None) -> bytes:
        try:
            async with self.http.stream(
                "GET", f"{self.base_url}/v1/{path}", headers=self.auth_headers(),
                params=params, timeout=30, follow_redirects=False,
            ) as response:
                if response.is_error:
                    await response.aread()
                self._check(response, envelope=False)
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if max_bytes is not None and len(content) + len(chunk) > max_bytes:
                        raise SandboxError(f"file exceeds {max_bytes} bytes")
                    content.extend(chunk)
                return bytes(content)
        except httpx.HTTPError as exc:
            raise SandboxError(f"sandbox request failed ({type(exc).__name__})") from None

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

    async def screenshot(self) -> bytes:
        return await self._bytes("browser/screenshot")

    async def captcha_detect(self) -> Any:
        return await self._json("browser/captcha/detect")

    async def bash(self, command: str) -> dict | None:
        return await self._json("bash/exec", {"command": command}, timeout=120)

    async def jupyter(self, code: str) -> dict | None:
        return await self._json("jupyter/execute", {"code": code}, timeout=120)

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
