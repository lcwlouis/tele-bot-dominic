"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import httpx

from dom.config import ModelEndpoint

TIMEOUT_SECONDS = 90
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
            response = await self.http.post(url, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
            if delay is None or not _retryable(response.status_code):
                break
            await self._sleep(delay)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]
