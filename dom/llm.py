"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from dom.config import ModelEndpoint

TIMEOUT_SECONDS = 90
CONNECT_TIMEOUT_SECONDS = 5  # an unreachable home lab should fail over fast

log = logging.getLogger("dom.llm")
RETRY_BACKOFF = (1, 3)  # seconds before the 1st and 2nd retry


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class ChatClient:
    def __init__(
        self,
        endpoint: ModelEndpoint,
        api_key: str | None,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        self.http = http
        self._sleep = sleep

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        """POST /chat/completions and return the assistant message."""
        body: dict = {"model": self.endpoint.model, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.endpoint.base_url.rstrip('/')}/chat/completions"

        for delay in (*RETRY_BACKOFF, None):
            response = await self.http.post(url, json=body, headers=headers, timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS))
            if delay is None or not _retryable(response.status_code):
                break
            await self._sleep(delay)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


class FallbackClient:
    """Use `primary` (e.g. a home-lab endpoint); when it is unreachable or failing (connection error,
    timeout, 429/5xx after retries) use `fallback` for that request. A 4xx from the primary is a bug in
    the request, not an outage, so it is raised rather than hidden."""

    def __init__(self, primary: ChatClient, fallback: ChatClient | None):
        self.primary = primary
        self.fallback = fallback

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        try:
            return await self.primary.complete(messages, tools)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if self.fallback is None or (status is not None and not _retryable(status)):
                raise
            log.warning("chat model %s failed (%s); using fallback %s",
                        self.primary.endpoint.model, status or type(exc).__name__, self.fallback.endpoint.model)
            return await self.fallback.complete(messages, tools)
