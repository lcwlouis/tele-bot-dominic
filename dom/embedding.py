"""OpenAI-compatible embeddings with the same ordered provider chain as other model roles."""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass

import httpx

from dom.config import Config, ProviderEndpoint
from dom.llm import CONNECT_TIMEOUT_SECONDS, MissingProviderKey, openrouter_host
from dom.usage import UsageRecorder, check_model_allowed, provider_address

TIMEOUT_SECONDS = 45
MAX_DIMENSIONS = 65_536


@dataclass(frozen=True)
class EmbeddingBatch:
    model_key: str
    model_id: str
    vectors: tuple[tuple[float, ...], ...]
    fallback: bool = False


class EmbeddingProvider:
    def __init__(
        self, endpoint: ProviderEndpoint, api_key: str | None, http: httpx.AsyncClient,
        usage: UsageRecorder | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.http = http
        self.usage = usage
        url = httpx.URL(endpoint.base_url)
        self.host = url.host
        self.address = provider_address(url)
        identity = f"{self.address}\n{endpoint.model}".encode()
        self.model_key = hashlib.sha256(identity).hexdigest()[:24]

    @property
    def available(self) -> bool:
        return bool(self.api_key) or not self.endpoint.api_key_env

    async def embed(self, texts: list[str], *, fallback: bool = False) -> EmbeddingBatch:
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding input must contain non-empty text")
        await check_model_allowed()
        if not self.available:
            raise MissingProviderKey("Set the embedding provider's API key in .env.")
        body: dict = {"model": self.endpoint.model, "input": texts, "encoding_format": "float"}
        if openrouter_host(self.host):
            body["provider"] = {"zdr": True}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = await self.http.post(
            f"{self.endpoint.base_url.rstrip('/')}/embeddings", json=body, headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=min(TIMEOUT_SECONDS, CONNECT_TIMEOUT_SECONDS)),
        )
        if self.usage is not None:
            await self.usage.record_response(
                response, "embedding", self.endpoint.model, self.address, fallback=fallback,
            )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError("embedding provider returned the wrong number of vectors")
        if any(not isinstance(row, dict) or row.get("index") != index for index, row in enumerate(rows)):
            try:
                ordered = sorted(rows, key=lambda row: row["index"])
            except (KeyError, TypeError):
                raise ValueError("embedding provider returned invalid vector indices") from None
        else:
            ordered = rows
        if [row.get("index") for row in ordered] != list(range(len(texts))):
            raise ValueError("embedding provider returned invalid vector indices")
        vectors = tuple(_vector(row.get("embedding") if isinstance(row, dict) else None) for row in ordered)
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("embedding provider returned inconsistent vector dimensions")
        return EmbeddingBatch(self.model_key, self.endpoint.model, vectors, fallback)


def _vector(value: object) -> tuple[float, ...]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_DIMENSIONS:
        raise ValueError("embedding provider returned an invalid vector")
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        raise ValueError("embedding provider returned an invalid vector") from None
    if any(not math.isfinite(item) for item in vector) or not any(vector):
        raise ValueError("embedding provider returned an invalid vector")
    return vector


class EmbeddingRouter:
    def __init__(self, primary: EmbeddingProvider, *fallbacks: EmbeddingProvider) -> None:
        self.providers = (primary, *fallbacks)

    @property
    def available(self) -> bool:
        return any(provider.available for provider in self.providers)

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        last: Exception | None = None
        for index, provider in enumerate(self.providers):
            try:
                return await provider.embed(texts, fallback=index > 0)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, MissingProviderKey, ValueError) as exc:
                last = exc
        if last is None:
            raise RuntimeError("no embedding provider is configured")
        raise last


def routed_embedding_client(
    config: Config, http: httpx.AsyncClient, usage: UsageRecorder | None = None,
) -> EmbeddingRouter | None:
    endpoints = config.providers("embedding")
    if not endpoints:
        return None
    providers = [
        EmbeddingProvider(
            endpoint, config.secret(endpoint.api_key_env) if endpoint.api_key_env else None, http, usage,
        )
        for endpoint in endpoints
    ]
    return EmbeddingRouter(*providers)
