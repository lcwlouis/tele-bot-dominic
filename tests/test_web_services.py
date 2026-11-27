"""Settings → Services: the KLIPY key, the local studio, desktop hosts and T3 Code."""

import json
import os
import stat
import time
import tomllib

import httpx
import pytest

import dom.web.services_settings as services
from dom.config import Config, load_config
from dom.desktop.fleet import DesktopFleet
from dom.t3.client import T3Unavailable
from dom.t3.tools import Bridge
from dom.web.app import create_app
from dom.web.auth import Auth
from test_desktop_fleet import Box
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN

API = "/api/services"
SAVE, TEST = API + "/save", API + "/test"
KEY = "klipy-key-value-0001"
STUDIO_TOKEN = "studio-token-value-0001"
START = """\
# Keep the instance options and comments.
timezone = "UTC"

[models.chat]
base_url = "http://localhost:8000/v1"
model = "chat-model"

[studio]
enabled = false  # switched on from Settings
base_url = ""

[t3]
enabled = false
"""


@pytest.fixture
def dashboard(make_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)
    for name in ("KLIPY_API_KEY", "STUDIO_TOKEN", "SANDBOX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.toml"
    path.write_text(START)
    app = make_app()
    app.config = load_config(tmp_path)
    web = create_app(app, Auth(Config(home=tmp_path, web={"dev_token_env": "DOM_TEST_TOKEN"})))
    seen = []
    answers = {"status": 200, "type": "application/json", "error": None}

    def respond(request):
        seen.append(request)
        if answers["error"] is not None:
            raise answers["error"]
        return httpx.Response(answers["status"], json={"data": {"data": []}, "ok": True}, headers={"content-type": answers["type"]})

    outside = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(services, "_http", lambda _app: outside)
    app.studio_http = outside

    def browser(*, authorized=True):
        headers = {"Authorization": f"Bearer {TOKEN}"} if authorized else {}
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN, headers=headers)

    app.seen, app.answers = seen, answers
    return app, path, browser


def find(view, name):
    return next(item for item in view["services"] if item["id"] == name)


def field(service, name):
    return next(item for item in service["fields"] if item["name"] == name)


async def current(client):
    return (await client.get(API)).json()


async def save(client, service, fields, **extra):
    revision = (await current(client))["revision"]
    return await client.post(SAVE, headers=POST_HEADERS, json={"revision": revision, "id": service, "fields": fields, **extra})


