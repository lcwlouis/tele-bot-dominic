"""Owner settings JSON contract: the studio's cloud backups (`[[studio.<chain>_fallbacks]]`).

POST /api/studio/save adds a backup to a chain (index null) or replaces the one at index, after a test that generates
no media: an OpenRouter backup's key must be accepted by OpenRouter's key endpoint and its model must be in the list of
its output kind; a direct Gemini backup's model must answer a models GET. A Gemini backup is saved only with paid=true.
POST /api/studio/remove drops one backup and POST /api/studio/move moves one a place up; neither reaches anything new,
so neither tests. POST /api/studio/models returns OpenRouter's public list for a backup kind, cached for a day. Every
POST takes the settings revision and keeps dashboard/web/model_settings.py's rules: one config write at a time, atomic
with config.toml.bak, and a key variable never reaches a host config.toml doesn't already pair it with. Each returns
ModelSettings (the list route returns ModelListView), and the running config takes the new chains at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dom import model_lists
from dom.config import CHAIN_PROVIDERS, MEDIA_BASE_URLS, MEDIA_KEY_RULE, ProviderEndpoint, StudioChain, StudioFallback, StudioProvider
from dom.web.model_settings import (
    _CONFIG_WRITE,
    ENV_NAME,
    MAX_FALLBACKS,
    MISSING_KEY,
    REVISION,
    KeyPairing,
    ModelSettings,
    Snapshot,
    TestResult,
    _check_keys,
    _check_revision,
    _input,
    _looks_like_secret,
    _redact,
    _settings,
    _snapshot,
    _write,
    set_backups,
    validated,
)
from dom.studio.cloud import VideoUnsupported, video_models, video_plan, video_rate
from dom.web.provider_settings import ListedModelView, ModelListView, _http
from dom.web.shared import owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
PREFIX = "/api/studio"
JSON_POSTS = {f"{PREFIX}/save", f"{PREFIX}/remove", f"{PREFIX}/move", f"{PREFIX}/test", f"{PREFIX}/models"}
TEST_TIMEOUT = 8
# OpenRouter lists each output kind's models separately; every list is public.
MODEL_LISTS = {"openrouter_image": "/images/models", "openrouter_tts": "/audio/speech/models",
               "openrouter_video": "/videos/models"}
MEDIA_NEEDS_KEY = "A media provider needs a key variable."
UNPAID_GEMINI = ("Tick the Paid Services box to use a direct Gemini key. Google may use audio made on an unpaid key to "
                 "improve its products, with human review, so Dom never runs Gemini without it.")


class BackupInput(StudioFallback):
    @field_validator("model", "api_key_env")
    @classmethod
    def _no_credentials(cls, value: str) -> str:
        if _looks_like_secret(value):
            raise ValueError("Enter a model ID and a key variable name, not a key.")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _key_env(cls, value: str) -> str:
        if not ENV_NAME.fullmatch(value):
            raise ValueError("Enter an environment variable name in capitals, such as OPENROUTER_MEDIA_API_KEY.")
        return value


class BackupSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    chain: StudioChain
    index: int | None = Field(None, ge=0, le=64)  # null adds the backup at the end
    backup: BackupInput


class BackupRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    chain: StudioChain
    index: int = Field(ge=0, le=64)


class BackupTest(BackupRef):
    pass


class BackupListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    provider: StudioProvider
    refresh: bool = False


def _chain(snapshot: Snapshot, chain: StudioChain) -> list[StudioFallback]:
    return list(getattr(snapshot.config.studio, f"{chain}_fallbacks"))


def _with_chain(current: Snapshot, chain: StudioChain, backups: list[StudioFallback]) -> Snapshot:
    """The settings with one studio chain replaced, validated (key pairing included) but not written."""
    document = tomlkit.parse(current.text)
    set_backups(document, chain, backups)
    return validated(current, document)


def _check_backup(current: Snapshot, edit: BackupSave) -> StudioFallback:
    """The backup as it will be saved, after every check that needs no network."""
    _check_revision(current, edit.revision)
    backup = StudioFallback(**edit.backup.model_dump())
    if backup.provider not in CHAIN_PROVIDERS[edit.chain]:
        raise HTTPException(422, "That provider can't make this kind of media.")
    if backup.provider == "gemini_tts" and not backup.paid:
        raise HTTPException(422, UNPAID_GEMINI)
    if backup.api_key_env in current.config.chat_keys():
        raise HTTPException(422, MEDIA_KEY_RULE.format(name=backup.api_key_env))
    backups = _chain(current, edit.chain)
    if edit.index is None and len(backups) >= MAX_FALLBACKS:
        raise HTTPException(422, f"A chain takes at most {MAX_FALLBACKS} backups.")
    if edit.index is not None and edit.index >= len(backups):
        raise HTTPException(404, "There is no studio backup at that position. Reload settings and try again.")
    try:
        _check_keys(current.config, [ProviderEndpoint(base_url=backup.base_url, api_key_env=backup.api_key_env)])
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    return backup


def _saved(current: Snapshot, edit: BackupSave) -> Snapshot:
    backup = _check_backup(current, edit)
    backups = _chain(current, edit.chain)
    if edit.index is None:
        backups.append(backup)
    else:
        backups[edit.index] = backup
    return _with_chain(current, edit.chain, backups)


def _removed(current: Snapshot, ref: BackupRef) -> Snapshot:
    _check_revision(current, ref.revision)
    backups = _chain(current, ref.chain)
    if ref.index >= len(backups):
        raise HTTPException(404, "There is no studio backup at that position. Reload settings and try again.")
    del backups[ref.index]
    return _with_chain(current, ref.chain, backups)


def _moved(current: Snapshot, ref: BackupRef) -> Snapshot:
    _check_revision(current, ref.revision)
    backups = _chain(current, ref.chain)
    if not 1 <= ref.index < len(backups):
        raise HTTPException(404, "There is no studio backup at that position. Reload settings and try again.")
    backups[ref.index - 1], backups[ref.index] = backups[ref.index], backups[ref.index - 1]
    return _with_chain(current, ref.chain, backups)


async def _commit(app: App, change: Callable[[Snapshot], Snapshot], failure: str) -> Snapshot:
    """Write one studio change and hand the running config the new chains. Backups are read on each use, so no route
    is rebuilt. One config write at a time, off the loop; a cancelled request still finishes the swap."""

    async def run() -> Snapshot:
        async with _CONFIG_WRITE:
            try:
                saved = await asyncio.to_thread(lambda: _apply(change, _snapshot(app.config.home)))
            except (OSError, ValueError):
                raise HTTPException(409, failure) from None
            app.config = app.config.model_copy(update={"studio": saved.config.studio})
            return saved

    return await asyncio.shield(asyncio.ensure_future(run()))


def _apply(change: Callable[[Snapshot], Snapshot], current: Snapshot) -> Snapshot:
    saved = change(current)
    _write(current, saved)
    return saved


async def _list(app: App, provider: str, *, refresh: bool) -> model_lists.ModelList:
    base = StudioFallback.model_construct(provider=provider).base_url
    address = base + MODEL_LISTS[provider]
    if provider == "openrouter_video":
        address += "?input=portrait-and-inline-audio"
    if not refresh and (listed := await model_lists.cached(app.store, address, None)):
        return listed
    secrets = await asyncio.to_thread(model_lists.secret_values, app.config)
    if provider == "openrouter_video":
        try:
            entries = await video_models(_http(app), base)
            models = model_lists.parse([entry for model, entry in entries.items() if model == entry["id"]], secrets)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise model_lists.ListUnavailable("Could not load OpenRouter's avatar models.") from exc
    else:
        models = await model_lists.fetch(_http(app), base, None, secrets=secrets, path=MODEL_LISTS[provider])
    return await model_lists.remember(app.store, address, None, models)


async def _get(app: App, url: str, headers: dict) -> httpx.Response:
    async with asyncio.timeout(TEST_TIMEOUT):
        response = await _http(app).get(url, headers=headers, follow_redirects=False, timeout=TEST_TIMEOUT)
    response.raise_for_status()
    return response


async def probe(app: App, config, backup: StudioFallback) -> TestResult:
    """Check a backup without generating media: the key is accepted and the model can make this kind of media."""
    start = monotonic()
    key = None
    try:
        _check_keys(config, [ProviderEndpoint(base_url=backup.base_url, api_key_env=backup.api_key_env)])
        key = await asyncio.to_thread(config.secret, backup.api_key_env)
        if backup.provider == "openrouter_video":
            async with asyncio.timeout(TEST_TIMEOUT):
                entry = (await video_models(_http(app), backup.base_url)).get(backup.model)
            if entry is None:
                raise VideoUnsupported(f"{backup.model} isn't a supported lip-sync avatar. "
                                       "Pick Avatar 6 Turbo or Avatar 6, which take a portrait and an audio track.")
            plan = video_plan(entry)
            price = video_rate(entry, plan)
            reply = f"The key works. Notes use {plan['resolution']} video (square Telegram crop) at ${price:g} per second."
        if not key:
            return TestResult(ok=False, latency_ms=0, error=MISSING_KEY)
        if backup.provider == "gemini_tts":
            await _get(app, f"{backup.base_url}/models/{backup.model}", {"x-goog-api-key": key})
            reply = "The key works and the model exists."
        else:
            await _get(app, backup.base_url + "/key", {"Authorization": f"Bearer {key}"})
            if backup.provider == "openrouter_video":
                return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=reply)
            listed = await _list(app, backup.provider, refresh=False)
            if not any(item.id == backup.model for item in listed.models):
                listed = await _list(app, backup.provider, refresh=True)
            if not any(item.id == backup.model for item in listed.models):
                kind = {"openrouter_image": "image", "openrouter_tts": "speech"}[backup.provider]
                return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000),
                                  error=f"OpenRouter doesn't list that model for {kind}. Pick one from the list.")
            reply = "The key works and OpenRouter lists the model."
    except (KeyPairing, VideoUnsupported) as exc:
        error = str(exc)
    except model_lists.ListUnavailable as exc:
        error = str(exc)
    except (TimeoutError, httpx.TimeoutException):
        error = "Provider test timed out."
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        error = ("The provider refused the key." if status in (401, 403)
                 else "The provider doesn't know that model." if status == 404
                 else f"Provider returned HTTP {status}.")
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        error = "Could not reach the provider."
    except (ValueError, KeyError, TypeError, AttributeError):
        error = "The provider's model list was not in the expected format."
    else:
        return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=reply)
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=_redact(error, key))


async def probe_key(app: App, config, kind: str, api_key_env: str | None) -> TestResult:
    """Check a media provider's key without generating media: OpenRouter's key endpoint, or Gemini's model list."""
    start = monotonic()
    base = MEDIA_BASE_URLS[kind]
    key = None
    try:
        _check_keys(config, [ProviderEndpoint(base_url=base, api_key_env=api_key_env)])
        key = await asyncio.to_thread(config.secret, api_key_env) if api_key_env else None
        if not key:
            return TestResult(ok=False, latency_ms=0, error=MISSING_KEY if api_key_env else MEDIA_NEEDS_KEY)
        if kind == "gemini":
            await _get(app, f"{base}/models?pageSize=1", {"x-goog-api-key": key})
        else:
            await _get(app, base + "/key", {"Authorization": f"Bearer {key}"})
    except KeyPairing as exc:
        error = str(exc)
    except (TimeoutError, httpx.TimeoutException):
        error = "Provider test timed out."
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        error = "The provider refused the key." if status in (401, 403) else f"Provider returned HTTP {status}."
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        error = "Could not reach the provider."
    else:
        return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply="The key works.")
    return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=_redact(error, key))


