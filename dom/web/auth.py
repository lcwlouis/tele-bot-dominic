"""Validate the owner dashboard's Cloudflare Access assertion or local bearer token."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import httpx
import jwt
from starlette.requests import Request

from dom.config import Config

log = logging.getLogger("dom.web")

JWKS_TTL = 3600  # a fetched key set is trusted this long without asking again
JWKS_STALE = 86400  # after a failed refresh, the previous keys stay usable this long
UNKNOWN_KID_INTERVAL = 60  # an unrecognised kid may trigger a refresh at most this often
FAILURE_BACKOFF = 30  # after a failed refresh, serve from the cache this long before retrying
FETCH_TIMEOUT = 5  # total seconds for one refresh
DEV_TOKEN_MIN_LENGTH = 32
CLOCK_LEEWAY = 60  # seconds of clock skew tolerated on exp, nbf and iat
_TEAM = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII)
Identity = Literal["access", "local"]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Auth:
    def __init__(self, config: Config, fetch_jwks: Callable[[str], Awaitable[dict]] | None = None):
        web = config.web
        if web.dev_token_env and (web.access_team or web.access_aud):
            raise ValueError("Dashboard auth: set either dev_token_env or access_team and access_aud, not both.")
        if (web.access_team or web.access_aud) and not (
            _TEAM.fullmatch(web.access_team) and web.access_aud.strip()
        ):
            raise ValueError("Dashboard auth needs a valid access_team and access_aud.")
        self.issuer = f"https://{web.access_team.lower()}.cloudflareaccess.com" if web.access_team else ""
        self.audience = web.access_aud
        token = config.secret(web.dev_token_env) if web.dev_token_env else None
        self.dev_token = token.encode("utf-8") if token and token.strip() else b""
        if self.dev_token:
            if len(token.strip()) < DEV_TOKEN_MIN_LENGTH:
                raise ValueError(f"Dashboard auth: the dev token must be at least {DEV_TOKEN_MIN_LENGTH} characters.")
            if not _is_loopback(web.bind):
                raise ValueError("Dashboard auth: web.bind must be a loopback address when only a dev token is set.")
        if not (self.issuer or self.dev_token):
            raise ValueError("Dashboard auth needs access_team and access_aud, or a populated dev_token_env.")
        self._fetch_jwks = fetch_jwks or self._fetch
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0  # the key set is fresh until then
        self._stale_at = 0.0  # the key set is unusable from then on
        self._last_refresh = float("-inf")
        self._retry_at = 0.0
        self._refresh: asyncio.Task[None] | None = None

    @staticmethod
    async def _fetch(url: str) -> dict:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def _reload(self) -> None:
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                jwks = await self._fetch_jwks(f"{self.issuer}/cdn-cgi/access/certs")
            keys = {}
            for entry in jwks["keys"]:
                if (
                    isinstance(entry, dict) and isinstance(entry.get("kid"), str)
                    and entry.get("kty") == "RSA" and entry.get("use", "sig") == "sig"
                    and entry.get("alg", "RS256") == "RS256"
                ):
                    keys[entry["kid"]] = jwt.PyJWK(entry, algorithm="RS256")
            if not keys:
                raise ValueError("the key set holds no usable RS256 key")
        except Exception as exc:
            self._retry_at = monotonic() + FAILURE_BACKOFF
            log.warning("Access key refresh failed (%s: %s).", type(exc).__name__, exc)
        else:
            now = monotonic()
            self._keys, self._expires_at, self._stale_at = keys, now + JWKS_TTL, now + JWKS_STALE
        finally:
            self._refresh = None

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        now = monotonic()
        fresh = now < self._expires_at
        if not (fresh and kid in self._keys):
            due = not fresh or now >= self._last_refresh + UNKNOWN_KID_INTERVAL
            if due and now >= self._retry_at:
                if self._refresh is None:
                    self._last_refresh = now
                    self._refresh = asyncio.create_task(self._reload())
                await asyncio.shield(self._refresh)
        return self._keys.get(kid) if monotonic() < self._stale_at else None

    async def authenticate(self, request: Request) -> Identity | None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if self.dev_token and scheme.lower() == "bearer" and hmac.compare_digest(
            self.dev_token, token.encode("latin-1"),
        ):
            return "local"
        assertion = request.headers.get("Cf-Access-Jwt-Assertion")
        if not (self.issuer and assertion):
            return None
        try:
            header = jwt.get_unverified_header(assertion)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                return None
            key = await self._key(kid)
            if key is not None:
                jwt.decode(
                    assertion, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                    leeway=CLOCK_LEEWAY, options={"require": ["aud", "exp", "iss"]},
                )
                return "access"
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, TypeError, ValueError, OverflowError):
            pass
        return None


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None:
            return None
        port = parts.port
        return parts.scheme, parts.hostname, port if port is not None else (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def same_origin(request: Request, identity: Identity) -> bool:
    # The tunnel terminates HTTPS. Use its scheme on an authenticated Access request and the
    # preserved Host header. Never use X-Forwarded-Host or derive the expected origin from Referer.
    scheme = request.url.scheme
    if identity == "access" and request.headers.get("X-Forwarded-Proto") == "https":
        scheme = "https"
    expected = _origin(f"{scheme}://{request.headers.get('Host', '')}")
    origin = request.headers.get("Origin", request.headers.get("Referer", ""))
    return expected is not None and _origin(origin) == expected
