"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from dom.config import ModelEndpoint
from dom.usage import UsageRecorder, check_model_allowed, request_kind

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
        *,
        usage: UsageRecorder | None = None,
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        self.http = http
        self._sleep = sleep
        self.usage = usage
        self.host = httpx.URL(endpoint.base_url).host

    async def complete(
        self, messages: list[dict], tools: list[dict], *, fallback: bool = False, once: bool = False,
    ) -> dict:
        """POST /chat/completions and return the assistant message."""
        body: dict = {"model": self.endpoint.model, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.endpoint.base_url.rstrip('/')}/chat/completions"

        for delay in (None,) if once else (*RETRY_BACKOFF, None):
            await check_model_allowed()
            response = await self.http.post(url, json=body, headers=headers, timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS))
            if self.usage is not None:
                await self.usage.record_response(response, "chat", self.endpoint.model, self.host, fallback=fallback)
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
        self.last_model: str | None = None
        self.last_fallback_at: datetime | None = None

    async def complete(self, messages: list[dict], tools: list[dict], *, once: bool = False) -> dict:
        if once:
            return await self.primary.complete(messages, tools, once=True)
        try:
            reply = await self.primary.complete(messages, tools)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if self.fallback is None or (status is not None and not _retryable(status)):
                raise
            log.warning("chat model %s failed (%s); using fallback %s",
                        self.primary.endpoint.model, status or type(exc).__name__, self.fallback.endpoint.model)
            reply = await self.fallback.complete(messages, tools, fallback=True)
            if request_kind("chat") == "chat":
                self.last_model = self.fallback.endpoint.model
                self.last_fallback_at = (
                    self.fallback.usage.now() if self.fallback.usage is not None else datetime.now(timezone.utc)
                )
        else:
            if request_kind("chat") == "chat":
                self.last_model = self.primary.endpoint.model
        return reply
