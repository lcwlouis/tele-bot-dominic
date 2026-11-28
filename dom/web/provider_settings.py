"""Owner settings JSON contract: providers and their model lists (E56, D8).

POST /api/providers/save adds a provider (original null) or edits one (original: its current name) and returns
ModelSettings. A provider is a `[providers.<name>]` table: kind (the service), type (chat, media, decisions or
search, E70), base URL and key variable. Editing one that roles or studio backups use rewrites those slots' address
and key variable in the same atomic save, after testing each changed slot as it will be sent (model and reasoning
effort). POST /api/providers/remove refuses a provider a slot still uses. POST /api/providers/test tests the provider
through its first use, or when nothing uses it yet by fetching its model list (a media provider: by checking its key).
POST /api/providers/models returns the provider's model list (a decisions provider: OpenRouter's decision models),
cached for a day; refresh fetches it again. Every POST takes the settings revision, like dom/web/model_settings.py,
and keeps its rules: saves are tested first, written atomically with config.toml.bak, and a key variable never
reaches a host config.toml doesn't already pair it with.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dom import model_lists
from dom.config import (
    MEDIA_ADDRESS_RULE,
    MEDIA_BASE_URLS,
    PROVIDER_NAME_PATTERN,
    STUDIO_CHAINS,
    TYPE_KINDS,
    ProviderEndpoint,
    ProviderKind,
    ProviderType,
    _read_dotenv,
    default_type,
    set_dotenv,
)
from dom.llm import FallbackClient, MissingProviderKey
from dom.web.model_settings import (
    DECISION_ROLES,
    ENV_NAME,
    MISSING_KEY,
    REVISION,
    _CONFIG_WRITE,
    KeyPairing,
    ModelSettings,
    ProviderSetupView,
    ProviderUse,
    Snapshot,
    SETTINGS_ROLES,
    TestResult,
    _check_keys,
    _check_revision,
    _input,
    _looks_like_secret,
    _probe,
    _probe_search,
    _probe_tuple,
    _redact,
    _settings,
    _snapshot,
    _validate_url,
    chain,
    commit,
    kind_fits,
    provider_catalog,
    same_url,
    set_backups,
    set_role,
    validated,
)
from dom.web.shared import owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
PREFIX = "/api/providers"
JSON_POSTS = {f"{PREFIX}/save", f"{PREFIX}/remove", f"{PREFIX}/test", f"{PREFIX}/models", f"{PREFIX}/keys"}
PROTECTED_ENV_KEYS = frozenset({
    "DOM_MASTER_KEY", "TELEGRAM_BOT_TOKEN", "WEB_DEV_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
})


class ProviderSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    original: str | None = Field(None, max_length=40)
    name: str = Field(pattern=PROVIDER_NAME_PATTERN)
    kind: ProviderKind
    type: ProviderType
    base_url: str
    api_key_env: str | None = None

    @model_validator(mode="after")
    def _kind_fits_type(self) -> ProviderSave:
        if self.kind not in TYPE_KINDS[self.type]:
            raise ValueError("That service can't be this type of provider.")
        fixed = MEDIA_BASE_URLS.get(self.kind) if self.type == "media" else None
        if fixed and self.base_url.rstrip("/") != fixed:
            raise ValueError(MEDIA_ADDRESS_RULE.format(url=fixed))
        return self

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if value != value.strip() or "  " in value:
            raise ValueError("Use a provider name without leading, trailing or double spaces.")
        return value

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("api_key_env")
    @classmethod
    def _key_env(cls, value: str | None) -> str | None:
        if value is not None and (not ENV_NAME.fullmatch(value) or _looks_like_secret(value)):
            raise ValueError("Enter an environment variable name or leave it unset.")
        return value


class ProviderRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    name: str = Field(max_length=40)


class ModelListRequest(ProviderRef):
    refresh: bool = False


class ListedModelView(BaseModel):
    id: str
    name: str
    reasoning: bool | None
    efforts: list[str]
    default_effort: str | None = None


class ModelListView(BaseModel):
    listable: bool
    models: list[ListedModelView]
    fetched_at: float | None = None


def _find(catalog: list[ProviderSetupView], name: str) -> ProviderSetupView:
    found = next((item for item in catalog if item.name == name), None)
    if found is None:
        raise HTTPException(404, "There is no provider with that name. Reload settings and try again.")
    return found


STUDIO_LABELS = {"voice": "Voice notes", "image": "Photos", "video": "Video notes"}
NOT_TESTED = "Not tested: choose this provider for triage to test it."


def _use_label(use) -> str:
    if use.role in STUDIO_CHAINS:
        return f"{STUDIO_LABELS[use.role]} backup {use.index + 1}"
    role = {"fallback": "Fallback", "media": "Photos and voice", "search": "Web search"}.get(use.role, use.role.capitalize())
    return role if use.index == 0 else f"{role} fallback {use.index}"


def _slot(config, use):
    """The role endpoint or studio backup a use points at."""
    if use.role in STUDIO_CHAINS:
        return getattr(config.studio, f"{use.role}_fallbacks")[use.index]
    return chain(config, use.role)[use.index]


def _http(app: App) -> httpx.AsyncClient:
    return (app.client.primary if isinstance(app.client, FallbackClient) else app.client).http


def _check_edit(snapshot: Snapshot, edit: ProviderSave) -> ProviderSetupView | None:
    """The provider being edited (None when adding), after the checks that need no network."""
    _check_revision(snapshot, edit.revision)
    catalog = provider_catalog(snapshot.config)
    original = _find(catalog, edit.original) if edit.original is not None else None
    if any(item is not original and item.name.lower() == edit.name.lower() for item in catalog):
        raise HTTPException(409, "Another provider already has that name.")
    twin = next((item for item in catalog if item is not original and same_url(item.base_url, edit.base_url)
                 and (item.api_key_env, item.type) == (edit.api_key_env, edit.type)), None)
    if twin is not None:
        raise HTTPException(409, f"That address and key variable are already saved as {twin.name}.")
    if original is not None:
        config = snapshot.config
        for use in original.uses:
            if not kind_fits(edit.kind, edit.type, use.role, _slot(config, use)):
                raise HTTPException(409, f"{_use_label(use)} can't use that kind of provider. Change that role first.")
        if not edit.api_key_env and any(use.role in STUDIO_CHAINS for use in original.uses):
            raise HTTPException(422, "A studio backup needs a key variable. Name one for this provider.")
    try:
        _check_keys(snapshot.config, [ProviderEndpoint(base_url=edit.base_url, api_key_env=edit.api_key_env)])
    except KeyPairing as exc:
        raise HTTPException(422, str(exc)) from None
    return original


def _moved_backup(backup, api_key_env: str | None):
    """A backup on a new key variable. A direct Gemini backup's paid confirmation was for the old key's Google
    project, so it is cleared: the backup is skipped until the owner confirms Paid Services for the new key."""
    cleared = backup.provider == "gemini_tts" and api_key_env != backup.api_key_env
    update = {"api_key_env": api_key_env} | ({"paid": False} if cleared else {})
    return backup.model_copy(update=update)


def _apply(current: Snapshot, edit: ProviderSave) -> Snapshot:
    """The settings with the provider saved, validated (with the key pairing checked on the effective routes) but not
    written."""
    original = _check_edit(current, edit)
    if original is not None and original.saved and (
            original.name, original.kind, original.type, original.base_url, original.api_key_env) == (
            edit.name, edit.kind, edit.type, edit.base_url, edit.api_key_env):
        return current  # nothing changed: keep the table as written (a legacy one stays readable by older releases)
    document = tomlkit.parse(current.text)
    if "providers" not in document:
        document["providers"] = tomlkit.table(is_super_table=True)
    providers = document["providers"]
    if original is not None and original.saved:
        providers.pop(original.name, None)
    entry = tomlkit.table()
    entry["kind"] = edit.kind
    if edit.type != default_type(edit.kind):  # chat and search tables stay readable by releases before types
        entry["type"] = edit.type
    entry["base_url"] = edit.base_url
    if edit.api_key_env:
        entry["api_key_env"] = edit.api_key_env
    document["providers"][edit.name] = entry
    if original is not None and (original.base_url, original.api_key_env) != (edit.base_url, edit.api_key_env):
        # Every slot on the old address moves with the provider; each changed role or backup chain is written out whole.
        config = current.config
        for role in dict.fromkeys(use.role for use in original.uses):
            if role in STUDIO_CHAINS:  # a backup's address is fixed by its service, so only its key variable moves
                backups = [
                    _moved_backup(backup, edit.api_key_env)
                    if (same_url(backup.base_url, original.base_url) and backup.api_key_env == original.api_key_env)
                    else backup
                    for backup in getattr(config.studio, f"{role}_fallbacks")
                ]
                set_backups(document, role, backups)
                continue
            slots = [
                endpoint.model_copy(update={"base_url": edit.base_url, "api_key_env": edit.api_key_env})
                if same_url(endpoint.base_url, original.base_url)
                and (endpoint.api_key_env or None) == original.api_key_env
                and kind_fits(original.kind, original.type, role, endpoint)
                else endpoint
                for endpoint in chain(config, role)
            ]
            set_role(document, role, slots)
    return validated(current, document)


class ListRefused(ValueError):
    """The list request would go somewhere it may not: a disallowed address or an unpaired key."""


async def _fetch_list(app: App, config, setup: ProviderSetupView) -> tuple[model_lists.ListedModel, ...]:
    """Fetch a provider's model list with the checks a role's probe makes: an allowed address (a hand-written table
    may name one the dashboard refuses) and a key variable paired with it. Raises ListRefused, MissingProviderKey or
    model_lists.ListUnavailable (with any key redacted)."""
    try:
        _validate_url(setup.base_url)
        _check_keys(config, [ProviderEndpoint(base_url=setup.base_url, api_key_env=setup.api_key_env)])
    except KeyPairing as exc:
        raise ListRefused(str(exc)) from None
    except ValueError:
        raise ListRefused("This provider's address isn't allowed. Fix it on the Providers tab.") from None
    key = config.secret(setup.api_key_env) if setup.api_key_env else None
    if setup.api_key_env and not key:
        raise MissingProviderKey
    location = model_lists.list_location(setup.kind, setup.type, setup.base_url)
    if location is None:
        raise ListRefused("This provider doesn't list its models.")
    base, path = location
    try:
        models = await model_lists.fetch(_http(app), base, key, secrets=model_lists.secret_values(config), path=path)
    except model_lists.ListUnavailable as exc:
        raise model_lists.ListUnavailable(_redact(str(exc), key)) from None
    if setup.type == "decisions":  # a decision model answers questions; it takes no reasoning effort
        models = tuple(replace(model, reasoning=False, efforts=(), default_effort=None) for model in models)
    return models


async def _probe_setup(app: App, config, setup: ProviderSetupView) -> TestResult:
    """Test a provider: through its first use when a slot uses it, else by fetching its model list (a search for a
    search service, a key check for a media provider)."""
    from dom.web import studio_settings  # it imports this module

    http = _http(app)
    if setup.uses:
        use = setup.uses[0]
        if use.role in STUDIO_CHAINS:
            backup = _slot(config, use).model_copy(update={"api_key_env": setup.api_key_env})
            return await studio_settings.probe(app, config, backup)
        endpoint = chain(config, use.role)[use.index].model_copy(
            update={"base_url": setup.base_url, "api_key_env": setup.api_key_env},
        )
        return await _probe(app, http, config, endpoint, role=use.role)
    if setup.type == "search":
        endpoint = ProviderEndpoint(base_url=setup.base_url, api_key_env=setup.api_key_env, provider=setup.kind)
        return await _probe_search(app, http, config, endpoint)
    if setup.type == "media":
        return await studio_settings.probe_key(app, config, setup.kind, setup.api_key_env)
    start = monotonic()
    if not setup.listable:
        return TestResult(ok=False, latency_ms=0, error=NOT_TESTED)
    try:
        models = await _fetch_list(app, config, setup)
    except ListRefused as exc:
        return TestResult(ok=False, latency_ms=0, error=str(exc))
    except MissingProviderKey:
        return TestResult(ok=False, latency_ms=0, error=MISSING_KEY)
    except model_lists.ListUnavailable as exc:
        return TestResult(ok=False, latency_ms=round((monotonic() - start) * 1000), error=str(exc))
    await model_lists.remember(app.store, setup.base_url, setup.api_key_env, models, type_=setup.type)
    return TestResult(ok=True, latency_ms=round((monotonic() - start) * 1000), reply=f"Lists {len(models)} models")


def _changed_slots(current: Snapshot, saved: Snapshot) -> list[tuple[str, int, ProviderEndpoint]]:
    """Each role slot that will send something not already sent: a new address, key variable, model or effort."""
    changed = []
    for role in SETTINGS_ROLES:
        before = {_probe_tuple(role, endpoint) for endpoint in chain(current.config, role)}
        changed += [(role, index, endpoint) for index, endpoint in enumerate(chain(saved.config, role))
                    if _probe_tuple(role, endpoint) not in before]
    return changed


def _changed_backups(current: Snapshot, saved: Snapshot) -> list[tuple[str, int, object]]:
    """Each studio backup that moved to a key variable it didn't use before."""
    changed = []
    for studio_chain in STUDIO_CHAINS:
        before = set(getattr(current.config.studio, f"{studio_chain}_fallbacks"))
        changed += [(studio_chain, index, backup)
                    for index, backup in enumerate(getattr(saved.config.studio, f"{studio_chain}_fallbacks"))
                    if backup not in before]
    return changed


