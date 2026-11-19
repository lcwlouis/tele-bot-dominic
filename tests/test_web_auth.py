import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from dom import cli
from dom.config import Config, load_config
from dom.home import init_home

ORIGIN = "https://dashboard.example.org"
POST_HEADERS = {"HX-Request": "true", "Origin": ORIGIN}
ISSUER = "https://example-team.cloudflareaccess.com"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("dom.web.auth.monotonic", clock)
    return clock


@pytest.fixture
def web_config(tmp_path):
    init_home(tmp_path)
    config_file = tmp_path / "config.toml"
    assert not load_config(tmp_path).web.enabled
    config_file.write_text(config_file.read_text().replace("[web]\nenabled = false", "[web]\nenabled = true"))
    return load_config(tmp_path)


@pytest.fixture
def access_keys():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    def make(kid):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid=kid, use="sig", alg="RS256")
        return key, jwk

    return make("first"), make("second")


async def test_access_jwt_validation_and_key_rotation(tmp_path, access_keys, clock):
    import jwt

    from dom.web.app import create_app
    from dom.web.auth import Auth

    (key, jwk), (next_key, next_jwk) = access_keys
    issuer = "https://example-team.cloudflareaccess.com"
    config = Config(
        home=tmp_path, web={"enabled": True, "access_team": "example-team", "access_aud": "example-audience"},
    )
    calls = []

    async def jwks(url):
        calls.append(url)
        return {"keys": [jwk] if len(calls) == 1 else [jwk, next_jwk]}

    claims = {"aud": "example-audience", "iss": issuer, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}

    def header(signing_key=key, kid="first", **overrides):
        return {"Cf-Access-Jwt-Assertion": jwt.encode(claims | overrides, signing_key, "RS256", headers={"kid": kid})}

    web = create_app(SimpleNamespace(owner_id=None), Auth(config, jwks))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        assert (await client.get("/", headers=header())).status_code == 200
        for invalid in (
            {}, header(aud="other"), header(exp=0), header(next_key), header(iss=ORIGIN), header(exp=None),
        ):
            assert (await client.get("/", headers=invalid)).status_code == 403
        assert len(calls) == 1
        clock.now += 61
        assert (await client.get("/", headers=header(next_key, "second"))).status_code == 200
    assert calls == [f"{issuer}/cdn-cgi/access/certs"] * 2


async def test_dev_token_and_security_headers(tmp_path, monkeypatch):
    from dom.web.app import create_app
    from dom.web.auth import Auth

    monkeypatch.setenv("DOM_TEST_TOKEN", "test-token")
    config = Config(home=tmp_path, web={"dev_token_env": "DOM_TEST_TOKEN"})
    web = create_app(SimpleNamespace(owner_id=None), Auth(config))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as client:
        for header, expected in (("Bearer incorrect", 403), ("Bearer test-token", 200)):
            response = await client.get("/", headers={"Authorization": header})
            assert response.status_code == expected
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"]
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["Cache-Control"] == "no-store"


async def test_auth_required_before_dom_starts(web_config, monkeypatch):
    monkeypatch.setattr(cli, "Bot", lambda **kwargs: pytest.fail("must refuse before starting Telegram"))
    with pytest.raises(SystemExit, match="auth"):
        await cli.run(web_config.home)


@pytest.mark.parametrize("web", [
    {}, {"access_team": "example-team"}, {"access_aud": "example-audience"},
    {"dev_token_env": "DOM_MISSING_TEST_TOKEN"},
])
def test_incomplete_auth_refused(tmp_path, web, monkeypatch):
    from dom.web.auth import Auth

    monkeypatch.delenv("DOM_MISSING_TEST_TOKEN", raising=False)
    with pytest.raises(ValueError, match="auth"):
        Auth(Config(home=tmp_path, web=web))


async def test_post_requires_htmx_and_same_origin(tmp_path, monkeypatch):
    from dom.web.app import create_app
    from dom.web.auth import Auth

    monkeypatch.setenv("DOM_TEST_TOKEN", "test-token")
    config = Config(home=tmp_path, web={"dev_token_env": "DOM_TEST_TOKEN"})
    web = create_app(SimpleNamespace(owner_id=None), Auth(config))

    @web.api_route("/action", methods=["POST", "PUT"])
    async def action():
        return "done"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web), base_url=ORIGIN,
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        assert (await client.post("/action", headers=POST_HEADERS)).status_code == 200
        assert (await client.post("/action", headers={
            "HX-Request": "true", "Referer": f"{ORIGIN}/chats",
        })).status_code == 200
        for invalid in (
            {}, {"Origin": ORIGIN}, {"HX-Request": "true"},
            POST_HEADERS | {"Origin": "http://dashboard.example.org"},
            POST_HEADERS | {"Origin": f"{ORIGIN}:8443"},
            POST_HEADERS | {"Origin": f"{ORIGIN}.example.org", "Referer": f"{ORIGIN}/chats"},
            POST_HEADERS | {"Origin": "null", "Referer": f"{ORIGIN}/chats"},
        ):
            assert (await client.post("/action", headers=invalid)).status_code == 403
        assert (await client.put("/action", headers=POST_HEADERS)).status_code == 403


