"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from dom import turn_log
from dom.config import Config, ModelRole, ProviderEndpoint
from dom.usage import Kind, UsageRecorder, check_model_allowed, model_failed, provider_address, request_kind

TIMEOUT_SECONDS = 90
CONNECT_TIMEOUT_SECONDS = 5  # an unreachable home lab should fail over fast

log = logging.getLogger("dom.llm")
RETRY_BACKOFF = (1, 3)  # seconds before the 1st and 2nd retry


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _fallback_worthy(status: int) -> bool:
    """Worth asking the fallback model: a busy or broken primary, or one this key may not use (an expired key, no
    credit, a model withdrawn). A 400 is a bad request, which the fallback would refuse too."""
    return _retryable(status) or status in (401, 402, 403, 404)


def openrouter_host(host: str) -> bool:
    host = host.rstrip(".")
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


class MissingProviderKey(RuntimeError):
    """The provider requires a key that has not been set."""


class ChatClient:
    def __init__(
        self,
        endpoint: ProviderEndpoint,
        api_key: str | None,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        usage: UsageRecorder | None = None,
        kind: Kind = "chat",
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        self.http = http
        self._sleep = sleep
        self.usage = usage
        self.kind = kind
        url = httpx.URL(endpoint.base_url)
        self.host = url.host
        self.address = provider_address(url)

    @property
    def available(self) -> bool:
        return bool(self.api_key) or not self.endpoint.api_key_env

    async def complete(
        self, messages: list[dict], tools: list[dict], *, fallback: bool = False, once: bool = False,
        max_tokens: int | None = None, timeout: float = TIMEOUT_SECONDS,
    ) -> dict:
        """POST /chat/completions and return the assistant message."""
        body: dict = {"model": self.endpoint.model, "messages": messages}
        if tools:
            body["tools"] = tools
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        openrouter = openrouter_host(self.host)
        if openrouter:
            body["provider"] = {"zdr": True}
        if self.endpoint.reasoning_effort is not None:
            if openrouter:
                body["reasoning"] = {"effort": self.endpoint.reasoning_effort}
            else:
                body["reasoning_effort"] = self.endpoint.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.endpoint.base_url.rstrip('/')}/chat/completions"
        kind = request_kind(self.kind)

        for delay in (None,) if once else (*RETRY_BACKOFF, None):
            await check_model_allowed()
            if not self.available:
                raise MissingProviderKey("Set the provider's API key in .env.")
            try:
                with turn_log.model_call(
                    self.endpoint.model, kind, messages, tools, fallback=fallback, host=self.address,
                ) as call:
                    response = await self.http.post(
                        url, json=body, headers=headers, follow_redirects=False,
                        timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_SECONDS)),
                    )
                    if call is not None:
                        call.received(response)
            except (httpx.TransportError, asyncio.CancelledError) as exc:
                if isinstance(exc, httpx.TransportError):
                    model_failed()
                if kind == "escalation" and self.usage is not None:
                    await self.usage.record_response(None, self.kind, self.endpoint.model, self.address, fallback=fallback)
                raise
            if not response.is_success:
                model_failed()
            if self.usage is not None:
                await self.usage.record_response(response, self.kind, self.endpoint.model, self.address, fallback=fallback)
            if delay is None or not _retryable(response.status_code):
                break
            with turn_log.timed("retry_backoff") if kind == "chat" else nullcontext():
                await self._sleep(delay)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


class FallbackClient:
    """Try the role's providers in order, keeping the current retry policy at each provider."""

    def __init__(self, primary: ChatClient, fallback: ChatClient | None, *additional: ChatClient):
        self.primary = primary
        self.fallback = fallback
        self.providers = (primary, fallback, *additional) if fallback is not None else (primary,)
        self.last_model: str | None = None
        self.last_fallback_at: datetime | None = None

    @property
    def available(self) -> bool:
        return any(provider.available for provider in self.providers)

    async def complete(self, messages: list[dict], tools: list[dict], *, once: bool = False) -> dict:
        return await self._complete(messages, tools, start=0, once=once)

    async def complete_on_fallback(self, messages: list[dict], tools: list[dict], *, once: bool = False) -> dict:
        """Ask the fallback model directly, e.g. when the primary is up but keeps answering unusably."""
        if self.fallback is None:
            raise RuntimeError("no fallback model is configured")
        return await self._complete(messages, tools, start=1, once=once)

    async def _complete(self, messages: list[dict], tools: list[dict], *, start: int, once: bool) -> dict:
        for index in range(start, len(self.providers)):
            provider = self.providers[index]
            fallback = index > 0
            try:
                reply = await provider.complete(messages, tools, fallback=fallback, once=once)
            except (httpx.TransportError, httpx.HTTPStatusError, MissingProviderKey) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if index + 1 == len(self.providers) or (status is not None and not _fallback_worthy(status)):
                    raise
                log.warning("model %s on %s failed (%s); trying the next provider",
                            provider.endpoint.model, provider.address, status or type(exc).__name__)
                continue
            if request_kind(provider.kind) == "chat":
                self.last_model = provider.endpoint.model
                if fallback:
                    self.last_fallback_at = provider.usage.now() if provider.usage else datetime.now(timezone.utc)
            return reply


def routed_client(
    config: Config, role: ModelRole, http: httpx.AsyncClient, usage: UsageRecorder | None = None,
) -> FallbackClient:
    endpoints = config.providers(role)
    if role == "chat":
        endpoints += config.providers("fallback")
    elif role == "drafting":  # drafts fall back to the chat route when no drafting provider can answer
        endpoints += config.providers("chat") + config.providers("fallback")
    elif role == "escalation":
        endpoints = endpoints or config.providers("drafting")
    kind: Kind = role if role in ("media", "escalation") else "chat"
    providers = [
        ChatClient(endpoint, config.secret(endpoint.api_key_env) if endpoint.api_key_env else None, http,
                   usage=usage, kind=kind)
        for endpoint in endpoints
    ]
    primary, *chain = providers
    return FallbackClient(primary, *chain) if chain else FallbackClient(primary, None)