@router.post(PREFIX + "/save")
async def save_provider(request: Request) -> ModelSettings:
    app = owner_app(request)
    edit = await _input(request, ProviderSave)
    current = _snapshot(app.config.home)
    original = _check_edit(current, edit)
    # Built first, so the key pairing is checked on the routes as they will be saved, before anything is sent.
    try:
        candidate = await asyncio.to_thread(_apply, current, edit)
    except (OSError, ValueError):
        raise HTTPException(409, "Could not save the provider. Check config.toml and its permissions.") from None
    from dom.web import studio_settings  # it imports this module

    changed = original is None or (original.base_url, original.api_key_env, original.kind, original.type) != (
        edit.base_url, edit.api_key_env, edit.kind, edit.type)
    slots = [*_changed_slots(current, candidate), *_changed_backups(current, candidate)]
    if slots:  # every moved slot, as it will be sent
        results = await asyncio.gather(*(
            studio_settings.probe(app, current.config, endpoint) if role in STUDIO_CHAINS
            else _probe(app, _http(app), current.config, endpoint, role=role)
            for role, _, endpoint in slots))
        failures = [
            f"{_use_label(ProviderUse(role=role, index=index))}: {result.error or 'Provider test failed.'}"
            for (role, index, _), result in zip(slots, results)
            if not result.ok and result.error != MISSING_KEY  # a new key variable is filled in after saving
        ]
        if failures:
            raise HTTPException(422, f"{edit.name}: {'; '.join(failures)}")
    elif changed and (original is None or not original.uses):  # a rename alone sends nothing anywhere
        setup = ProviderSetupView(
            name=edit.name, kind=edit.kind, type=edit.type, base_url=edit.base_url, api_key_env=edit.api_key_env,
            key_set=False, is_local=False, saved=True, uses=[],
            listable=model_lists.listable(edit.kind, edit.type, edit.base_url),
        )
        result = await _probe_setup(app, current.config, setup)
        # A new key variable is filled in after saving, and a decisions endpoint without a list is tested once triage
        # uses it.
        # An unused media provider may be saved before its key variable is named; a backup still needs one.
        if not result.ok and result.error not in (MISSING_KEY, NOT_TESTED, studio_settings.MEDIA_NEEDS_KEY):
            raise HTTPException(422, f"{edit.name}: {result.error or 'Provider test failed.'}")
    saved = await commit(app, lambda fresh: _apply(fresh, edit),
                         "Could not save the provider. Check config.toml and its permissions.")
    return await asyncio.to_thread(_settings, app, saved)