# --- JWKS refresh policy -------------------------------------------------------------------------


def access_dashboard(tmp_path, jwks):
    from dom.web.app import create_app
    from dom.web.auth import Auth

    config = Config(
        home=tmp_path, web={"enabled": True, "access_team": "example-team", "access_aud": "example-audience"},
    )
    web = create_app(SimpleNamespace(owner_id=None), Auth(config, jwks))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN)


def assertion(key, kid, **claims):
    import jwt

    payload = {
        "aud": "example-audience", "iss": ISSUER, "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    } | claims
    return {"Cf-Access-Jwt-Assertion": jwt.encode(payload, key, "RS256", headers={"kid": kid})}


async def test_unknown_kids_fetch_once_per_minute(tmp_path, access_keys, clock):
    (key, jwk), _ = access_keys
    calls = []

    async def jwks(url):
        calls.append(url)
        return {"keys": [jwk]}

    async with access_dashboard(tmp_path, jwks) as client:
        for number in range(5):
            assert (await client.get("/", headers=assertion(key, f"junk-{number}"))).status_code == 403
        assert len(calls) == 1
        clock.now += 61
        assert (await client.get("/", headers=assertion(key, "junk-later"))).status_code == 403
        assert len(calls) == 2


async def test_concurrent_requests_share_one_refresh(tmp_path, access_keys, clock):
    (key, jwk), _ = access_keys
    calls = []
    gate = asyncio.Event()

    async def jwks(url):
        calls.append(url)
        await gate.wait()
        return {"keys": [jwk]}

    async with access_dashboard(tmp_path, jwks) as client:
        requests = [asyncio.create_task(client.get("/", headers=assertion(key, "first"))) for _ in range(5)]
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        assert [response.status_code for response in await asyncio.gather(*requests)] == [200] * 5
    assert len(calls) == 1


async def test_cached_key_is_not_delayed_by_a_stalled_refresh(tmp_path, access_keys, clock):
    (key, jwk), _ = access_keys
    calls = []
    gate = asyncio.Event()

    async def jwks(url):
        calls.append(url)
        if len(calls) > 1:
            await gate.wait()
        return {"keys": [jwk]}

    async with access_dashboard(tmp_path, jwks) as client:
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 200
        clock.now += 61
        stalled = asyncio.create_task(client.get("/", headers=assertion(key, "unknown")))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(calls) == 2
        response = await asyncio.wait_for(client.get("/", headers=assertion(key, "first")), 1)
        assert response.status_code == 200
        gate.set()
        assert (await stalled).status_code == 403


async def test_failed_refresh_is_cached_for_thirty_seconds(tmp_path, access_keys, clock):
    (key, _), _ = access_keys
    calls = []

    async def jwks(url):
        calls.append(url)
        raise httpx.ConnectError("unreachable")

    async with access_dashboard(tmp_path, jwks) as client:
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 403
        clock.now += 10
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 403
        assert len(calls) == 1
        clock.now += 21
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 403
        assert len(calls) == 2


async def test_refresh_gives_up_after_the_timeout(tmp_path, access_keys, clock, monkeypatch):
    (key, _), _ = access_keys
    calls = []
    monkeypatch.setattr("dom.web.auth.FETCH_TIMEOUT", 0.05)

    async def jwks(url):
        calls.append(url)
        await asyncio.Event().wait()

    async with access_dashboard(tmp_path, jwks) as client:
        response = await asyncio.wait_for(client.get("/", headers=assertion(key, "first")), 2)
        assert response.status_code == 403
        assert len(calls) == 1


async def test_previous_keys_survive_a_failed_refresh_for_a_day(tmp_path, access_keys, clock):
    (key, jwk), _ = access_keys
    calls = []

    async def jwks(url):
        calls.append(url)
        if len(calls) > 1:
            raise httpx.ConnectError("unreachable")
        return {"keys": [jwk]}

    async with access_dashboard(tmp_path, jwks) as client:
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 200
        clock.now += 3601
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 200
        assert len(calls) == 2
        clock.now += 86400
        assert (await client.get("/", headers=assertion(key, "first"))).status_code == 403
        assert len(calls) == 3
