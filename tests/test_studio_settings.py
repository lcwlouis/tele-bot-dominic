"""Data policies per OpenRouter route, the studio backups editor, studio switch availability and the profile interval."""

import asyncio
import json
import tomllib
from datetime import timedelta

import httpx
import pytest

from dom.config import ModelEndpoint, ProviderEndpoint, StudioConfig, StudioFallback, load_config, routing_preferences
from dom.doctor import _model as doctor_model
from dom.escalation import Prices
from dom.jev import JevClient
from dom.llm import ChatClient
from dom.search import SearchClient
from dom.studio.policy import enabled, switch_servable, unserved
from test_studio_cloud import IMAGE, allowed, cloud_server, configure  # noqa: F401 (fixture)
from test_web_model_settings import API, ORIGIN_HEADER, START, dashboard  # noqa: F401 (fixture)
from test_web_provider_settings import _mock, _post, _provider

OPENROUTER = "https://openrouter.ai/api/v1"
POLICIES = [("zdr", {"zdr": True}), ("no_training", {"data_collection": "deny"}), ("any", None)]
STUDIO = "/api/studio"
ROUTES = [STUDIO + path for path in ("/save", "/remove", "/move", "/test", "/models")]
PERMISSIONS = "/api/permissions"
MEDIA_KEY = "OPENROUTER_MEDIA_API_KEY"


def test_routing_preferences_and_defaults():
    assert routing_preferences("zdr") == {"zdr": True}
    assert routing_preferences("no_training") == {"data_collection": "deny"}
    assert routing_preferences("any") == {}
    # Every route keeps today's zero data retention unless the owner chooses otherwise.
    assert ProviderEndpoint(base_url=OPENROUTER).data_policy == "zdr"
    assert StudioFallback(provider="openrouter_image", model="a/b", api_key_env="K").data_policy == "zdr"
    assert JevClient(None, "k", "m").data_policy == "zdr"


def _capture():
    seen = []

    def respond(request):
        seen.append((request.url, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/decisions"):
            return httpx.Response(200, json={"answers": {"check": {"type": "choice", "choice": "yes"}}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.parametrize(("policy", "expected"), POLICIES)
async def test_every_openrouter_call_site_sends_the_routes_policy(policy, expected, make_app, cloud_server):
    endpoint = ModelEndpoint(base_url=OPENROUTER, model="acme/model", data_policy=policy)
    seen, http = _capture()
    async with http:
        await ChatClient(endpoint, "k", http).complete([{"role": "user", "content": "hi"}], [], once=True)
        await SearchClient(endpoint, "k", http).search("hello")
        await JevClient(http, "k", "acme/jev", OPENROUTER + "/alpha", data_policy=policy).decide(
            "state", {"check": {"type": "choice", "instructions": "x", "criteria": {"yes": "y", "no": "n"}}})
        await doctor_model(http, "chat", endpoint, "k")
        prices = Prices(asyncio.ensure_future, lambda: 0.0)
        prices.warm(ChatClient(endpoint, "k", http))
        await prices.pending
    bodies = [body for _, body in seen if body is not None]
    assert len(bodies) == 4 and all(body.get("provider") == expected for body in bodies)
    price_url = next(url for url, body in seen if body is None)
    assert (price_url.params.get("zdr") == "true") is (policy == "zdr")  # its prices come from the matching list

    app = make_app()
    configure(app, cloud_server, images=(IMAGE.model_copy(update={"data_policy": policy, "supports_reference": False,
                                                                   "allow_without_reference": True}),))
    await app.cloud_studio.generate(app.config, "photo", "A park", None, 1, allowed)
    assert cloud_server[1][0][1].get("provider") == expected


async def test_a_providers_policy_reaches_every_role_slot_using_it(dashboard):
    app, path, browser = dashboard
    path.write_text(START.replace("[custom]", '[models.media]\nbase_url = "https://openrouter.ai/api/v1"\n'
                                  'model = "acme/see"\napi_key_env = "ROUTER_KEY"\n\n[custom]'))
    async with _mock(app, lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})), \
            browser() as client:
        settings = (await client.get(API)).json()
        router = next(item for item in settings["providers"] if item["base_url"] == OPENROUTER)
        assert router["data_policy"] == "zdr"
        saved = await _post(client, "/save", original=router["name"], name="Router", kind="openrouter",
                            base_url=OPENROUTER, api_key_env="ROUTER_KEY", data_policy="no_training")
        assert saved.status_code == 200, saved.text
        assert _provider(saved.json(), "Router")["data_policy"] == "no_training"
        assert saved.json()["roles"]["media"]["primary"]["data_policy"] == "no_training"
        # A role save keeps the provider's policy for the slot, though the editor doesn't send one.
        resaved = await client.post(API, headers=ORIGIN_HEADER, json={
            "revision": saved.json()["revision"], "role": "media", "fallbacks": [],
            "primary": {"base_url": OPENROUTER, "model": "acme/see", "api_key_env": "ROUTER_KEY"},
        })
        assert resaved.status_code == 200, resaved.text
    config = tomllib.loads(path.read_text())
    assert config["providers"]["Router"]["data_policy"] == "no_training"
    assert config["models"]["media"]["data_policy"] == "no_training"
    assert "data_policy" not in config["models"]["chat"]  # zdr stays implicit
    assert load_config(path.parent).models["media"].data_policy == "no_training"


def _studio_mock(app, *, key_status=200, models=("acme/voice", "acme/video")):
    seen = []

    def respond(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/key":
            return httpx.Response(key_status, json={"data": {}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model} for model in models]})
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, json={"name": "models/voice"})
        return httpx.Response(500)

    return seen, _mock(app, respond)


