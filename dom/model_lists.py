"""Model lists fetched from each provider for the dashboard's model picker (D8), with reasoning efforts (D9).

OpenRouter's `/models` says which models reason (`supported_parameters`); any other OpenAI-compatible `/models`
lists IDs only, so whether those reason is unknown. A decisions provider (triage) lists OpenRouter's decision models,
`/api/v1/models?output_modalities=decisions` on its origin. Lists are cached in the store's kv table for a day, keyed
by the provider's address, key variable and type, so a role slot finds its list without knowing the provider's name.
Fetching is a dashboard action, never on the reply path: one GET, no redirects, a timeout and a size cap. A
provider could echo a credential back in a model's fields, so an entry holding any configured secret value is dropped
before it is cached or shown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Collection
from dataclasses import asdict, dataclass

import httpx

from dom import anthropic_api
from dom.config import EFFORT_PATTERN, Config
from dom.secret_patterns import API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN

LIST_TTL = 24 * 60 * 60
LIST_TIMEOUT = 10
MAX_LIST_BYTES = 8 * 1024 * 1024
MAX_MODELS = 3000
MAX_ID = 200
MAX_NAME = 120
# Shorter values aren't credentials, and matching them would drop ordinary model names.
MIN_SECRET = 8
# Offered when a model reasons but its provider names no levels, and accepted when nothing is known about the model.
DEFAULT_EFFORTS = ("low", "medium", "high")
# Every level a provider is known to take, in order from none to most. OpenRouter lists each model's own subset.
KNOWN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ListUnavailable(Exception):
    """The provider's model list could not be fetched; the message is safe to show."""


@dataclass(frozen=True)
class ListedModel:
    id: str
    name: str
    reasoning: bool | None  # None: the provider doesn't say
    efforts: tuple[str, ...]
    default_effort: str | None = None  # what the provider uses when Dom sends no effort; "none" when it doesn't reason by default
    input: tuple[str, ...] | None = None  # input types the provider lists (text, image, audio, video); None: it doesn't say


@dataclass(frozen=True)
class ModelList:
    models: tuple[ListedModel, ...]
    fetched_at: float


def cache_key(base_url: str, api_key_env: str | None, type_: str = "chat") -> str:
    identity = f"{base_url}\n{api_key_env or ''}" + ("" if type_ == "chat" else f"\n{type_}")
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"model-list:v3:{digest}"  # v3: input types from OpenRouter's `architecture` object


def secret_values(config: Config) -> frozenset[str]:
    """The values of every key variable config.toml names, and the fixed credentials Dom reads, that are set."""
    found: set[str] = set()
    for name in config.secret_names():
        try:
            value = config.secret(name)
        except UnicodeDecodeError:  # malformed .env: still redact the process environment's value
            logging.getLogger("dom.model_lists").warning(".env is not valid UTF-8; redacting environment keys only")
            value = os.environ.get(name)
        if value and len(value) >= MIN_SECRET:
            found.add(value)
    return frozenset(found)


def _holds_secret(texts: Collection[str], secrets: Collection[str]) -> bool:
    return any(secret in text for secret in secrets for text in texts)


def _usable_id(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= MAX_ID
        and all(char.isprintable() and not char.isspace() for char in value)
        and not any(pattern.search(value) for pattern in (API_KEY, BEARER_TOKEN, TELEGRAM_BOT_TOKEN))
    )


