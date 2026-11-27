"""Owner settings JSON contract: Services (owner decision E81), the things that have no other home in Settings.

GET /api/services returns ServicesView: the KLIPY GIF key, the local studio (address and token), the desktop hosts
(one entry per host, under "desktop") and the T3 Code bridge. Each service has a label, a state (ok, missing, error or
off), a one-line detail, and its editable fields with their kind and limits. A secret field only ever says whether a
value is set: values are written to .env and never read back. `notices` carries what Settings shows as an info glyph
rather than a warning (the zero-data-retention recommendation, shown once in `dom setup`).

POST /api/services/save takes ServiceSave: the GET revision, a service id and the fields to change. A field left out
is unchanged; a secret set to null is removed. Fields are validated, config.toml is written like every other
settings save, and the running service is rebuilt at once, except desktop hosts and the T3 switch, which are
built at startup (`restart_required`). POST /api/services/test takes ServiceTest and makes one real, harmless
request: KLIPY's search with the key, the studio's health with its token, T3's list of linked machines.
Both return the changed service as ServiceView (the test returns ServiceTestResult). Owner only, same-origin JSON.
"""

from __future__ import annotations

import asyncio
import re
import time
from time import monotonic
from typing import TYPE_CHECKING, Literal

import httpx
import tomlkit
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dom.config import Config, DesktopHost, StudioConfig
from dom.gifs import GifSearch
from dom.setup import STUDIO_NOTE_TAILSCALE, ZDR_RECOMMENDATION
from dom.studio.client import StudioClient
from dom.t3.auth import SCOPES
from dom.t3.client import READS, T3Unavailable
from dom.web.model_settings import REVISION, Snapshot, _check_revision, _input, _snapshot, save_values
from dom.web.provider_settings import _http
from dom.web.shared import owner_app

if TYPE_CHECKING:
    from dom.telegram.handlers import App

router = APIRouter()
PREFIX = "/api/services"
SAVE_PATH, TEST_PATH = PREFIX + "/save", PREFIX + "/test"
JSON_POSTS = {SAVE_PATH, TEST_PATH}
TEST_TIMEOUT = 8
KLIPY = "KLIPY_API_KEY"
State = Literal["ok", "missing", "error", "off"]
FieldValue = str | int | bool | None


class FieldView(BaseModel):
    name: str
    label: str
    kind: Literal["text", "url", "secret", "bool", "int"]
    value: str | int | bool | None  # never set for a secret
    is_set: bool | None = None  # a secret's only status: whether a value is held in .env
    editable: bool = True
    help: str = ""
    min: int | None = None
    max: int | None = None


class LinkView(BaseModel):
    label: str
    kind: Literal["telegram", "command"]
    text: str


class PermissionView(BaseModel):
    id: str
    label: str
    detail: str


class ServiceView(BaseModel):
    id: str
    label: str
    state: State
    detail: str
    help: str
    fields: list[FieldView] = []
    links: list[LinkView] = []
    permissions: list[PermissionView] = []
    children: list[ServiceView] = []
    can_test: bool = False
    restart_required: bool = False


class Notice(BaseModel):
    id: str
    glyph: Literal["info"]
    text: str


class ServicesView(BaseModel):
    revision: str
    services: list[ServiceView]
    notices: list[Notice]


class ServiceSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    id: str = Field(max_length=80)
    fields: dict[str, str | int | bool | None] = Field(max_length=8)


class ServiceTest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=REVISION)
    id: Literal["klipy", "studio", "t3"]


class ServiceTestResult(BaseModel):
    ok: bool
    detail: str
    latency_ms: int


def _secret(name: str, label: str, value: str | None, help_: str) -> FieldView:
    return FieldView(name=name, label=label, kind="secret", value=None, is_set=bool(value), help=help_)


# ---- KLIPY ------------------------------------------------------------------------------------------------------

def _klipy(app: App, config: Config) -> ServiceView:
    key = config.secret(KLIPY)
    if not key:
        state, detail = "missing", "No key: Dom does not offer reaction GIFs."
    elif app.gifs is None:
        state, detail = "ok", "Key set. Restart Dom to turn GIFs on."
    else:
        state, detail = "ok", "Key set: Dom can send reaction GIFs."
    return ServiceView(
        id="klipy", label="GIFs (KLIPY)", state=state, detail=detail, can_test=True,
        help="Dom searches KLIPY for a reaction GIF with a short query and nothing about the chat. Get a key from KLIPY.",
        fields=[_secret(KLIPY, "KLIPY API key", key, f"Stored in .env as {KLIPY}. Paste a new key to replace it.")],
    )


