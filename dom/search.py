"""Web search role with ordered provider fallback: SearXNG, OpenRouter web, DeepSeek web search and Tavily."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

import httpx

from dom import turn_log
from dom.config import Config, ProviderEndpoint, SearchProvider
from dom.llm import MissingProviderKey, openrouter_host
from dom.usage import UsageRecorder, check_model_allowed, provider_address

CONNECT_TIMEOUT_SECONDS = 3
SEARCH_TIMEOUT_SECONDS = 8  # fast fail-over on the reply path for JSON search APIs
OPENROUTER_TIMEOUT_SECONDS = 15  # OpenRouter web runs search plus LLM synthesis
TIMEOUT_SECONDS = OPENROUTER_TIMEOUT_SECONDS
MAX_RESULT = 2000
MAX_RESULTS = 5
MAX_TITLE = 120
MAX_URL = 200
MAX_SNIPPET = 240
DEEPSEEK_QUERY_LIMIT = 128  # DeepSeek /v1/tools/search enforces 1..128 Unicode chars
DEEPSEEK_SEARCH_COST_USD = 0.00075  # $0.75 per 1,000 successful non-empty requests
TAVILY_CREDIT_COST_USD = 0.008  # pay-as-you-go rate per basic credit

log = logging.getLogger("dom.search")
_XML_ILLEGAL = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_ATTRIBUTE_ESCAPES = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


@dataclass(frozen=True)
class ProviderInfo:
    id: SearchProvider
    label: str
    default_base_url: str
    default_api_key_env: str
    needs_model: bool
    needs_key: bool
    data_retention: str
    policy_url: str | None = None
    policy_label: str | None = None


PROVIDER_INFO: dict[SearchProvider, ProviderInfo] = {
    "searxng": ProviderInfo(
        id="searxng",
        label="SearXNG (self-hosted)",
        default_base_url="",
        default_api_key_env="",
        needs_model=False,
        needs_key=False,
        data_retention="Self-hosted on your own network; queries only leave your host to the upstream engines you enable in SearXNG.",
    ),
    "openrouter": ProviderInfo(
        id="openrouter",
        label="OpenRouter web",
        default_base_url="https://openrouter.ai/api/v1",
        default_api_key_env="OPENROUTER_API_KEY",
        needs_model=True,
        needs_key=True,
        data_retention="Zero data retention enforced via provider.zdr under your OpenRouter key.",
    ),
    "deepseek": ProviderInfo(
        id="deepseek",
        label="DeepSeek web search",
        default_base_url="https://api.deepseek.com/v1",
        default_api_key_env="DEEPSEEK_API_KEY",
        needs_model=False,
        needs_key=True,
        data_retention="Cloud search API with no ZDR header; check DeepSeek's Open Platform Terms before enabling.",
        policy_url="https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html",
        policy_label="DeepSeek Open Platform Terms",
    ),
    "tavily": ProviderInfo(
        id="tavily",
        label="Tavily",
        default_base_url="https://api.tavily.com",
        default_api_key_env="TAVILY_API_KEY",
        needs_model=False,
        needs_key=True,
        data_retention="Cloud search API publishing zero-data-retention terms; review Tavily's data policy before enabling.",
        policy_url="https://www.tavily.com/privacy",
        policy_label="Tavily data policy",
    ),
}


@dataclass(frozen=True)
class SearchOutcome:
    text: str
    provider: SearchProvider
    label: str
    host: str
    result_count: int | None = None
    index: int = 0


def search_provider_kind(endpoint: ProviderEndpoint) -> SearchProvider:
    return endpoint.provider or "openrouter"


def _escaped_prefix(value: str, limit: int, entities: dict[str, str] | None = None) -> str:
    if limit <= 0:
        return ""
    text = escape(_XML_ILLEGAL.sub("", value[:limit]), entities or {})[:limit]
    head, amp, tail = text.rpartition("&")
    return head if amp and ";" not in tail else text


def _clean_line(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _urls(message: dict) -> list[str]:
    annotations = message.get("annotations")
    if not isinstance(annotations, list):
        return []
    urls: list[str] = []
    for annotation in annotations:
        citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
        url = citation.get("url") if isinstance(citation, dict) else None
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    return urls


def _wrap_answer(query: str, answer: str, urls: list[str]) -> str:
    header = f'<search_results untrusted="true" query="{_escaped_prefix(query, 500, _ATTRIBUTE_ESCAPES)}">'
    footer = "</search_results>"
    sources = _escaped_prefix("\nSources: " + ", ".join(urls), 750) if urls else ""
    room = MAX_RESULT - len(header) - len(footer) - len(sources)
    return header + _escaped_prefix(answer, room) + sources + footer


def _wrap_results(query: str, items: list[tuple[str, str, str]]) -> str:
    header = f'<search_results untrusted="true" query="{_escaped_prefix(query, 500, _ATTRIBUTE_ESCAPES)}">'
    footer = "</search_results>"
    blocks: list[str] = []
    for title, url, snippet in items[:MAX_RESULTS]:
        lines: list[str] = []
        idx = len(blocks) + 1
        if title:
            lines.append(f"{idx}. {title}")
        elif url:
            lines.append(f"{idx}. {url}")
        if url and title:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}" if lines else f"{idx}. {snippet}")
        if lines:
            blocks.append("\n".join(lines))
    body = "\n\n".join(blocks) if blocks else "No results found."
    room = MAX_RESULT - len(header) - len(footer)
    return header + _escaped_prefix(body, room) + footer


def _extract_results(raw_results: list, snippet_keys: tuple[str, ...]) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        title = _clean_line(entry.get("title"), MAX_TITLE)
        url = _clean_line(entry.get("url"), MAX_URL)
        snippet = ""
        for key in snippet_keys:
            snippet = _clean_line(entry.get(key), MAX_SNIPPET)
            if snippet:
                break
        if title or url or snippet:
            items.append((title, url, snippet))
        if len(items) >= MAX_RESULTS:
            break
    return items


def _join_path(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _positive_number(value: object) -> float | None:
    if type(value) in (int, float) and value > 0:
        return float(value)
    return None


class SearchClient:
    """One configured web search provider."""

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        api_key: str | None,
        http: httpx.AsyncClient,
        *,
        usage: UsageRecorder | None = None,
    ):
        self.endpoint = endpoint
        self.api_key = api_key or None
        self.http = http
        self.usage = usage
        self.provider: SearchProvider = search_provider_kind(endpoint)
        url = httpx.URL(endpoint.base_url)
        self.host = url.host
        self.address = provider_address(url)

    @property
    def available(self) -> bool:
        if self.provider in ("deepseek", "tavily"):
            return bool(self.api_key)
        return bool(self.api_key) or not self.endpoint.api_key_env

    @property
    def usage_model(self) -> str:
        if self.provider == "openrouter":
            return self.endpoint.model
        if self.provider == "deepseek":
            return "deepseek-search"
        return self.provider

    @property
    def label(self) -> str:
        info = PROVIDER_INFO[self.provider]
        if self.provider == "openrouter" and self.endpoint.model:
            return f"{info.label} ({self.endpoint.model})"
        return f"{info.label} ({self.address})" if self.address else info.label

    def _timeout(self, timeout: float | None) -> tuple[float, httpx.Timeout]:
        default = OPENROUTER_TIMEOUT_SECONDS if self.provider == "openrouter" else SEARCH_TIMEOUT_SECONDS
        total = default if timeout is None else timeout
        return total, httpx.Timeout(total, connect=min(total, CONNECT_TIMEOUT_SECONDS))

    async def search(self, query: str, *, fallback: bool = False, timeout: float | None = None) -> str:
        return (await self.search_with_meta(query, fallback=fallback, timeout=timeout)).text

    async def search_with_meta(
        self, query: str, *, fallback: bool = False, timeout: float | None = None,
    ) -> SearchOutcome:
        await check_model_allowed()
        if not self.available:
            raise MissingProviderKey("Set the provider's API key in .env.")
        if self.provider == "searxng":
            return await self._search_searxng(query, fallback=fallback, timeout=timeout)
        if self.provider == "deepseek":
            return await self._search_deepseek(query, fallback=fallback, timeout=timeout)
        if self.provider == "tavily":
            return await self._search_tavily(query, fallback=fallback, timeout=timeout)
        return await self._search_openrouter(query, fallback=fallback, timeout=timeout)

    async def _search_openrouter(self, query: str, *, fallback: bool, timeout: float | None) -> SearchOutcome:
        messages = [{
            "role": "user",
            "content": f"Search the web and answer concisely with the key facts: {query}",
        }]
        plugins = [{"id": "web", "max_results": 3}]
        body: dict = {"model": self.endpoint.model, "messages": messages, "plugins": plugins}
        if openrouter_host(self.host):
            body["provider"] = {"zdr": True}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        total, request_timeout = self._timeout(timeout)
        with turn_log.model_call(self.usage_model, "search", messages, plugins, fallback=fallback, host=self.address) as call:
            async with asyncio.timeout(total):
                response = await self.http.post(
                    f"{self.endpoint.base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers=headers,
                    follow_redirects=False,
                    timeout=request_timeout,
                )
        if call is not None:
            call.received(response)
        if self.usage is not None:
            await self.usage.record_response(response, "search", self.usage_model, self.address, fallback=fallback)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        answer = message["content"]
        if not isinstance(answer, str):
            raise ValueError("search response has no answer text")
        text = _wrap_answer(query, answer, _urls(message))
        return SearchOutcome(text=text, provider="openrouter", label=self.label, host=self.address)

    async def _search_searxng(self, query: str, *, fallback: bool, timeout: float | None) -> SearchOutcome:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        total, request_timeout = self._timeout(timeout)
        with turn_log.model_call(self.usage_model, "search", [], [], fallback=fallback, host=self.address) as call:
            async with asyncio.timeout(total):
                response = await self.http.get(
                    _join_path(self.endpoint.base_url, "/search"),
                    params={"q": query, "format": "json"},
                    headers=headers,
                    follow_redirects=False,
                    timeout=request_timeout,
                )
        if call is not None:
            call.received(response)
        if self.usage is not None:
            await self.usage.record_response(response, "search", self.usage_model, self.address, fallback=fallback)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("SearXNG response has no results list")
        raw_results = payload["results"]
        if not raw_results and payload.get("unresponsive_engines"):
            raise ValueError("SearXNG upstream engines unresponsive")
        items = _extract_results(raw_results, ("content", "snippet"))
        return SearchOutcome(
            text=_wrap_results(query, items),
            provider="searxng",
            label=self.label,
            host=self.address,
            result_count=len(items),
        )

    async def _search_deepseek(self, query: str, *, fallback: bool, timeout: float | None) -> SearchOutcome:
        trimmed = query.strip()[:DEEPSEEK_QUERY_LIMIT]
        body = {"query": trimmed, "count": MAX_RESULTS, "need_content": False}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        total, request_timeout = self._timeout(timeout)
        with turn_log.model_call(self.usage_model, "search", [], [], fallback=fallback, host=self.address) as call:
            async with asyncio.timeout(total):
                response = await self.http.post(
                    _join_path(self.endpoint.base_url, "/tools/search"),
                    json=body,
                    headers=headers,
                    follow_redirects=False,
                    timeout=request_timeout,
                )
        if call is not None:
            call.received(response)
        payload = None
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
        billed = isinstance(payload, dict) and isinstance(payload.get("results"), list) and bool(payload["results"])
        if self.usage is not None:
            await self.usage.record_response(
                response, "search", self.usage_model, self.address, fallback=fallback,
                default_cost=DEEPSEEK_SEARCH_COST_USD if billed else 0.0,
            )
        response.raise_for_status()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("DeepSeek search response has no results list")
        items = _extract_results(payload["results"], ("snippet", "summary", "content"))
        return SearchOutcome(
            text=_wrap_results(query, items),
            provider="deepseek",
            label=self.label,
            host=self.address,
            result_count=len(items),
        )

    async def _search_tavily(self, query: str, *, fallback: bool, timeout: float | None) -> SearchOutcome:
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": MAX_RESULTS,
            "include_answer": False,
            "include_usage": True,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        total, request_timeout = self._timeout(timeout)
        with turn_log.model_call(self.usage_model, "search", [], [], fallback=fallback, host=self.address) as call:
            async with asyncio.timeout(total):
                response = await self.http.post(
                    _join_path(self.endpoint.base_url, "/search"),
                    json=body,
                    headers=headers,
                    follow_redirects=False,
                    timeout=request_timeout,
                )
        if call is not None:
            call.received(response)
        payload = None
        default_cost = 0.0
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                usage = payload.get("usage")
                credits = _positive_number(usage.get("credits")) if isinstance(usage, dict) else None
                default_cost = TAVILY_CREDIT_COST_USD * (credits if credits is not None else 1.0)
        if self.usage is not None:
            await self.usage.record_response(
                response, "search", self.usage_model, self.address, fallback=fallback, default_cost=default_cost,
            )
        response.raise_for_status()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("Tavily search response has no results list")
        items = _extract_results(payload["results"], ("content", "snippet"))
        return SearchOutcome(
            text=_wrap_results(query, items),
            provider="tavily",
            label=self.label,
            host=self.address,
            result_count=len(items),
        )


class SearchRouter:
    """Try configured search providers in order, falling through on errors and timeouts."""

    def __init__(self, primary: SearchClient, *fallbacks: SearchClient):
        self.primary = primary
        self.providers = (primary, *fallbacks)

    @property
    def endpoint(self) -> ProviderEndpoint:
        return self.primary.endpoint

    @property
    def available(self) -> bool:
        return any(provider.available for provider in self.providers)

    async def search(self, query: str, *, timeout: float | None = None) -> str:
        return (await self.search_with_meta(query, timeout=timeout)).text

    async def search_with_meta(self, query: str, *, timeout: float | None = None) -> SearchOutcome:
        for index, provider in enumerate(self.providers):
            try:
                outcome = await provider.search_with_meta(query, fallback=index > 0, timeout=timeout)
            except (MissingProviderKey, TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                if index + 1 == len(self.providers):
                    raise
                detail = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                log.warning(
                    "search provider %s on %s failed (%s); trying the next provider",
                    provider.usage_model, provider.address, detail,
                )
                continue
            return SearchOutcome(
                text=outcome.text,
                provider=outcome.provider,
                label=outcome.label,
                host=outcome.host,
                result_count=outcome.result_count,
                index=index,
            )
        raise RuntimeError("no search providers configured")


def routed_search_client(
    config: Config, http: httpx.AsyncClient, usage: UsageRecorder | None = None,
) -> SearchRouter | None:
    endpoints = config.providers("search")
    if not endpoints:
        return None
    providers = [
        SearchClient(
            endpoint,
            config.secret(endpoint.api_key_env) if endpoint.api_key_env else None,
            http,
            usage=usage,
        )
        for endpoint in endpoints
    ]
    router = SearchRouter(providers[0], *providers[1:])
    return router if router.available else None