def _effort_name(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(EFFORT_PATTERN, value) else None


def _efforts(entry: dict, reasoning: bool | None) -> tuple[tuple[str, ...], str | None]:
    """The model's levels and its default, from OpenRouter's `reasoning` object:
    {"supported_efforts": [...], "default_effort": "high", "default_enabled": true, "mandatory": false}."""
    details = entry.get("reasoning")
    details = details if isinstance(details, dict) else {}
    listed = details.get("supported_efforts")
    names = tuple(dict.fromkeys(name for item in listed if (name := _effort_name(item)))) if isinstance(listed, list) else ()
    rank = {name: index for index, name in enumerate(KNOWN_EFFORTS)}
    names = tuple(sorted(names, key=lambda name: rank.get(name, len(rank))))
    default = "none" if details.get("default_enabled") is False else _effort_name(details.get("default_effort"))
    if names:
        return names, default
    return (DEFAULT_EFFORTS if reasoning is not False else ()), default


_INPUT_NAMES = ("text", "image", "audio", "video")


def _input_types(entry: dict) -> tuple[str, ...] | None:
    """The input types OpenRouter lists as `architecture.input_modalities`, or Anthropic `capabilities.image_input`."""
    architecture = entry.get("architecture")
    listed = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    if isinstance(listed, list):
        return tuple(name for name in _INPUT_NAMES if name in listed)
    capabilities = entry.get("capabilities")
    if isinstance(capabilities, dict) and isinstance(capabilities.get("image_input"), dict):
        return ("text", "image") if capabilities["image_input"].get("supported") else ("text",)
    return None


def parse(payload: object, secrets: Collection[str] = ()) -> tuple[ListedModel, ...]:
    """The `data` array of an OpenAI-style list; entries without a usable ID, or holding a secret value, are skipped."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise ListUnavailable("The provider's model list was not in the expected format.")
    models: dict[str, ListedModel] = {}
    for entry in data:
        if not isinstance(entry, dict) or not _usable_id(entry.get("id")):
            continue
        params = entry.get("supported_parameters")
        reasoning = bool({"reasoning", "reasoning_effort", "include_reasoning"} & set(params)) if isinstance(params, list) else None
        name = entry.get("name") or entry.get("display_name")
        name = " ".join(name.split())[:MAX_NAME] if isinstance(name, str) and name.strip() else entry["id"]
        efforts, default_effort = _efforts(entry, reasoning)
        model = ListedModel(entry["id"], name, reasoning, efforts, default_effort, _input_types(entry))
        if secrets and _holds_secret((json.dumps(entry, ensure_ascii=False), model.id, model.name, *model.efforts), secrets):
            continue
        models.setdefault(entry["id"], model)
        if len(models) >= MAX_MODELS:
            break
    return tuple(sorted(models.values(), key=lambda model: model.id.lower()))


async def fetch(
    http: httpx.AsyncClient, base_url: str, key: str | None, *, secrets: Collection[str], path: str = "/models",
) -> tuple[ListedModel, ...]:
    """GET {base_url}{path} (OpenRouter keeps its image, speech and video models in their own lists). The caller has
    already checked that `key` may go to this address and that the address is allowed; entries holding any of
    `secrets` (or `key`) are dropped."""
    url = base_url.rstrip("/") + path
    try:
        is_anthropic = anthropic_api.anthropic_host(httpx.URL(url).host)
    except httpx.InvalidURL:
        is_anthropic = False
    headers = anthropic_api.headers(key) if is_anthropic else ({"Authorization": f"Bearer {key}"} if key else {})
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
        return parse(json.loads(body), {*secrets, *([key] if key else [])})
    except ValueError:
        raise ListUnavailable("The provider's model list was not valid JSON.") from None


DECISIONS_LIST = "/api/v1/models?output_modalities=decisions"


def list_location(kind: str, type_: str, base_url: str) -> tuple[str, str] | None:
    """Where a provider's model list is, as (base, path) for fetch(), or None when it has none: chat providers list
    `{base_url}/models`; OpenRouter's decisions models are on its origin's v1 API; media providers and the search
    services list nothing here (studio backups fetch each media kind's list themselves)."""
    if type_ == "chat" and kind in ("openai", "openrouter", "anthropic"):
        return base_url, "/models"
    if type_ == "decisions" and kind == "openrouter":
        try:
            url = httpx.URL(base_url)
        except httpx.InvalidURL:
            return None
        host = (url.host or "").rstrip(".")
        if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
            return str(url.copy_with(path="/", query=None, fragment=None)).rstrip("/"), DECISIONS_LIST
    return None


def listable(kind: str, type_: str = "chat", base_url: str = "") -> bool:
    return list_location(kind, type_, base_url) is not None


async def cached(
    store, base_url: str, api_key_env: str | None, *, fresh_only: bool = True, secrets: Collection[str] = (),
    type_: str = "chat",
) -> ModelList | None:
    """The cached list, without entries holding any of `secrets` (a list cached before a key value was set)."""
    raw = await store.kv_get(cache_key(base_url, api_key_env, type_))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        listed = ModelList(
            tuple(
                ListedModel(
                    item["id"], item["name"], item["reasoning"], tuple(item["efforts"]), item.get("default_effort"),
                    tuple(item["input"]) if isinstance(item.get("input"), list) else None,
                )
                for item in data["models"]
            ),
            float(data["fetched_at"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if fresh_only and time.time() - listed.fetched_at > LIST_TTL:
        return None
    if secrets:
        kept = tuple(item for item in listed.models if not _holds_secret((item.id, item.name, *item.efforts), secrets))
        listed = ModelList(kept, listed.fetched_at)
    return listed


async def remember(
    store, base_url: str, api_key_env: str | None, models: tuple[ListedModel, ...], *, type_: str = "chat",
) -> ModelList:
    listed = ModelList(models, time.time())
    payload = {"fetched_at": listed.fetched_at, "models": [asdict(model) for model in models]}
    await store.kv_set(cache_key(base_url, api_key_env, type_), json.dumps(payload, separators=(",", ":")))
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
