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


async def test_access_jwt_validation_and_key_rotation(tmp_path, access_keys):
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