@router.post(PREFIX + "/save")
async def save_backup(request: Request) -> ModelSettings:
    app = owner_app(request)
    edit = await _input(request, BackupSave)
    current = await asyncio.to_thread(_snapshot, app.config.home)
    backup = _check_backup(current, edit)
    try:  # built first, so the key pairing is checked on the routes as they will be saved, before anything is sent
        await asyncio.to_thread(_saved, current, edit)
    except (OSError, ValueError):
        raise HTTPException(409, "Could not save the backup. Check config.toml and its permissions.") from None
    result = await probe(app, current.config, backup)
    if not result.ok and result.error != MISSING_KEY:  # a new key variable is filled in after saving
        raise HTTPException(422, result.error or "Backup test failed.")
    saved = await _commit(app, lambda fresh: _saved(fresh, edit),
                          "Could not save the backup. Check config.toml and its permissions.")
    return await asyncio.to_thread(_settings, app, saved)


@router.post(PREFIX + "/remove")
async def remove_backup(request: Request) -> ModelSettings:
    app = owner_app(request)
    ref = await _input(request, BackupRef)
    saved = await _commit(app, lambda current: _removed(current, ref),
                          "Could not remove the backup. Check config.toml and its permissions.")
    return await asyncio.to_thread(_settings, app, saved)