async def _studio(client, path, **body):
    revision = (await client.get(API)).json()["revision"]
    return await client.post(STUDIO + path, headers=ORIGIN_HEADER, json={"revision": revision, **body})


VOICE_BACKUP = {"provider": "openrouter_tts", "model": "acme/voice", "api_key_env": MEDIA_KEY}
VIDEO_BACKUP = {"provider": "openrouter_video", "model": "acme/video", "api_key_env": MEDIA_KEY,
                "data_policy": "no_training"}


async def test_studio_routes_need_owner_auth_same_origin_and_json(dashboard):
    _, _, browser = dashboard
    async with browser(authorized=False) as client:
        for route in ROUTES:
            assert (await client.post(route, json={}, headers=ORIGIN_HEADER)).status_code == 403
    async with browser() as client:
        for route in ROUTES:
            for headers in ({}, {"Origin": "https://other.example.test"}):
                assert (await client.post(route, headers=headers, json={})).status_code == 403
            assert (await client.post(route, headers=ORIGIN_HEADER, json={})).status_code == 422
        stale = await client.post(STUDIO + "/save", headers=ORIGIN_HEADER,
                                  json={"revision": "0" * 64, "chain": "voice", "backup": VOICE_BACKUP})
        assert stale.status_code == 409


async def test_studio_backups_add_edit_move_remove_with_a_media_key(dashboard, monkeypatch):
    app, path, browser = dashboard
    seen, http = _studio_mock(app)
    async with http, browser() as client:
        # A new media key variable is saved first (its value goes in .env afterwards), like any new key variable.
        added = await _studio(client, "/save", chain="voice", backup=VOICE_BACKUP)
        assert added.status_code == 200, added.text
        assert not seen and added.json()["studio"]["voice"][0]["key_set"] is False
        monkeypatch.setenv(MEDIA_KEY, "fixture-media-key")
        tested = await _studio(client, "/test", chain="voice", index=0)
        assert tested.json()["ok"] is True, tested.text
        assert seen == ["/api/v1/key", "/api/v1/audio/speech/models"]  # no media was generated
        video = await _studio(client, "/save", chain="video", backup=VIDEO_BACKUP)
        assert video.status_code == 200, video.text
        assert app.config.studio.video_fallbacks[0].data_policy == "no_training"  # applies at once
        gemini = {"provider": "gemini_tts", "model": "voice", "api_key_env": "GEMINI_KEY", "paid": True}
        second = await _studio(client, "/save", chain="voice", backup=gemini)
        assert second.status_code == 200, second.text
        edited = await _studio(client, "/save", chain="voice", index=0,
                               backup={**VOICE_BACKUP, "data_policy": "no_training"})
        assert edited.status_code == 200, edited.text
        moved = await _studio(client, "/move", chain="voice", index=1)
        assert [item["provider"] for item in moved.json()["studio"]["voice"]] == ["gemini_tts", "openrouter_tts"]
        before = path.read_text()
        removed = await _studio(client, "/remove", chain="voice", index=0)
        assert removed.status_code == 200
        assert path.with_name("config.toml.bak").read_text() == before
        assert (await _studio(client, "/remove", chain="voice", index=4)).status_code == 404
    studio = tomllib.loads(path.read_text())["studio"]
    assert studio["voice_fallbacks"] == [{**VOICE_BACKUP, "data_policy": "no_training"}]
    assert studio["video_fallbacks"] == [VIDEO_BACKUP]
    assert tomllib.loads(path.read_text())["custom"] == {"stuff": [{"keep": True}]}