async def _test_klipy(app: App, config: Config) -> ServiceTestResult:
    key = await asyncio.to_thread(config.secret, KLIPY)
    start = monotonic()
    if not key:
        return ServiceTestResult(ok=False, detail="Add the KLIPY key first.", latency_ms=0)
    failure = await GifSearch(key, _http(app)).probe()
    return ServiceTestResult(ok=failure is None, detail=failure or "KLIPY accepted the key.",
                             latency_ms=round((monotonic() - start) * 1000))


# ---- the local studio -------------------------------------------------------------------------------------------

STUDIO_ORIGIN_RULE = "Use an http:// or https:// address such as http://studio.example.test:8765, with no path."


def _studio(app: App, config: Config) -> ServiceView:
    studio = config.studio
    token = config.secret(studio.token_env)
    if not studio.enabled:
        state, detail = "off", "The studio is off. Dom sends no local media (cloud backups may still serve)."
    elif not studio.base_url:
        state, detail = "missing", "Set the studio's address."
    elif not token:
        state, detail = "missing", f"Paste the studio's token; it is kept in .env as {studio.token_env}."
    elif app.studio is None:
        state, detail = "ok", "Set up. Restart Dom to connect."
    elif app.studio.status.startswith("away since"):
        state, detail = "error", f"Not answering: {app.studio.status}."
    elif app.studio.ready:
        state, detail = "ok", "Connected and ready." if app.studio.status == "on" else f"Connected, {app.studio.status}."
    else:
        state, detail = "ok", "Set up. Press Test to check it now."
    return ServiceView(
        id="studio", label="Local studio", state=state, detail=detail, can_test=True,
        help="The media server that makes Dom's voice notes, round videos and photos: on this machine, or on a rented "
             "cloud GPU. " + STUDIO_NOTE_TAILSCALE,
        fields=[
            FieldView(name="enabled", label="Use the studio", kind="bool", value=studio.enabled),
            FieldView(name="base_url", label="Address", kind="url", value=studio.base_url,
                      help="The studio's origin without /v1, for example http://studio.example.test:8765."),
            _secret("token", "Token", token, f"Kept in .env as {studio.token_env}. Paste a new one to replace it."),
        ],
    )


async def _test_studio(app: App, config: Config) -> ServiceTestResult:
    studio = config.studio
    token = await asyncio.to_thread(config.secret, studio.token_env)
    start = monotonic()
    if not studio.base_url or not token:
        return ServiceTestResult(ok=False, detail="Set the address and the token first.", latency_ms=0)
    try:
        async with asyncio.timeout(TEST_TIMEOUT):
            response = await (app.studio_http or _http(app)).get(
                studio.base_url + "/v1/health", headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False, timeout=TEST_TIMEOUT,
            )
        response.raise_for_status()
        ok = response.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"
        detail = "The studio answered." if ok else "That address answered, but it does not look like the studio."
    except httpx.HTTPStatusError as error:
        ok, detail = False, ("The studio refused the token." if error.response.status_code in (401, 403)
                             else f"The studio answered HTTP {error.response.status_code}.")
    except (TimeoutError, httpx.TimeoutException):
        ok, detail = False, "The studio did not answer in time."
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        ok, detail = False, "Could not reach the studio."
    return ServiceTestResult(ok=ok, detail=detail, latency_ms=round((monotonic() - start) * 1000))


# ---- desktop hosts ----------------------------------------------------------------------------------------------

def _desktop(app: App, config: Config) -> ServiceView:
    hosts = config.desktop_hosts
    fleet = app.desktop_worker
    children = [_host(app, config, host) for host in hosts]
    if not hosts:
        state, detail = "off", "Desktop is off. Set [desktop] mode to local or remote, or list hosts."
    elif fleet is None:
        state, detail = "off", "Desktop is configured. Restart Dom to start it."
    else:
        bad = [child for child in children if child.state in ("error", "missing")]
        state, detail = ("error" if bad else "ok"), (
            f"{len(bad)} of {len(children)} hosts need attention." if bad else f"{len(children)} host(s) ready.")
    return ServiceView(
        id="desktop", label="Desktop hosts", state=state, detail=detail, children=children,
        help="Machines that run Dom's desktop. Hosts, limits and isolation are read when Dom starts, so a change "
             "applies after a restart. " + _guest_limits(config),
        restart_required=config.desktop != app.config.desktop or config.desktop_mode != app.config.desktop_mode,
    )


