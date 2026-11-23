"""Model lists fetched from each provider for the dashboard's model picker (D8), with reasoning efforts (D9).

OpenRouter's `/models` says which models reason (`supported_parameters`); any other OpenAI-compatible `/models`
lists IDs only, so whether those reason is unknown. Lists are cached in the store's kv table for a day, keyed by
the provider's address and key variable, so a role slot finds its list without knowing the provider's name.
Fetching is a dashboard action, never on the reply path: one GET, no redirects, a timeout and a size cap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass

import httpx

from dom.config import EFFORT_PATTERN
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN

LIST_TTL = 24 * 60 * 60
LIST_TIMEOUT = 10
MAX_LIST_BYTES = 8 * 1024 * 1024
MAX_MODELS = 3000
MAX_ID = 200
MAX_NAME = 120
# Offered when a model reasons but its provider names no levels, and accepted when nothing is known about the model.
DEFAULT_EFFORTS = ("low", "medium", "high")
KNOWN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


class ListUnavailable(Exception):
    """The provider's model list could not be fetched; the message is safe to show."""


@dataclass(frozen=True)
class ListedModel:
    id: str
    name: str
    reasoning: bool | None  # None: the provider doesn't say
    efforts: tuple[str, ...]


@dataclass(frozen=True)
class ModelList:
    models: tuple[ListedModel, ...]
    fetched_at: float


def cache_key(base_url: str, api_key_env: str | None) -> str:
    digest = hashlib.sha256(f"{base_url}\n{api_key_env or ''}".encode()).hexdigest()
    return f"model-list:{digest}"


def _usable_id(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= MAX_ID
        and all(char.isprintable() and not char.isspace() for char in value)
        and not any(pattern.search(value) for pattern in (API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN))
    )


def _efforts(entry: dict, reasoning: bool | None) -> tuple[str, ...]:
    listed = entry.get("supported_reasoning_efforts")
    if isinstance(listed, list):
        names = tuple(dict.fromkeys(item for item in listed if isinstance(item, str) and re.fullmatch(EFFORT_PATTERN, item)))
        if names:
            return names
    return DEFAULT_EFFORTS if reasoning is not False else ()


def parse(payload: object) -> tuple[ListedModel, ...]:
    """The `data` array of an OpenAI-style list; entries without a usable ID are skipped."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise ListUnavailable("The provider's model list was not in the expected format.")
    models: dict[str, ListedModel] = {}
    for entry in data:
        if not isinstance(entry, dict) or not _usable_id(entry.get("id")):
            continue
        params = entry.get("supported_parameters")
        reasoning = bool({"reasoning", "reasoning_effort", "include_reasoning"} & set(params)) if isinstance(params, list) else None
        name = entry.get("name")
        name = " ".join(name.split())[:MAX_NAME] if isinstance(name, str) and name.strip() else entry["id"]
        models.setdefault(entry["id"], ListedModel(entry["id"], name, reasoning, _efforts(entry, reasoning)))
        if len(models) >= MAX_MODELS:
            break
    return tuple(sorted(models.values(), key=lambda model: model.id.lower()))


async def fetch(http: httpx.AsyncClient, base_url: str, key: str | None) -> tuple[ListedModel, ...]:
    """GET {base_url}/models. The caller has already checked that `key` may go to this address."""
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with asyncio.timeout(LIST_TIMEOUT):
            async with http.stream(
                "GET", url, headers=headers, follow_redirects=False, timeout=httpx.Timeout(LIST_TIMEOUT),
            ) as response:
                if response.is_redirect:
                    raise ListUnavailable("The provider redirected the list request. Use its final address.")
                if response.status_code >= 400:
                    raise ListUnavailable(f"The provider returned HTTP {response.status_code} for its model list.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_LIST_BYTES:
                        raise ListUnavailable("The provider's model list is too large.")
    except (TimeoutError, httpx.TimeoutException):
        raise ListUnavailable("The model list request timed out.") from None
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        raise ListUnavailable("Could not reach the provider.") from None
    try:
        return parse(json.loads(body))
    except ValueError:
        raise ListUnavailable("The provider's model list was not valid JSON.") from None


def listable(kind: str) -> bool:
    """Kinds with a `/models` list. Jev's decisions API and the search services don't list models."""
    return kind in ("openai", "openrouter")


async def cached(store, base_url: str, api_key_env: str | None, *, fresh_only: bool = True) -> ModelList | None:
    raw = await store.kv_get(cache_key(base_url, api_key_env))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        listed = ModelList(
            tuple(ListedModel(item["id"], item["name"], item["reasoning"], tuple(item["efforts"])) for item in data["models"]),
            float(data["fetched_at"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if fresh_only and time.time() - listed.fetched_at > LIST_TTL:
        return None
    return listed


async def remember(store, base_url: str, api_key_env: str | None, models: tuple[ListedModel, ...]) -> ModelList:
    listed = ModelList(models, time.time())
    payload = {"fetched_at": listed.fetched_at, "models": [asdict(model) for model in models]}
    await store.kv_set(cache_key(base_url, api_key_env), json.dumps(payload, separators=(",", ":")))
    return listed


def effort_error(listed: ModelList | None, model: str, effort: str | None) -> str | None:
    """Why `effort` can't be used with `model`, checked against the fetched list when it knows the model."""
    if effort is None:
        return None
    found = next((item for item in listed.models if item.id == model), None) if listed else None
    if found is None or found.reasoning is None:
        if effort not in KNOWN_EFFORTS:
            return f"Choose a reasoning effort: {', '.join(DEFAULT_EFFORTS)}, or the provider default."
        return None
    if found.reasoning is False:
        return "This model doesn't take a reasoning effort. Use the provider default."
    if effort not in found.efforts:
        return f"This model takes {', '.join(found.efforts)}, or the provider default."
    return None
