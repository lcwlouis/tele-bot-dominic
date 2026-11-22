"""OpenRouter web search, with results marked as untrusted tool data."""

from __future__ import annotations

import asyncio
import re
from xml.sax.saxutils import escape

import httpx

from dom import turn_log
from dom.config import ModelEndpoint
from dom.llm import openrouter_host
from dom.usage import UsageRecorder, check_model_allowed

TIMEOUT_SECONDS = 30
MAX_RESULT = 2000
_XML_ILLEGAL = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_ATTRIBUTE_ESCAPES = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


def _escaped_prefix(value: str, limit: int, entities: dict[str, str] | None = None) -> str:
    text = escape(_XML_ILLEGAL.sub("", value[:limit]), entities or {})[:limit]
    head, amp, tail = text.rpartition("&")
    return head if amp and ";" not in tail else text


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


class SearchClient:
    def __init__(
        self, endpoint: ModelEndpoint, api_key: str, http: httpx.AsyncClient, *, usage: UsageRecorder | None = None,
    ):
        self.endpoint, self.api_key, self.http, self.usage = endpoint, api_key, http, usage
        self.host = httpx.URL(endpoint.base_url).host

    async def search(self, query: str) -> str:
        await check_model_allowed()
        messages = [{
            "role": "user",
            "content": f"Search the web and answer concisely with the key facts: {query}",
        }]
        plugins = [{"id": "web", "max_results": 3}]
        body: dict = {"model": self.endpoint.model, "messages": messages, "plugins": plugins}
        if openrouter_host(self.host):
            body["provider"] = {"zdr": True}
        with turn_log.model_call(self.endpoint.model, "search", messages, plugins) as call:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                response = await self.http.post(
                    f"{self.endpoint.base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=TIMEOUT_SECONDS,
                )
        if call is not None:
            call.received(response)
        if self.usage is not None:
            await self.usage.record_response(response, "search", self.endpoint.model, self.host)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        answer = message["content"]
        if not isinstance(answer, str):
            raise ValueError("search response has no answer text")

        header = f'<search_results untrusted="true" query="{_escaped_prefix(query, 500, _ATTRIBUTE_ESCAPES)}">'
        footer = "</search_results>"
        urls = _urls(message)
        sources = _escaped_prefix("\nSources: " + ", ".join(urls), 750) if urls else ""
        room = MAX_RESULT - len(header) - len(footer) - len(sources)
        return header + _escaped_prefix(answer, room) + sources + footer
