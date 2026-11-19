"""Validate the owner dashboard's Cloudflare Access assertion or local bearer token."""

from __future__ import annotations

import asyncio
import hmac
import re
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import httpx
import jwt
from starlette.requests import Request

from dom.config import Config

JWKS_TTL = 3600
_TEAM = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII)
Identity = Literal["access", "local"]


class Auth:
    def __init__(self, config: Config, fetch_jwks: Callable[[str], Awaitable[dict]] | None = None):
        web = config.web
        if (web.access_team or web.access_aud) and not (
            _TEAM.fullmatch(web.access_team) and web.access_aud.strip()
        ):
            raise ValueError("Dashboard auth needs a valid access_team and access_aud.")
        self.issuer = f"https://{web.access_team.lower()}.cloudflareaccess.com" if web.access_team else ""
        self.audience = web.access_aud
        token = config.secret(web.dev_token_env) if web.dev_token_env else None
        self.dev_token = token.encode("utf-8") if token and token.strip() else b""
        if not (self.issuer or self.dev_token):
            raise ValueError("Dashboard auth needs access_team and access_aud, or a populated dev_token_env.")
        self._fetch_jwks = fetch_jwks or self._fetch
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    async def _fetch(url: str) -> dict:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        async with self._lock:
            if monotonic() >= self._expires_at or kid not in self._keys:
                jwks = await self._fetch_jwks(f"{self.issuer}/cdn-cgi/access/certs")
                keys = {}
                for entry in jwks["keys"]:
                    if (
                        isinstance(entry, dict) and isinstance(entry.get("kid"), str)
                        and entry.get("kty") == "RSA" and entry.get("use", "sig") == "sig"
                        and entry.get("alg", "RS256") == "RS256"
                    ):
                        keys[entry["kid"]] = jwt.PyJWK(entry, algorithm="RS256")
                self._keys = keys
                self._expires_at = monotonic() + JWKS_TTL
            return self._keys.get(kid)

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
                    options={"require": ["aud", "exp", "iss"]},
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
        return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
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