def _remove(current: Snapshot, ref: ProviderRef) -> Snapshot:
    _check_revision(current, ref.revision)
    setup = _find(provider_catalog(current.config), ref.name)
    if setup.uses:
        uses = ", ".join(_use_label(use) for use in setup.uses)
        raise HTTPException(409, f"{setup.name} is still used by {uses}. Change those roles first.")
    if not setup.saved:
        raise HTTPException(404, "There is no saved provider with that name.")
    document = tomlkit.parse(current.text)
    document["providers"].pop(setup.name)
    if not document["providers"]:
        document.pop("providers")
    return validated(current, document)


@router.post(PREFIX + "/remove")
async def remove_provider(request: Request) -> ModelSettings:
    app = owner_app(request)
    ref = await _input(request, ProviderRef)
    saved = await commit(app, lambda current: _remove(current, ref),
                         "Could not remove the provider. Check config.toml and its permissions.")
    return await asyncio.to_thread(_settings, app, saved)


@router.post(PREFIX + "/test")
async def test_provider(request: Request) -> TestResult:
    app = owner_app(request)
    ref = await _input(request, ProviderRef)
    snapshot = _snapshot(app.config.home)
    _check_revision(snapshot, ref.revision)
    setup = _find(provider_catalog(snapshot.config), ref.name)
    result = await _probe_setup(app, snapshot.config, setup)
    if result.ok and any(use.role in DECISION_ROLES for use in setup.uses):
        app.refresh_triage()  # a key added to .env after saving takes effect once it works
    return result