@pytest.mark.parametrize(("chain", "backup", "fragment"), [
    ("video", {**VIDEO_BACKUP, "data_policy": "zdr"}, "zero data retention"),
    ("video", {k: v for k, v in VIDEO_BACKUP.items() if k != "data_policy"}, "zero data retention"),
    ("voice", {"provider": "gemini_tts", "model": "voice", "api_key_env": "GEMINI_KEY"}, "Paid Services"),
    ("image", VOICE_BACKUP, "can't make this kind"),
    ("voice", {**VOICE_BACKUP, "api_key_env": "TELEGRAM_BOT_TOKEN"}, "another credential"),
    ("voice", {**VOICE_BACKUP, "api_key_env": "BACKUP_KEY"}, "different host"),
    ("voice", {**VOICE_BACKUP, "model": "bad model"}, ""),
    ("voice", {**VOICE_BACKUP, "extra": 1}, "Unexpected fields"),
])
async def test_studio_backup_validation(dashboard, chain, backup, fragment):
    app, path, browser = dashboard
    seen, http = _studio_mock(app)
    before = path.read_text()
    async with http, browser() as client:
        refused = await _studio(client, "/save", chain=chain, backup=backup)
    assert refused.status_code == 422 and fragment in refused.json()["detail"]
    assert not seen and path.read_text() == before


@pytest.mark.parametrize(("key_status", "models", "fragment"), [
    (401, ("acme/voice",), "refused the key"), (200, ("acme/other",), "doesn't list that model"),
])
async def test_studio_backup_test_runs_before_the_save(dashboard, monkeypatch, key_status, models, fragment):
    app, path, browser = dashboard
    path.write_text(START + '[studio]\n[[studio.image_fallbacks]]\nprovider = "openrouter_image"\n'
                    f'model = "acme/image"\napi_key_env = "{MEDIA_KEY}"\n')  # the media key is paired already
    monkeypatch.setenv(MEDIA_KEY, "fixture-media-key")
    before = path.read_text()
    _, http = _studio_mock(app, key_status=key_status, models=models)
    async with http, browser() as client:
        refused = await _studio(client, "/save", chain="voice", backup=VOICE_BACKUP)
    assert refused.status_code == 422 and fragment in refused.json()["detail"]
    assert path.read_text() == before


async def test_studio_backup_list_is_the_output_kinds_list(dashboard):
    app, _, browser = dashboard
    seen, http = _studio_mock(app)
    async with http, browser() as client:
        listed = await _studio(client, "/models", provider="openrouter_video")
        assert [item["id"] for item in listed.json()["models"]] == ["acme/video", "acme/voice"]
        assert (await _studio(client, "/models", provider="gemini_tts")).json() == {"listable": False, "models": [],
                                                                                   "fetched_at": None}
    assert seen == ["/api/v1/videos/models"]


def test_switches_need_something_that_can_serve_them():
    nothing = StudioConfig(voice=True, video_notes=True, images=True, special=True, profile_photo_follows_preset=True)
    assert not any(enabled(nothing, "owner_dm", kind) for kind in ("voice", "video_note", "photo", "special"))
    assert unserved(nothing) == ["voice notes", "round video notes", "photos", "special video notes", "the profile photo"]
    voice_only = nothing.model_copy(update={"voice_fallbacks": (StudioFallback(
        provider="openrouter_tts", model="a/b", api_key_env="K"),)})
    assert enabled(voice_only, "friend_dm", "voice") and not enabled(voice_only, "owner_dm", "photo")
    unpaid = nothing.model_copy(update={"voice_fallbacks": (StudioFallback(
        provider="gemini_tts", model="voice", api_key_env="K"),)})
    assert not switch_servable(unpaid, "voice")  # an unpaid Gemini key never runs
    local = nothing.model_copy(update={"enabled": True, "base_url": "https://studio.example.org"})
    assert unserved(local) == [] and switch_servable(local, "profile_photo_follows_preset")