def _guest_limits(config: Config) -> str:
    """What a task for a group or a friend may use, so the owner sees it beside the hosts (set under [desktop.guest])."""
    guest = config.desktop.guest
    if not guest.enabled:
        return "Tasks for groups and friends are off."
    cap = lambda usd: f"${usd:.2f}" if usd else "no limit"  # noqa: E731
    return (
        f"Tasks for groups and friends (each needs your Allow once): {guest.max_steps} steps and {guest.max_minutes} "
        f"minutes at most, {cap(guest.task_cost_cap_usd)} a task and {cap(guest.daily_cost_cap_usd)} a day for each chat. "
        "They run on a host with guest = true, else on a container of their own on the local host."
    )


def _host(app: App, config: Config, host: DesktopHost) -> ServiceView:
    fleet = app.desktop_worker
    live = next((item for item in fleet.hosts if item.config.name == host.name), None) if fleet is not None else None
    fields = []
    if host.mode == "remote":
        fields += [
            FieldView(name="url", label="Sandbox address", kind="url", value=host.url,
                      help="The remote sandbox API's base URL."),
            _secret("api_key", "API key", config.secret(host.api_key_env),
                    f"Kept in .env as {host.api_key_env}. Paste a new one to replace it."),
            FieldView(name="guest", label="Kept for guests", kind="bool", value=host.guest, editable=False,
                      help="Set guest = true in [[desktop.hosts]] to keep this host for tasks that groups and friends ask for. "
                           "Your own tasks never run on it."),
            FieldView(name="remote_isolation_confirmed", label="Isolation checked", kind="bool",
                      value=host.remote_isolation_confirmed,
                      help="Tick once you have checked the remote host cannot reach your LAN or the machine running Dom."),
        ]
    else:
        fields += [
            FieldView(name="max_containers", label="Most containers at once", kind="int", value=host.max_containers,
                      min=1, max=64, editable=bool(config.desktop.hosts),
                      help="Only a host listed in [[desktop.hosts]] can change this."),
            FieldView(name="min_free_memory_mb", label="Memory to keep free (MB)", kind="int", value=host.memory_floor_mb,
                      min=0, max=1_000_000, editable=bool(config.desktop.hosts),
                      help="Dom will not start a container that would leave less than this free."),
        ]
    if host.mode == "remote" and not config.secret(host.api_key_env):
        state, detail = "missing", "Add the sandbox API key."
    elif host.mode == "remote" and not host.remote_isolation_confirmed:
        state, detail = "missing", "Confirm the host cannot reach your LAN before Dom uses it."
    elif live is None or fleet is None:
        state, detail = "off", "Restart Dom to use this host."
    else:
        lanes = [lane for lane in fleet.lanes.values() if lane.host is live]
        errored = next((lane for lane in lanes if lane.status() == "error"), None)
        running = sum(lane.status() in ("starting", "ready", "stopping") for lane in lanes)
        if errored is not None:
            state, detail = "error", str(getattr(errored.sandbox, "error", None) or "A container failed to start.")[:200]
        else:
            state = "ok"
            detail = f"{running} of {host.max_containers} container(s) running." if host.mode == "local" else (
                "Running." if running else "Idle: it starts when a task needs it.")
    return ServiceView(
        id=f"desktop:{host.name}", label=host.name, state=state, detail=detail, fields=fields,
        help=f"A {host.mode} desktop host.", restart_required=True,
    )


# ---- T3 Code ----------------------------------------------------------------------------------------------------

T3_PERMISSIONS = {
    "orchestration:read": ("Read", "Read projects, threads and search results on linked machines."),
    "orchestration:operate": ("Operate", "Send a message to a thread, only after you tap to confirm its destination and exact text."),
}


def _t3(app: App, config: Config) -> ServiceView:
    t3 = config.t3
    bridge = app.t3
    expires = bridge.expiry() if bridge is not None else None
    if not t3.enabled:
        state, detail = "off", "The bridge is off."
    elif bridge is None:
        state, detail = "ok", "Switched on. Restart Dom to start the bridge."
    elif not bridge.available():
        state, detail = "missing", "Not signed in. Run the sign-in command below."
    elif expires is not None and expires <= time.time():
        state, detail = "error", "The sign-in has expired. Run the sign-in command below."
    elif bridge.reachable is not None:
        state, detail = "ok", f"Signed in. {bridge.reachable} linked machine(s) reachable at the last check."
    else:
        state, detail = "ok", "Signed in. Press Test to check the linked machines."
    return ServiceView(
        id="t3", label="T3 Code", state=state, detail=detail, can_test=True,
        help=f"Lets you look at coding projects and threads on linked machines from your DM. Version {t3.version} "
             f"on port {t3.port}, loopback only.",
        fields=[
            FieldView(name="enabled", label="Use the bridge", kind="bool", value=t3.enabled,
                      help="Applies after a restart."),
            FieldView(name="version", label="Version", kind="text", value=t3.version, editable=False),
        ],
        permissions=[
            PermissionView(id=scope, label=T3_PERMISSIONS[scope][0], detail=T3_PERMISSIONS[scope][1])
            for scope in SCOPES
        ] + [PermissionView(id="allowlist", label="Allowed tools", detail=", ".join(sorted(READS)) + ", and a confirmed send.")],
        links=[
            LinkView(label="Sign in", kind="command", text="dom t3-connect <home> --yes"),
            LinkView(label="Sign-in, machines and audit history", kind="telegram", text="/t3"),
        ],
        restart_required=t3.enabled != (bridge is not None),
    )