def _list_view(listed: model_lists.ModelList) -> ModelListView:
    return ModelListView(
        listable=True, fetched_at=listed.fetched_at,
        models=[ListedModelView(id=item.id, name=item.name, reasoning=item.reasoning, efforts=list(item.efforts),
                                  default_effort=item.default_effort)
                for item in listed.models],
    )


@router.post(PREFIX + "/models")
async def list_models(request: Request) -> ModelListView:
    app = owner_app(request)
    query = await _input(request, ModelListRequest)
    snapshot = _snapshot(app.config.home)
    _check_revision(snapshot, query.revision)
    setup = _find(provider_catalog(snapshot.config), query.name)
    if not setup.listable:
        return ModelListView(listable=False, models=[])
    secrets = model_lists.secret_values(snapshot.config)
    if not query.refresh and (listed := await model_lists.cached(
            app.store, setup.base_url, setup.api_key_env, secrets=secrets, type_=setup.type)):
        return _list_view(listed)
    try:
        models = await _fetch_list(app, snapshot.config, setup)
    except ListRefused as exc:
        raise HTTPException(422, str(exc)) from None
    except MissingProviderKey:
        raise HTTPException(422, f"Add {setup.api_key_env} to .env, then refresh the list. You can type a model ID meanwhile.") from None
    except model_lists.ListUnavailable as exc:
        raise HTTPException(502, str(exc)) from None
    return _list_view(await model_lists.remember(app.store, setup.base_url, setup.api_key_env, models, type_=setup.type))


class EnvKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64)
    value: str | None = Field(None, max_length=4096)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        clean = value.strip()
        if not ENV_NAME.fullmatch(clean):
            raise ValueError("Key name must be uppercase letters, digits and underscores (e.g. ANTHROPIC_API_KEY).")
        if clean in PROTECTED_ENV_KEYS:
            raise ValueError(f"{clean} is a protected system credential and cannot be changed from the dashboard.")
        return clean

    @field_validator("value")
    @classmethod
    def _valid_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("Key value must be a single line.")
        return value.strip() or None


def _env_keys_view(app: App) -> dict:
    """List key names and whether each is set (never any secret values)."""
    home = app.config.home
    dotenv_keys = set(_read_dotenv(home / ".env"))
    referenced = set(app.config.secret_names())
    names = sorted((dotenv_keys | referenced) - PROTECTED_ENV_KEYS)
    return {
        "keys": [
            {"name": name, "set": bool(app.config.secret(name)), "in_dotenv": name in dotenv_keys}
            for name in names
        ],
    }


@router.get(PREFIX + "/keys")
async def read_env_keys(request: Request) -> dict:
    app = owner_app(request)
    return await asyncio.to_thread(_env_keys_view, app)


@router.post(PREFIX + "/keys")
async def save_env_key(request: Request) -> dict:
    """Write-only .env key update: adds, replaces or clears one non-protected key and refreshes live clients."""
    app = owner_app(request)
    update = await _input(request, EnvKeyUpdate)

    async def run() -> dict:
        async with _CONFIG_WRITE:
            await asyncio.to_thread(set_dotenv, app.config.home, update.name, update.value)
            app.refresh_triage()
            app.refresh_stt()
            warning = None
            try:
                app.apply_model_routes(app.config)
            except Exception as exc:
                warning = f"Saved to .env, but live routes could not be refreshed ({type(exc).__name__})."
            view = await asyncio.to_thread(_env_keys_view, app)
            if warning:
                view["warning"] = warning
            return view

    return await asyncio.shield(asyncio.ensure_future(run()))