async def test_the_server_refuses_a_switch_nothing_can_serve(dashboard, monkeypatch):
    app, path, browser = dashboard
    async with browser() as client:
        listed = (await client.get(PERMISSIONS)).json()
        voice = next(item for item in listed["items"] if item["id"] == "studio_voice")
        assert voice["available"] is False and "voice backup" in voice["needs"]
        assert next(item for item in listed["items"] if item["id"] == "openers")["available"] is True
        refused = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={
            "revision": listed["revision"], "id": "studio_voice", "enabled": True})
        assert refused.status_code == 409 and "voice backup" in refused.json()["detail"]
        assert not app.config.studio.voice
        # Once a backup can serve it, it turns on; turning it off is always allowed, even after the setup goes.
        path.write_text(START + '[studio]\n[[studio.voice_fallbacks]]\nprovider = "openrouter_tts"\n'
                        'model = "a/b"\napi_key_env = "VOICE_KEY"\n')
        revision = (await client.get(PERMISSIONS)).json()["revision"]
        on = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={
            "revision": revision, "id": "studio_voice", "enabled": True})
        assert on.status_code == 200 and app.config.studio.voice
        path.write_text(START + "[studio]\nvoice = true\n")
        listed = (await client.get(PERMISSIONS)).json()
        voice = next(item for item in listed["items"] if item["id"] == "studio_voice")
        assert voice["enabled"] and not voice["available"]  # on, but nothing can serve it
        off = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={
            "revision": listed["revision"], "id": "studio_voice", "enabled": False})
        assert off.status_code == 200


async def test_status_says_when_a_switch_is_on_but_nothing_serves_it(make_app):
    from dom.studio.profile import status_text
    from dom.telegram.owner import render_status

    app = make_app()
    app.config = app.config.model_copy(update={"studio": StudioConfig(voice=True)})
    assert "On, but nothing can make them: voice notes" in await status_text(app)
    view = await render_status(app)
    assert "nothing can make them: voice notes" in str(view)


PROFILE_INTERVAL = PERMISSIONS + "/profile-interval"


async def test_profile_interval_route(dashboard):
    app, path, browser = dashboard
    async with browser(authorized=False) as client:
        assert (await client.post(PROFILE_INTERVAL, json={}, headers=ORIGIN_HEADER)).status_code == 403
    async with browser() as client:
        listed = (await client.get(PERMISSIONS)).json()
        assert listed["profile_photo_every_days"] == 14
        body = {"revision": listed["revision"], "days": 7}
        assert (await client.post(PROFILE_INTERVAL, json=body)).status_code == 403
        for bad in ({**body, "days": 3}, {**body, "days": "7"}, {**body, "extra": 1}):
            assert (await client.post(PROFILE_INTERVAL, headers=ORIGIN_HEADER, json=bad)).status_code == 422
        saved = await client.post(PROFILE_INTERVAL, headers=ORIGIN_HEADER, json=body)
        assert saved.status_code == 200 and saved.json()["profile_photo_every_days"] == 7
    assert app.config.studio.profile_photo_every_days == 7
    assert tomllib.loads(path.read_text())["studio"]["profile_photo_every_days"] == 7


async def test_turning_the_profile_switch_on_allows_a_sync_at_once(dashboard):
    app, path, browser = dashboard
    path.write_text(START + '[studio]\nenabled = true\nbase_url = "https://studio.example.org"\n')
    await app.store.kv_set("studio:profile_at", (app.dispatch.now() - timedelta(hours=1)).isoformat())
    async with browser() as client:
        revision = (await client.get(PERMISSIONS)).json()["revision"]
        on = await client.post(PERMISSIONS, headers=ORIGIN_HEADER, json={
            "revision": revision, "id": "studio_profile", "enabled": True})
        assert on.status_code == 200
    assert not await app.store.kv_get("studio:profile_at")