async def _test_t3(app: App) -> ServiceTestResult:
    start = monotonic()
    if app.t3 is None:
        return ServiceTestResult(ok=False, detail="The bridge is not running.", latency_ms=0)
    try:
        machines = await app.t3.machines(summary="dashboard test")
    except T3Unavailable as error:
        return ServiceTestResult(ok=False, detail=str(error)[:200], latency_ms=round((monotonic() - start) * 1000))
    reachable = sum(row["status"] == "reachable" for row in machines)
    return ServiceTestResult(ok=True, detail=f"{reachable} of {len(machines)} linked machine(s) reachable.",
                             latency_ms=round((monotonic() - start) * 1000))


# ---- the list ---------------------------------------------------------------------------------------------------

def _services(app: App, snapshot: Snapshot) -> ServicesView:
    config = snapshot.config
    return ServicesView(
        revision=snapshot.revision,
        services=[_klipy(app, config), _studio(app, config), _desktop(app, config), _t3(app, config)],
        notices=[Notice(id="zdr", glyph="info", text=ZDR_RECOMMENDATION)],
    )


@router.get(PREFIX)
async def read_services(request: Request) -> ServicesView:
    app = owner_app(request)
    return await asyncio.to_thread(lambda: _services(app, _snapshot(app.config.home)))


# ---- saving -----------------------------------------------------------------------------------------------------

def _refuse(detail: str) -> HTTPException:
    return HTTPException(422, detail)


def _only(fields: dict, allowed: set[str]) -> None:
    if set(fields) - allowed:
        raise _refuse("Unexpected fields. Use only the documented settings fields.")


def _secret_value(fields: dict, name: str) -> str | None:
    value = fields[name]
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,512}", value)):
        raise _refuse("A key or token is one run of letters, digits and symbols, with no spaces.")
    return value


def _flag(fields: dict, name: str) -> bool:
    if not isinstance(fields[name], bool):
        raise _refuse("Choose on or off.")
    return fields[name]


def _whole(fields: dict, name: str, low: int, high: int, *, nullable: bool = False) -> int | None:
    value = fields[name]
    if value is None and nullable:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise _refuse(f"Use a whole number from {low} to {high}.")
    return value