@router.post(PREFIX + "/move")
async def move_backup(request: Request) -> ModelSettings:
    """Move a backup one place up. Nothing new is reached, so nothing is tested; it applies at once."""
    app = owner_app(request)
    ref = await _input(request, BackupRef)
    saved = await _commit(app, lambda current: _moved(current, ref),
                          "Could not save the order. Check config.toml and its permissions.")
    return await asyncio.to_thread(_settings, app, saved)


@router.post(PREFIX + "/test")
async def test_backup(request: Request) -> TestResult:
    app = owner_app(request)
    ref = await _input(request, BackupTest)
    snapshot = await asyncio.to_thread(_snapshot, app.config.home)
    _check_revision(snapshot, ref.revision)
    backups = _chain(snapshot, ref.chain)
    if ref.index >= len(backups):
        raise HTTPException(404, "There is no studio backup at that position. Reload settings and try again.")
    return await probe(app, snapshot.config, backups[ref.index])


@router.post(PREFIX + "/models")
async def backup_models(request: Request) -> ModelListView:
    app = owner_app(request)
    query = await _input(request, BackupListRequest)
    snapshot = await asyncio.to_thread(_snapshot, app.config.home)
    _check_revision(snapshot, query.revision)
    if query.provider not in MODEL_LISTS:
        return ModelListView(listable=False, models=[])
    try:
        listed = await _list(app, query.provider, refresh=query.refresh)
    except model_lists.ListUnavailable as exc:
        raise HTTPException(502, str(exc)) from None
    return ModelListView(
        listable=True, fetched_at=listed.fetched_at,
        models=[ListedModelView(id=item.id, name=item.name, reasoning=None, efforts=[]) for item in listed.models],
    )
