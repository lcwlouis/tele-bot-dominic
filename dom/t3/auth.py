"""Supervised OAuth sign-in for the local T3 gateway."""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from dom.t3.client import TIMEOUT, T3Unavailable, cli_json, local_base, request
from dom.t3.service import BASE_DIR, _package_entry

TOKEN_FILE = "t3-token.json"
REDIRECT = "http://127.0.0.1:39732/callback"
# What the sign-in asks T3 Code for: reading projects and threads, and operating them (Dom only sends after a tap).
SCOPES = ("orchestration:read", "orchestration:operate")


def command(config):
    return [str(Path(config.node).expanduser()), str(_package_entry(config.version))]


def connect_home(home, config):
    try:
        cli = command(config)
    except ValueError:
        raise T3Unavailable("Install the configured T3 package first.") from None
    connect(config.base_url, home / TOKEN_FILE, cli, str(Path.home() / BASE_DIR))


def connect(base_url, token_path, cli, base_dir, *, transport=None, runner=subprocess.run):
    base = local_base(base_url)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    resource = base + "/mcp"
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT, trust_env=False) as http:
            registered, _ = request(http, base + "/oauth/mcp/register", json={
                "client_name": "Dom", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code"],
                "response_types": ["code"], "token_endpoint_auth_method": "none",
            })
            client_id = registered["client_id"]
            pairing = cli_json(cli, [
                "auth", "pairing", "create", "--base-dir", base_dir, *(arg for scope in SCOPES for arg in ("--scope", scope)),
                "--ttl", "5m", "--label", "Dom", "--json",
            ], runner)
            decision, _ = request(http, base + "/oauth/mcp/decision", json={
                "authorization": {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                  "code_challenge": challenge, "code_challenge_method": "S256",
                                  "state": state, "resource": resource},
                "decision": {"_tag": "pairing-code", "access": "approval-required", "code": pairing["credential"]},
            })
            redirect = urlsplit(decision["redirectTo"])
            query = parse_qs(redirect.query)
            if redirect._replace(query="", fragment="").geturl() != REDIRECT or query.get("state") != [state]:
                raise ValueError
            token, _ = request(http, base + "/oauth/mcp/token", data={
                "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
                "client_id": client_id, "code_verifier": verifier, "resource": resource,
            })
        payload = {"access_token": token["access_token"], "expires_at": time.time() + int(token["expires_in"]),
                   "client_id": client_id}
        with tempfile.NamedTemporaryFile(mode="w", dir=Path(token_path).parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with stream:
                    json.dump(payload, stream)
                os.replace(temporary, token_path)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, KeyError):
        raise T3Unavailable("T3 sign-in failed.") from None