async def test_routes_need_owner_auth_same_origin_and_json(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        assert (await client.get(API)).status_code == 403
        for route in (SAVE, TEST):
            assert (await client.post(route, json={}, headers={"Origin": ORIGIN})).status_code == 403
    async with browser() as client:
        for route in (SAVE, TEST):
            for headers in ({}, {"Origin": "https://other.example.test"}, {"HX-Request": "true"}):
                assert (await client.post(route, headers=headers, json={})).status_code == 403
            assert (await client.post(route, headers=POST_HEADERS, json={})).status_code == 422
            assert (await client.post(route, headers={"Origin": ORIGIN}, data={})).status_code == 403
        app.store.owner_id = None
        assert (await client.get(API)).status_code == 409
    assert path.read_text() == START


async def test_every_service_has_a_label_a_state_and_a_detail_and_zdr_is_an_info_notice(dashboard):
    _, _, browser = dashboard
    async with browser() as client:
        view = await current(client)
    assert [item["id"] for item in view["services"]] == ["klipy", "studio", "desktop", "t3"]
    for item in view["services"]:
        assert item["label"] and item["detail"] and item["state"] in ("ok", "missing", "error", "off")
    assert [find(view, name)["state"] for name in ("klipy", "studio", "desktop", "t3")] == ["missing", "off", "off", "off"]
    assert [(n["id"], n["glyph"]) for n in view["notices"]] == [("zdr", "info")]
    assert "Zero data retention is recommended" in view["notices"][0]["text"]
    assert "warning" not in json.dumps(view["notices"]).lower()


async def test_klipy_key_is_written_to_env_never_read_back_and_turns_gifs_on(dashboard, tmp_path):
    app, path, browser = dashboard
    assert app.gifs is None
    async with browser() as client:
        saved = await save(client, "klipy", {"KLIPY_API_KEY": KEY})
        assert saved.status_code == 200
        service = find(saved.json(), "klipy")
        assert service["state"] == "ok" and field(service, "KLIPY_API_KEY") == {
            "name": "KLIPY_API_KEY", "label": "KLIPY API key", "kind": "secret", "value": None, "is_set": True,
            "editable": True, "help": field(service, "KLIPY_API_KEY")["help"], "min": None, "max": None,
        }
        assert KEY not in saved.text and KEY not in (await client.get(API)).text
        assert app.gifs is not None
        tested = await client.post(TEST, headers=POST_HEADERS, json={"revision": saved.json()["revision"], "id": "klipy"})
    assert tested.json()["ok"] is True and KEY in str(app.seen[-1].url)  # the key goes to KLIPY only
    assert KEY not in tested.text
    env = tmp_path / ".env"
    assert env.read_text() == f"KLIPY_API_KEY={KEY}\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert path.read_text() == START  # nothing in config.toml changed


async def test_klipy_test_reports_a_refused_key_without_the_key(dashboard):
    app, _, browser = dashboard
    async with browser() as client:
        await save(client, "klipy", {"KLIPY_API_KEY": KEY})
        revision = (await current(client))["revision"]
        body = {"revision": revision, "id": "klipy"}
        app.answers["status"] = 403
        refused = (await client.post(TEST, headers=POST_HEADERS, json=body)).json()
        app.answers["status"], app.answers["error"] = 200, httpx.ConnectError("boom " + KEY)
        unreachable = (await client.post(TEST, headers=POST_HEADERS, json=body)).json()
    assert refused["ok"] is False and refused["detail"] == "KLIPY refused the key."
    assert unreachable["ok"] is False and unreachable["detail"] == "Could not reach KLIPY."
    assert KEY not in json.dumps([refused, unreachable])


async def test_klipy_test_without_a_key_makes_no_request(dashboard):
    app, _, browser = dashboard
    async with browser() as client:
        result = (await client.post(TEST, headers=POST_HEADERS, json={
            "revision": (await current(client))["revision"], "id": "klipy"})).json()
    assert result["ok"] is False and "Add the KLIPY key" in result["detail"] and app.seen == []


async def test_a_secret_set_to_null_is_removed_and_other_env_lines_stay(dashboard, tmp_path):
    app, _, browser = dashboard
    (tmp_path / ".env").write_text(f"# mine\nTELEGRAM_BOT_TOKEN=bot-token-value\nKLIPY_API_KEY={KEY}\nOTHER=1\n")
    app.gifs = object()
    async with browser() as client:
        assert find(await current(client), "klipy")["state"] == "ok"
        saved = await save(client, "klipy", {"KLIPY_API_KEY": None})
    assert find(saved.json(), "klipy")["state"] == "missing" and app.gifs is None
    assert (tmp_path / ".env").read_text() == "# mine\nTELEGRAM_BOT_TOKEN=bot-token-value\nOTHER=1\n"


@pytest.mark.parametrize("value", ["", "two words", "line\nbreak", "x" * 513, 12, True, "tab\there", "café"])
async def test_a_malformed_secret_is_refused_and_nothing_is_written(dashboard, tmp_path, value):
    _, _, browser = dashboard
    async with browser() as client:
        refused = await save(client, "klipy", {"KLIPY_API_KEY": value})
    assert refused.status_code == 422 and not (tmp_path / ".env").exists()


@pytest.mark.parametrize(("service", "fields"), [
    ("klipy", {"other": "x"}), ("klipy", {"KLIPY_API_KEY": KEY, "extra": "x"}), ("studio", {"nonsense": 1}),
    ("studio", {"token_env": "OPENROUTER_API_KEY"}), ("t3", {"port": 9}), ("desktop:default", {"max_containers": 3}),
])
async def test_unknown_fields_are_refused(dashboard, tmp_path, service, fields):
    app, path, browser = dashboard
    async with browser() as client:
        refused = await save(client, service, fields)
    assert refused.status_code in (404, 422)
    assert path.read_text() == START and not (tmp_path / ".env").exists()


async def test_unknown_services_and_stale_pages_are_refused(dashboard):
    _, path, browser = dashboard
    async with browser() as client:
        assert (await save(client, "mystery", {})).status_code == 404
        assert (await save(client, "klipy:extra", {})).status_code == 404
        revision = (await current(client))["revision"]
        path.write_text(START + "\n# changed by hand\n")
        stale = await client.post(SAVE, headers=POST_HEADERS, json={
            "revision": revision, "id": "klipy", "fields": {"KLIPY_API_KEY": KEY}})
    assert stale.status_code == 409


async def test_studio_address_and_token_are_saved_and_the_client_is_built_now(dashboard, tmp_path):
    app, path, browser = dashboard
    assert app.studio is None
    async with browser() as client:
        saved = await save(client, "studio", {
            "enabled": True, "base_url": "http://studio.example.test:8080/", "token": STUDIO_TOKEN})
        assert saved.status_code == 200
        studio = find(saved.json(), "studio")
        assert studio["state"] == "ok" and field(studio, "base_url")["value"] == "http://studio.example.test:8080"
        assert field(studio, "enabled")["value"] is True
        assert field(studio, "token")["is_set"] is True and field(studio, "token")["value"] is None
        assert STUDIO_TOKEN not in saved.text and STUDIO_TOKEN not in (await client.get(API)).text
    written = tomllib.loads(path.read_text())["studio"]
    assert written["enabled"] is True and written["base_url"] == "http://studio.example.test:8080"
    assert "# Keep the instance options and comments." in path.read_text()
    assert "# switched on from Settings" in path.read_text()
    assert (tmp_path / ".env").read_text() == f"STUDIO_TOKEN={STUDIO_TOKEN}\n"
    assert app.config.studio.enabled and app.config.studio.base_url == "http://studio.example.test:8080"
    assert app.studio is not None and app.studio.config.base_url == app.config.studio.base_url


async def test_the_studio_needs_an_address_and_a_token_to_connect(dashboard):
    app, _, browser = dashboard
    async with browser() as client:
        on = await save(client, "studio", {"enabled": True})
        assert find(on.json(), "studio")["state"] == "missing" and "address" in find(on.json(), "studio")["detail"]
        addressed = await save(client, "studio", {"base_url": "http://studio.example.test:8080"})
        assert find(addressed.json(), "studio")["state"] == "missing" and "token" in find(addressed.json(), "studio")["detail"]
        assert app.studio is None
        tokened = await save(client, "studio", {"token": STUDIO_TOKEN})
        assert app.studio is not None
        removed = await save(client, "studio", {"token": None})
    assert find(tokened.json(), "studio")["state"] == "ok"
    assert find(removed.json(), "studio")["state"] == "missing" and app.studio is None


@pytest.mark.parametrize("address", [
    "studio.example.test", "ftp://studio.example.test", "http://studio.example.test/v1", "http://user:pw@studio.example.test",
    "http://studio.example.test?x=1", "", 5, None, True,
])
async def test_a_bad_studio_address_is_refused(dashboard, address):
    app, path, browser = dashboard
    async with browser() as client:
        refused = await save(client, "studio", {"base_url": address})
    if address == "":  # an empty address clears it
        assert refused.status_code == 200
        return
    assert refused.status_code == 422 and path.read_text() == START
    assert "http://studio.example.test:8765" in refused.json()["detail"]


@pytest.mark.parametrize(("status", "content_type", "error", "ok", "words"), [
    (200, "application/json", None, True, "answered"),
    (200, "text/html", None, False, "does not look like the studio"),
    (401, "application/json", None, False, "refused the token"),
    (503, "application/json", None, False, "HTTP 503"),
    (200, "application/json", httpx.ConnectError("no route"), False, "Could not reach the studio"),
    (200, "application/json", httpx.ReadTimeout("slow"), False, "did not answer in time"),
])
async def test_studio_test_calls_health_with_the_token_and_says_what_went_wrong(
    dashboard, status, content_type, error, ok, words,
):
    app, _, browser = dashboard
    async with browser() as client:
        await save(client, "studio", {"enabled": True, "base_url": "http://studio.example.test:8080", "token": STUDIO_TOKEN})
        app.answers.update(status=status, type=content_type, error=error)
        revision = (await current(client))["revision"]
        result = (await client.post(TEST, headers=POST_HEADERS, json={"revision": revision, "id": "studio"})).json()
    assert result["ok"] is ok and words in result["detail"] and STUDIO_TOKEN not in json.dumps(result)
    request = app.seen[-1]
    assert str(request.url) == "http://studio.example.test:8080/v1/health"
    assert request.headers["authorization"] == f"Bearer {STUDIO_TOKEN}"


async def test_studio_test_without_settings_makes_no_request(dashboard):
    app, _, browser = dashboard
    async with browser() as client:
        result = (await client.post(TEST, headers=POST_HEADERS, json={
            "revision": (await current(client))["revision"], "id": "studio"})).json()
    assert result["ok"] is False and app.seen == []


async def test_the_running_studios_state_shows_in_the_view(dashboard):
    app, _, browser = dashboard
    async with browser() as client:
        await save(client, "studio", {"enabled": True, "base_url": "http://studio.example.test:8080", "token": STUDIO_TOKEN})
        app.studio._away_since = __import__("datetime").datetime(2026, 1, 1, 9, 30, tzinfo=__import__("datetime").timezone.utc)
        studio = find(await current(client), "studio")
    assert studio["state"] == "error" and "away since 09:30 UTC" in studio["detail"]


REMOTE = """
[desktop]
url = ""

[[desktop.hosts]]
name = "pi"
mode = "local"
max_containers = 2

[[desktop.hosts]]
name = "gpu"
mode = "remote"
url = "https://sandbox.example.test"
api_key_env = "SANDBOX_API_KEY"
"""


async def fleet(app, path, tmp_path):
    path.write_text(START + REMOTE)
    app.config = load_config(tmp_path)
    app.desktop_worker = DesktopFleet(app, free_memory=lambda: 8000, sandbox_factory=lambda host, name: Box(host, name))
    return app.desktop_worker


async def test_desktop_hosts_are_listed_with_status_and_only_the_basics_are_editable(dashboard, tmp_path):
    app, path, browser = dashboard
    await fleet(app, path, tmp_path)
    async with browser() as client:
        desktop = find(await current(client), "desktop")
    hosts = {child["label"]: child for child in desktop["children"]}
    assert list(hosts) == ["pi", "gpu"] and hosts["pi"]["id"] == "desktop:pi"
    assert [f["name"] for f in hosts["pi"]["fields"]] == ["max_containers", "min_free_memory_mb"]
    assert [f["name"] for f in hosts["gpu"]["fields"]] == ["url", "api_key", "remote_isolation_confirmed"]
    assert "Tasks for groups and friends" in desktop["help"] and "15 steps" in desktop["help"]
    assert hosts["pi"]["state"] == "ok" and "0 of 2 container" in hosts["pi"]["detail"]
    assert hosts["gpu"]["state"] == "missing" and "API key" in hosts["gpu"]["detail"]
    assert desktop["state"] == "error" and desktop["restart_required"] is False


async def test_a_remote_host_needs_its_key_and_an_isolation_check_before_it_is_ok(dashboard, tmp_path):
    app, path, browser = dashboard
    await fleet(app, path, tmp_path)
    async with browser() as client:
        keyed = await save(client, "desktop:gpu", {"api_key": "sandbox-key-value-1"})
        gpu = next(c for c in find(keyed.json(), "desktop")["children"] if c["label"] == "gpu")
        assert gpu["state"] == "missing" and "cannot reach your LAN" in gpu["detail"]
        assert "sandbox-key-value-1" not in keyed.text
        checked = await save(client, "desktop:gpu", {"remote_isolation_confirmed": True})
    gpu = next(c for c in find(checked.json(), "desktop")["children"] if c["label"] == "gpu")
    assert gpu["state"] == "ok" and (tmp_path / ".env").read_text() == "SANDBOX_API_KEY=sandbox-key-value-1\n"


async def test_editing_a_desktop_host_writes_that_hosts_table_and_asks_for_a_restart(dashboard, tmp_path):
    app, path, browser = dashboard
    await fleet(app, path, tmp_path)
    before = app.config.desktop
    async with browser() as client:
        saved = await save(client, "desktop:pi", {"max_containers": 4, "min_free_memory_mb": 1024})
        assert saved.status_code == 200
        desktop = find(saved.json(), "desktop")
        assert desktop["restart_required"] is True
        remote = await save(client, "desktop:gpu", {"url": "https://other.example.test"})
    written = tomllib.loads(path.read_text())["desktop"]["hosts"]
    assert written[0] == {"name": "pi", "mode": "local", "max_containers": 4, "min_free_memory_mb": 1024}
    assert written[1]["url"] == "https://other.example.test" and written[1]["name"] == "gpu"
    assert "# Keep the instance options and comments." in path.read_text()
    assert app.config.desktop == before  # the running fleet is built at startup: nothing changes until a restart
    assert find(remote.json(), "desktop")["restart_required"] is True
    pi = next(c for c in find(remote.json(), "desktop")["children"] if c["label"] == "pi")
    assert field(pi, "max_containers")["value"] == 4


@pytest.mark.parametrize(("service", "fields"), [
    ("desktop:pi", {"max_containers": 0}), ("desktop:pi", {"max_containers": 65}), ("desktop:pi", {"max_containers": True}),
    ("desktop:pi", {"max_containers": "2"}), ("desktop:pi", {"min_free_memory_mb": -1}),
    ("desktop:gpu", {"url": "ftp://sandbox.example.test"}), ("desktop:gpu", {"url": "https://user:pw@sandbox.example.test"}),
    ("desktop:gpu", {"remote_isolation_confirmed": "yes"}), ("desktop:gpu", {"max_containers": 2}),
    ("desktop:pi", {"url": "https://x.example.test"}),
])
async def test_bad_desktop_edits_are_refused_and_nothing_is_written(dashboard, tmp_path, service, fields):
    app, path, browser = dashboard
    await fleet(app, path, tmp_path)
    text = path.read_text()
    async with browser() as client:
        refused = await save(client, service, fields)
    assert refused.status_code == 422 and path.read_text() == text and not (tmp_path / ".env").exists()


async def test_an_unknown_desktop_host_is_not_found(dashboard, tmp_path):
    app, path, browser = dashboard
    await fleet(app, path, tmp_path)
    async with browser() as client:
        assert (await save(client, "desktop:nope", {"max_containers": 2})).status_code == 404


async def test_the_single_default_host_edits_the_desktop_keys(dashboard, tmp_path):
    app, path, browser = dashboard
    path.write_text(START + '\n[desktop]\nmode = "remote"\nurl = "https://sandbox.example.test"\n')
    app.config = load_config(tmp_path)
    async with browser() as client:
        view = find(await current(client), "desktop")
        (host,) = view["children"]
        assert host["id"] == "desktop:default" and [f["name"] for f in host["fields"]][0] == "url"
        saved = await save(client, "desktop:default", {"url": "https://new.example.test", "remote_isolation_confirmed": True})
    assert saved.status_code == 200
    written = tomllib.loads(path.read_text())["desktop"]
    assert written["url"] == "https://new.example.test" and written["remote_isolation_confirmed"] is True


async def test_t3_is_off_until_enabled_and_asks_for_a_restart_when_switched_on(dashboard):
    app, path, browser = dashboard
    async with browser() as client:
        t3 = find(await current(client), "t3")
        assert t3["state"] == "off" and t3["restart_required"] is False
        saved = await save(client, "t3", {"enabled": True})
    t3 = find(saved.json(), "t3")
    assert t3["state"] == "ok" and "Restart" in t3["detail"] and t3["restart_required"] is True
    assert tomllib.loads(path.read_text())["t3"]["enabled"] is True
    assert app.t3 is None


async def test_t3_shows_its_sign_in_state_version_permissions_and_commands(dashboard, tmp_path):
    app, path, browser = dashboard
    path.write_text(START.replace("[t3]\nenabled = false", "[t3]\nenabled = true"))
    app.config = load_config(tmp_path)
    app.t3 = Bridge(app)
    async with browser() as client:
        t3 = find(await current(client), "t3")
        assert t3["state"] == "missing" and "Not signed in" in t3["detail"]
        (tmp_path / "t3-token.json").write_text(json.dumps({"access_token": "t", "expires_at": time.time() - 10}))
        assert find(await current(client), "t3")["state"] == "error"
        (tmp_path / "t3-token.json").write_text(json.dumps({"access_token": "t", "expires_at": time.time() + 86400}))
        t3 = find(await current(client), "t3")
    assert t3["state"] == "ok" and "Signed in" in t3["detail"] and t3["restart_required"] is False
    assert field(t3, "version") == {**field(t3, "version"), "value": "0.1.10", "editable": False}
    assert [p["id"] for p in t3["permissions"]] == ["orchestration:read", "orchestration:operate", "allowlist"]
    assert "confirm" in t3["permissions"][1]["detail"]
    assert "t3_thread_read" in t3["permissions"][2]["detail"] and "confirmed send" in t3["permissions"][2]["detail"]
    assert [(link["kind"], link["text"]) for link in t3["links"]] == [
        ("command", "dom t3-connect <home> --yes"), ("telegram", "/t3")]
    assert "t3-token" not in json.dumps(t3) and "access_token" not in json.dumps(t3)


async def test_t3_test_lists_machines_through_the_audited_bridge(dashboard, tmp_path):
    app, path, browser = dashboard
    app.t3 = Bridge(app)
    rows = [{"label": "laptop", "environmentId": "e1", "status": "reachable"},
            {"label": "desk", "environmentId": "e2", "status": "offline"}]

    async def machines(**_):
        return rows

    app.t3.machines = machines
    async with browser() as client:
        revision = (await current(client))["revision"]
        result = (await client.post(TEST, headers=POST_HEADERS, json={"revision": revision, "id": "t3"})).json()
        assert result["ok"] is True and "1 of 2" in result["detail"]

        async def down(**_):
            raise T3Unavailable("T3 sign-in expired or is invalid. Reconnect to T3 Code.")

        app.t3.machines = down
        failed = (await client.post(TEST, headers=POST_HEADERS, json={"revision": revision, "id": "t3"})).json()
        app.t3 = None
        off = (await client.post(TEST, headers=POST_HEADERS, json={"revision": revision, "id": "t3"})).json()
    assert failed["ok"] is False and "expired" in failed["detail"]
    assert off["ok"] is False and "not running" in off["detail"]


async def test_no_view_ever_holds_a_secret_value(dashboard, tmp_path):
    app, path, browser = dashboard
    values = ["klipy-secret-aaa", "studio-secret-bbb", "sandbox-secret-ccc", "bot-secret-ddd"]
    (tmp_path / ".env").write_text(
        f"KLIPY_API_KEY={values[0]}\nSTUDIO_TOKEN={values[1]}\nSANDBOX_API_KEY={values[2]}\nTELEGRAM_BOT_TOKEN={values[3]}\n")
    path.write_text(START.replace("enabled = false  # switched", "enabled = true  # switched") + REMOTE)
    app.config = load_config(tmp_path)
    async with browser() as client:
        view = await client.get(API)
    assert view.status_code == 200 and not any(value in view.text for value in values)
    assert os.environ.get("KLIPY_API_KEY") is None