class _Change:
    """What one service save writes: config.toml values, .env secrets, and what to apply to the running app."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, ...], bool | int | str | None] = {}
        self.env: dict[str, str | None] = {}
        self.live: dict[str, object] = {}  # top-level Config keys the running config takes from the saved one


def _klipy_change(fields: dict) -> _Change:
    _only(fields, {KLIPY})
    change = _Change()
    if KLIPY in fields:
        change.env[KLIPY] = _secret_value(fields, KLIPY)
    return change


def _studio_change(fields: dict, config: Config) -> _Change:
    _only(fields, {"enabled", "base_url", "token"})
    change = _Change()
    if "enabled" in fields:
        change.values[("studio", "enabled")] = _flag(fields, "enabled")
    if "base_url" in fields:
        base_url = fields["base_url"]
        try:
            StudioConfig(base_url=base_url if isinstance(base_url, str) else None)  # the config's own origin check
        except ValidationError:
            raise _refuse(STUDIO_ORIGIN_RULE) from None
        change.values[("studio", "base_url")] = StudioConfig(base_url=base_url).base_url
    if "token" in fields:
        change.env[config.studio.token_env] = _secret_value(fields, "token")
    return change


def _host_change(fields: dict, config: Config, name: str) -> _Change:
    host = next((item for item in config.desktop_hosts if item.name == name), None)
    if host is None:
        raise HTTPException(404, "There is no desktop host with that name. Reload settings and try again.")
    listed = bool(config.desktop.hosts)
    allowed = {"url", "api_key", "remote_isolation_confirmed"} if host.mode == "remote" else (
        {"max_containers", "min_free_memory_mb"} if listed else set())
    _only(fields, allowed)
    change = _Change()
    prefix = ("desktop", "hosts") if listed else ("desktop",)
    if "url" in fields:
        url = fields["url"]
        if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s/@]+(?:/[^\s]*)?", url):
            raise _refuse("Use an http:// or https:// address without credentials.")
        change.values[(*prefix, host.name, "url") if listed else ("desktop", "url")] = url
    if "remote_isolation_confirmed" in fields:
        change.values[(*prefix, host.name, "remote_isolation_confirmed") if listed else ("desktop", "remote_isolation_confirmed")] = _flag(
            fields, "remote_isolation_confirmed")
    if "api_key" in fields:
        change.env[host.api_key_env] = _secret_value(fields, "api_key")
    if "max_containers" in fields:
        change.values[(*prefix, host.name, "max_containers")] = _whole(fields, "max_containers", 1, 64)
    if "min_free_memory_mb" in fields:
        change.values[(*prefix, host.name, "min_free_memory_mb")] = _whole(
            fields, "min_free_memory_mb", 0, 1_000_000, nullable=True)
    return change


def _t3_change(fields: dict) -> _Change:
    _only(fields, {"enabled"})
    change = _Change()
    if "enabled" in fields:
        change.values[("t3", "enabled")] = _flag(fields, "enabled")
    return change


def _write_host_values(document: tomlkit.TOMLDocument, values: dict) -> dict:
    """Desktop host edits address an entry of [[desktop.hosts]] by name; return the plain paths left for set_value."""
    plain = {}
    for path, value in values.items():
        if path[:2] == ("desktop", "hosts"):
            entry = next((table for table in document["desktop"]["hosts"] if table.get("name") == path[2]), None)
            if entry is None:
                raise HTTPException(404, "There is no desktop host with that name. Reload settings and try again.")
            if value is None:
                entry.pop(path[3], None)
            else:
                entry[path[3]] = value
        else:
            plain[path] = value
    return plain


async def _apply_live(app: App, saved: Snapshot, service: str) -> None:
    """Rebuild what the running app built from the saved values at startup."""
    config = saved.config
    if service == "klipy":
        key = await asyncio.to_thread(config.secret, KLIPY)
        app.gifs = GifSearch(key, _http(app)) if key else None
    elif service == "studio":
        token = await asyncio.to_thread(config.secret, config.studio.token_env)
        studio = app.config.studio
        app.studio = (
            StudioClient(studio, token, app.studio_http or _http(app)) if studio.enabled and studio.base_url and token else None
        )


@router.post(SAVE_PATH)
async def save_service(request: Request) -> ServicesView:
    app = owner_app(request)
    edit = await _input(request, ServiceSave)
    snapshot = await asyncio.to_thread(_snapshot, app.config.home)
    _check_revision(snapshot, edit.revision)
    service, _, host = edit.id.partition(":")
    config = snapshot.config
    if service == "klipy" and not host:
        change = _klipy_change(edit.fields)
    elif service == "studio" and not host:
        change = _studio_change(edit.fields, config)
    elif service == "desktop" and host:
        change = _host_change(edit.fields, config, host)
    elif service == "t3" and not host:
        change = _t3_change(edit.fields)
    else:
        raise HTTPException(404, "There is no such service. Reload settings and try again.")
    if not change.values and not change.env:
        return _services(app, snapshot)
    # Desktop and T3 are built at startup: the file takes the change and the running config waits for a restart.
    live = {
        "studio": lambda saved: {"studio": app.config.studio.model_copy(update={
            key: getattr(saved.studio, key) for key in ("enabled", "base_url")})},
    }.get(service, lambda saved: {})
    saved = await save_values(
        app, edit.revision, change.values, live, "Could not save the service. Check config.toml and its permissions.",
        env=change.env, prepare=_write_host_values if service == "desktop" else None,
    )
    await _apply_live(app, saved, service)
    return await asyncio.to_thread(lambda: _services(app, _snapshot(app.config.home)))


@router.post(TEST_PATH)
async def test_service(request: Request) -> ServiceTestResult:
    app = owner_app(request)
    edit = await _input(request, ServiceTest)
    snapshot = await asyncio.to_thread(_snapshot, app.config.home)
    _check_revision(snapshot, edit.revision)
    if edit.id == "klipy":
        return await _test_klipy(app, snapshot.config)
    if edit.id == "studio":
        return await _test_studio(app, snapshot.config)
    return await _test_t3(app)
