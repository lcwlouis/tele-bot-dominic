"""Authenticated encryption at rest (AES-256-GCM + HKDF-SHA256) and E101 secure secret requests.

The encrypted store (`SecretStore`) is intentionally small and namespace-scoped so W3 (saved desktop logins and
E101 owner secrets) and W4 (Google Calendar / Gmail OAuth tokens) share one encryption layer, one `.env` master
key (`DOM_MASTER_KEY`), and one crash-safe rotation path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from dom.config import Config, _read_dotenv, set_dotenv, set_dotenv_many
from dom.notice import dashboard_url, send_notice

if TYPE_CHECKING:
    from dom.store import Store
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

MASTER_KEY_ENV = "DOM_MASTER_KEY"
PREVIOUS_KEY_ENV = "DOM_MASTER_KEY_PREVIOUS"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_ENVELOPE_VERSION = "v1"
_HKDF_SALT = b"dom-secret-store-v1"
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_ENV_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

SECRET_REQUEST_TTL = timedelta(minutes=15)
SECRET_DISMISS_COOLDOWN = timedelta(minutes=10)
MAX_PENDING_SECRET_REQUESTS = 5
RESERVED_ENV_VARS = frozenset((
    MASTER_KEY_ENV,
    PREVIOUS_KEY_ENV,
    "TELEGRAM_BOT_TOKEN",
    "SANDBOX_API_KEY",
))
_DANGEROUS_ENV_EXACT = frozenset((
    "PATH", "IFS", "HOME", "SHELL", "USER", "LOGNAME", "TZ", "TMPDIR", "NO_PROXY",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
))
_DANGEROUS_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHON", "SSL_", "DOM_")
_DANGEROUS_ENV_SUFFIXES = ("_PROXY",)
SecretTarget = Literal["secret", "env", "login"]
SECRET_TARGETS = ("secret", "env", "login")
SECRET_LABEL_LIMIT = 80
SECRET_REASON_LIMIT = 240
SECRET_VALUE_LIMIT = 4096


class SecretStoreError(RuntimeError):
    """Raised when the master key is missing/invalid or encrypted data fails authentication."""


@dataclass(frozen=True)
class SecretMeta:
    namespace: str
    key: str
    key_id: str
    created_at: str
    updated_at: str


def generate_master_key() -> str:
    """Return a fresh 256-bit master key encoded as URL-safe base64 (44 printable ASCII chars, no spaces)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode("ascii")


def _decode_key(encoded: str, label: str = MASTER_KEY_ENV) -> bytes:
    text = encoded.strip()
    if not text:
        raise SecretStoreError(f"{label} is empty")
    if not _B64URL_RE.fullmatch(text):
        raise SecretStoreError(f"{label} is not valid base64url")
    try:
        padding = "=" * (-len(text) % 4)
        raw = base64.b64decode((text + padding).encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError):
        raise SecretStoreError(f"{label} is not valid base64url") from None
    if len(raw) != _KEY_BYTES:
        raise SecretStoreError(f"{label} must decode to {_KEY_BYTES} bytes")
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != text.rstrip("="):
        raise SecretStoreError(f"{label} is not canonical base64url")
    return raw


def key_fingerprint(raw_key: bytes) -> str:
    """Non-secret 12-hex-char identifier for a master key."""
    return hashlib.sha256(b"dom-key-id:" + raw_key).hexdigest()[:12]


def _derive_subkey(raw_key: bytes, namespace: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_HKDF_SALT,
        info=f"namespace:{namespace}".encode("utf-8"),
    ).derive(raw_key)


def _aad(namespace: str, key: str) -> bytes:
    return f"{_ENVELOPE_VERSION}:{namespace}:{key}".encode("utf-8")


def _encrypt(raw_key: bytes, namespace: str, key: str, payload: dict[str, Any] | str) -> tuple[str, str]:
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(_NONCE_BYTES)
    subkey = _derive_subkey(raw_key, namespace)
    ct = AESGCM(subkey).encrypt(nonce, plaintext, _aad(namespace, key))
    kid = key_fingerprint(raw_key)
    token = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    return kid, f"{_ENVELOPE_VERSION}:{kid}:{token}"


def _decrypt(candidate_keys: list[bytes], namespace: str, key: str, envelope: str) -> dict[str, Any] | str:
    parts = envelope.split(":", 2)
    if len(parts) != 3 or parts[0] != _ENVELOPE_VERSION:
        raise SecretStoreError("unsupported ciphertext format")
    _, expected_kid, encoded = parts
    if not _B64URL_RE.fullmatch(encoded):
        raise SecretStoreError("corrupt ciphertext encoding")
    try:
        blob = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError):
        raise SecretStoreError("corrupt ciphertext encoding") from None
    if len(blob) < _NONCE_BYTES + 16:
        raise SecretStoreError("ciphertext is truncated")
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    # Try the key matching expected_kid first, then any remaining configured key.
    ordered = sorted(candidate_keys, key=lambda raw: key_fingerprint(raw) != expected_kid)
    aad = _aad(namespace, key)
    for raw_key in ordered:
        try:
            plaintext = AESGCM(_derive_subkey(raw_key, namespace)).decrypt(nonce, ct, aad)
        except InvalidTag:
            continue
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SecretStoreError("decrypted payload is not valid JSON") from None
        if not isinstance(data, (dict, str)):
            raise SecretStoreError("unexpected secret payload type")
        return data
    raise SecretStoreError("could not decrypt secret with the configured master key")


def _validate_namespace_and_key(namespace: str, key: str) -> None:
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("invalid secret namespace")
    if not isinstance(key, str) or not _KEY_NAME_RE.fullmatch(key):
        raise ValueError("invalid secret key")


def mask_username(username: str) -> str:
    """Return a masked display hint for a username or email without exposing the full identifier."""
    text = username.strip()
    if not text:
        return ""
    if "@" in text:
        local, _, domain = text.partition("@")
        if len(local) <= 1:
            masked_local = "*"
        elif len(local) <= 3:
            masked_local = local[0] + "***"
        else:
            masked_local = local[:2] + "***"
        return f"{masked_local}@{domain}"
    if len(text) <= 2:
        return text[0] + "***"
    return text[:2] + "***"


_active_redactions: ContextVar[set[str] | None] = ContextVar("dom_active_redactions", default=None)


@contextmanager
def secret_redaction_scope(initial: Iterable[str] = ()) -> Iterator[set[str]]:
    """Scope a per-run redaction set (e.g. for a desktop task) so `redact_secrets` scrubs those values everywhere."""
    outer = _active_redactions.get()
    scoped: set[str] = set(outer) if outer else set()
    for item in initial:
        if isinstance(item, str) and item:
            scoped.add(item)
    token = _active_redactions.set(scoped)
    try:
        yield scoped
    finally:
        scoped.clear()
        _active_redactions.reset(token)


def register_redaction(*values: str) -> None:
    """Register secret values into the active per-run redaction scope, if one is active."""
    scoped = _active_redactions.get()
    if scoped is None:
        return
    for item in values:
        if isinstance(item, str) and item:
            scoped.add(item)


def active_redactions() -> tuple[str, ...]:
    """Return currently active per-run redaction values (for W2 tracers and redaction boundaries)."""
    scoped = _active_redactions.get()
    return tuple(scoped) if scoped else ()


def redact_secrets(text: str, config: Config | None = None, extra: Iterable[str] = ()) -> str:
    """Replace known `.env` secrets (including DOM_MASTER_KEY), active per-run secrets, and `extra` with `[redacted]`.

    Designed to be called by both W3 redaction boundaries and W2's debug tracer before writing anything to disk.
    """
    if not text:
        return text
    values: list[str] = []
    if config is not None:
        try:
            env = _read_dotenv(config.home / ".env")
        except (OSError, UnicodeDecodeError):
            env = {}
        for name in config.secret_names():
            val = env.get(name) or os.environ.get(name)
            if val and len(val) >= 6:
                values.append(val)
    if scoped := _active_redactions.get():
        for item in scoped:
            if isinstance(item, str) and len(item) >= 4:
                values.append(item)
    for item in extra:
        if isinstance(item, str) and len(item) >= 4:
            values.append(item)
    # Longest first so substrings cannot partially break redaction.
    for secret in sorted(set(values), key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _slug_key(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")[:48]
    return slug or "secret"


class SecretStore:
    """Encrypted key-value store backed by SQLite `encrypted_secrets` and `.env` DOM_MASTER_KEY."""

    def __init__(self, store: Store, home: Path):
        self.store = store
        self.home = Path(home)
        self._key_lock = asyncio.Lock()

    def _read_env_value(self, name: str) -> str | None:
        try:
            val = _read_dotenv(self.home / ".env").get(name)
        except (OSError, UnicodeDecodeError):
            val = None
        return val or os.environ.get(name) or None

    def _load_keys(self) -> tuple[bytes | None, bytes | None]:
        primary_raw = self._read_env_value(MASTER_KEY_ENV)
        previous_raw = self._read_env_value(PREVIOUS_KEY_ENV)
        primary = _decode_key(primary_raw, MASTER_KEY_ENV) if primary_raw else None
        previous = _decode_key(previous_raw, PREVIOUS_KEY_ENV) if previous_raw else None
        return primary, previous

    def _ensure_primary_key_sync(self, existing_count: int = 0) -> bytes:
        primary, previous = self._load_keys()
        if primary is not None:
            return primary
        if existing_count > 0 or previous is not None:
            raise SecretStoreError(
                f"{MASTER_KEY_ENV} is missing while {existing_count} encrypted secret(s) already exist; "
                f"restore {MASTER_KEY_ENV} in .env before writing new secrets."
            )
        encoded = generate_master_key()
        set_dotenv(self.home, MASTER_KEY_ENV, encoded)
        return _decode_key(encoded, MASTER_KEY_ENV)

    async def put(self, namespace: str, key: str, payload: dict[str, Any] | str) -> str:
        """Encrypt `payload` under (namespace, key) and upsert atomically. Returns the key_id used."""
        _validate_namespace_and_key(namespace, key)
        if not isinstance(payload, (dict, str)):
            raise ValueError("secret payload must be a dict or string")
        async with self._key_lock:
            existing_count = await self.store.count_encrypted_secrets()
            raw_key = await asyncio.to_thread(self._ensure_primary_key_sync, existing_count)
            kid, envelope = _encrypt(raw_key, namespace, key, payload)
            now = datetime.now(timezone.utc).isoformat()
            await self.store.put_encrypted_secret(namespace, key, kid, envelope, now)
            return kid

    async def get(self, namespace: str, key: str) -> dict[str, Any] | str | None:
        """Decrypt and return payload under (namespace, key), or None if absent. Fails closed on key/auth errors."""
        _validate_namespace_and_key(namespace, key)
        row = await self.store.get_encrypted_secret(namespace, key)
        if row is None:
            return None
        primary, previous = await asyncio.to_thread(self._load_keys)
        keys = [k for k in (primary, previous) if k is not None]
        if not keys:
            raise SecretStoreError(f"{MASTER_KEY_ENV} is not configured")
        return _decrypt(keys, namespace, key, row["ciphertext"])

    async def delete(self, namespace: str, key: str) -> bool:
        _validate_namespace_and_key(namespace, key)
        return await self.store.delete_encrypted_secret(namespace, key)

    async def exists(self, namespace: str, key: str) -> bool:
        _validate_namespace_and_key(namespace, key)
        return await self.store.get_encrypted_secret(namespace, key) is not None

    async def list_keys(self, namespace: str) -> list[SecretMeta]:
        if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
            raise ValueError("invalid secret namespace")
        rows = await self.store.list_encrypted_secrets(namespace)
        return [
            SecretMeta(
                namespace=row["namespace"],
                key=row["key"],
                key_id=row["key_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def key_status(self) -> dict[str, Any]:
        """Return non-secret master key status for the dashboard and diagnostics."""
        error = ""
        primary = previous = None
        try:
            primary, previous = await asyncio.to_thread(self._load_keys)
        except SecretStoreError as exc:
            error = str(exc)
        total = await self.store.count_encrypted_secrets()
        return {
            "configured": primary is not None,
            "key_id": key_fingerprint(primary) if primary is not None else None,
            "previous_present": previous is not None,
            "total_entries": total,
            "error": error,
        }

    async def rotate_key(self) -> tuple[str, int]:
        """Crash- and cancellation-safely rotate DOM_MASTER_KEY and re-encrypt every row in encrypted_secrets.

        Protocol:
        1. Decrypt every existing row in memory with the current key(s) to ensure nothing is unreadable.
        2. If DOM_MASTER_KEY_PREVIOUS is present on entry (prior interrupted rotation), finish that rotation first
           by re-encrypting all rows under the current primary key and clearing DOM_MASTER_KEY_PREVIOUS before
           minting a new key. If that fails, `.env` is untouched and every row remains readable.
        3. Capture the exact `.env` values of MASTER and PREVIOUS, then atomically stage
           DOM_MASTER_KEY=<new> and DOM_MASTER_KEY_PREVIOUS=<old> in `.env`.
        4. Re-encrypt every row under `<new>` inside one SQLite transaction, run as its own task. A cancel does not
           interrupt it (its worker thread cannot be stopped): we wait for the real outcome. On failure the exact
           pre-stage `.env` values are restored; on commit the new key stays.
        5. Atomically remove DOM_MASTER_KEY_PREVIOUS from `.env`, then re-raise any cancel that arrived.
        """
        async with self._key_lock:
            primary, previous = await asyncio.to_thread(self._load_keys)
            rows = await self.store.all_encrypted_secrets()
            keys = [k for k in (primary, previous) if k is not None]
            if rows and not keys:
                raise SecretStoreError(f"cannot rotate key: {MASTER_KEY_ENV} is not configured")
            decrypted: list[tuple[str, str, dict[str, Any] | str]] = []
            for row in rows:
                payload = _decrypt(keys, row["namespace"], row["key"], row["ciphertext"])
                decrypted.append((row["namespace"], row["key"], payload))

            # Step 2: Finish any interrupted prior rotation before touching .env for a new rotation.
            if previous is not None:
                if primary is None:
                    raise SecretStoreError(
                        f"cannot finish interrupted rotation: {MASTER_KEY_ENV} is missing while {PREVIOUS_KEY_ENV} is set"
                    )
                if decrypted:
                    now_finish = datetime.now(timezone.utc).isoformat()
                    finish_reencrypted = [
                        (ns, k, *_encrypt(primary, ns, k, payload), now_finish)
                        for ns, k, payload in decrypted
                    ]
                    try:
                        await self.store.replace_encrypted_secrets(finish_reencrypted)
                    except Exception as exc:
                        raise SecretStoreError(
                            "could not finish interrupted key rotation; existing keys left unchanged"
                        ) from exc
                await asyncio.to_thread(set_dotenv_many, self.home, {PREVIOUS_KEY_ENV: None})

            pre_env = await asyncio.to_thread(_read_dotenv, self.home / ".env")
            pre_master_in_env = pre_env.get(MASTER_KEY_ENV)
            pre_prev_in_env = pre_env.get(PREVIOUS_KEY_ENV)
            old_encoded = pre_master_in_env or os.environ.get(MASTER_KEY_ENV) or None

            new_encoded = generate_master_key()
            new_raw = _decode_key(new_encoded, MASTER_KEY_ENV)
            new_kid = key_fingerprint(new_raw)
            now = datetime.now(timezone.utc).isoformat()
            reencrypted = [
                (ns, k, *_encrypt(new_raw, ns, k, payload), now)
                for ns, k, payload in decrypted
            ]

            stage_env: dict[str, str | None] = {MASTER_KEY_ENV: new_encoded}
            if old_encoded:
                stage_env[PREVIOUS_KEY_ENV] = old_encoded
            await asyncio.to_thread(set_dotenv_many, self.home, stage_env)
            # The re-encrypt runs in a worker thread that cancelling this await cannot stop, so it runs as its own
            # task and we always wait for its real outcome before deciding which key `.env` must hold.
            write = asyncio.ensure_future(self.store.replace_encrypted_secrets(reencrypted))
            cancelled: asyncio.CancelledError | None = None
            while not write.done():
                try:
                    await asyncio.wait({write})
                except asyncio.CancelledError as exc:
                    cancelled = exc
            failure: BaseException | None = (
                asyncio.CancelledError() if write.cancelled() else write.exception()
            )
            # Synchronous so the `.env` fix-up still completes if another cancel arrives.
            if failure is not None:
                rollback: dict[str, str | None] = {
                    MASTER_KEY_ENV: pre_master_in_env,
                    PREVIOUS_KEY_ENV: pre_prev_in_env,
                }
                try:
                    set_dotenv_many(self.home, rollback)
                except Exception:
                    log.exception("failed to restore .env after secret rotation rollback")
                if cancelled is not None:
                    raise cancelled
                if isinstance(failure, Exception):
                    raise SecretStoreError("could not re-encrypt secrets; rolled back") from failure
                raise failure
            set_dotenv_many(self.home, {PREVIOUS_KEY_ENV: None})
            if cancelled is not None:
                raise cancelled
            return new_kid, len(reencrypted)


# ---- E101: Secure secret requests -------------------------------------------------------------------------------


def _is_dangerous_env_name(name: str) -> bool:
    return (
        name in _DANGEROUS_ENV_EXACT
        or name.startswith(_DANGEROUS_ENV_PREFIXES)
        or name.endswith(_DANGEROUS_ENV_SUFFIXES)
    )


def reserved_env_vars(config: Config) -> frozenset[str]:
    names = set(RESERVED_ENV_VARS)
    if config.web.dev_token_env:
        names.add(config.web.dev_token_env)
    if config.desktop.api_key_env:
        names.add(config.desktop.api_key_env)
    for host in config.desktop_hosts:
        if host.api_key_env:
            names.add(host.api_key_env)
    return frozenset(names)


def requestable_env_vars(config: Config) -> frozenset[str]:
    """Only `.env` variable names referenced by `config.toml` (model/provider/backup API keys, studio token, KLIPY),
    excluding reserved critical keys and dangerous process/environment variables."""
    reserved = reserved_env_vars(config)
    return frozenset(
        name for name in config.secret_names()
        if _ENV_NAME_RE.fullmatch(name) and name not in reserved and not _is_dangerous_env_name(name)
    )


def _validate_requestable_env_key(name: str, config: Config) -> None:
    if not name or not _ENV_NAME_RE.fullmatch(name):
        raise ValueError("env target requires an uppercase variable name like OPENROUTER_API_KEY")
    if name in reserved_env_vars(config) or _is_dangerous_env_name(name):
        raise ValueError(f"{name} is a reserved variable and cannot be requested by Dom")
    if name not in requestable_env_vars(config):
        raise ValueError(
            f"{name} is not referenced in config.toml; use target='secret' for general secrets."
        )


class SecretRequests:
    """Manages E101 one-time dashboard secret entry requests initiated by Dom in the owner DM."""

    def __init__(self, app: App):
        self.app = app

    def _now(self) -> datetime:
        return self.app.dispatch.now().astimezone(timezone.utc)

    async def create(
        self,
        *,
        label: str,
        reason: str,
        target: SecretTarget = "secret",
        key: str | None = None,
    ) -> dict[str, Any]:
        """Create a pending one-time secret request, send the owner-DM card, and return non-secret metadata."""
        app = self.app
        if app.owner_id is None:
            raise ValueError("owner is not claimed yet")
        clean_label = " ".join((label or "").split()).strip()
        clean_reason = " ".join((reason or "").split()).strip()
        if not clean_label or len(clean_label) > SECRET_LABEL_LIMIT:
            raise ValueError(f"label must be 1 to {SECRET_LABEL_LIMIT} characters")
        if not clean_reason or len(clean_reason) > SECRET_REASON_LIMIT:
            raise ValueError(f"reason must be 1 to {SECRET_REASON_LIMIT} characters")
        if target not in SECRET_TARGETS:
            raise ValueError("target must be secret, env, or login")

        now = self._now()
        await self.expire_pending()
        latest_cancel = await app.store.latest_cancelled_secret_request()
        if latest_cancel and latest_cancel > (now - SECRET_DISMISS_COOLDOWN).isoformat():
            raise ValueError("The owner recently dismissed a secret request; wait before asking again.")
        pending = await app.store.pending_secret_requests(now.isoformat())
        if len(pending) >= MAX_PENDING_SECRET_REQUESTS:
            raise ValueError(
                f"{len(pending)} secret requests are already waiting for the owner; wait for them to finish first."
            )

        raw_key = (key or "").strip()
        if target == "env":
            _validate_requestable_env_key(raw_key, app.config)
            target_key = raw_key
        elif target == "login":
            from dom.desktop.permissions import normalize_site

            if raw_key:
                site = normalize_site(raw_key)
                if site is None:
                    raise ValueError("login target requires a valid public site hostname like example.org")
                target_key = site
            else:
                target_key = ""
        else:
            target_key = raw_key if raw_key and _KEY_NAME_RE.fullmatch(raw_key) else _slug_key(clean_label)

        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        expires_at = (now + SECRET_REQUEST_TTL).isoformat()
        request_id = await app.store.create_secret_request(
            label=clean_label,
            reason=clean_reason,
            target=target,
            target_key=target_key,
            token_hash=token_hash,
            created_at=now.isoformat(),
            expires_at=expires_at,
        )
        await app.store.record_owner_event(f"Dom requested secret '{clean_label}' (#{request_id})")
        await self._send_card(request_id, clean_label, clean_reason, target, target_key, token)
        return {"request_id": request_id, "status": "requested", "label": clean_label}

    @staticmethod
    def _destination_text(target: str, target_key: str) -> str:
        if target == "env":
            return f"Destination: .env variable {target_key}"
        if target == "login":
            return f"Destination: saved desktop login for {target_key}" if target_key else "Destination: saved desktop login"
        return f"Destination: encrypted credential store ({target_key})"

    async def _send_card(
        self,
        request_id: int,
        label: str,
        reason: str,
        target: str,
        target_key: str,
        token: str,
    ) -> None:
        app = self.app
        if app.owner_id is None:
            return
        # Put the one-time token in the URL fragment (#t=...) so it never appears in HTTP GET access logs.
        base = dashboard_url(app.config.web.public_url, "/app/secret")
        link = f"{base}#t={token}" if base else None
        buttons: list[InlineKeyboardButton] = []
        if link:
            buttons.append(InlineKeyboardButton(text="Enter secret securely", url=link))
        buttons.append(InlineKeyboardButton(text="Cancel", callback_data=f"sreq:cancel:{request_id}"))
        details = [
            reason,
            self._destination_text(target, target_key),
        ]
        tag = "expires in 15 min · never send secrets in chat" if link else "open Waiting in dashboard · expires in 15 min"
        try:
            sent = await send_notice(
                app, app.owner_id, f"Secret requested: {label}", details, tag=tag, buttons=buttons,
            )
        except Exception:
            log.exception("could not send secret request card #%s", request_id)
            return
        if isinstance(message_id := getattr(sent, "message_id", None), int):
            await app.store.set_secret_request_card(request_id, message_id)

    async def _settle_card(self, message_id: int | None, label: str) -> None:
        app = self.app
        if not message_id or message_id < 0 or app.owner_id is None:  # no card, or one the old bot sent
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="noop")]])
        try:
            from dom.telegram.executor import retry_telegram

            await retry_telegram(
                lambda: app.bot.edit_message_reply_markup(
                    chat_id=app.owner_id, message_id=message_id, reply_markup=markup,
                ),
                sleep=app.sleep,
            )
        except TelegramAPIError:
            log.info("could not settle secret request card %s", message_id)

    async def expire_pending(self) -> int:
        now = self._now().isoformat()
        expired = await self.app.store.expire_secret_requests(now)
        for row in expired:
            await self._settle_card(row.get("card_message_id"), "Expired")
        return len(expired)

    async def pending_list(self) -> list[dict[str, Any]]:
        await self.expire_pending()
        rows = await self.app.store.pending_secret_requests(self._now().isoformat())
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "reason": row["reason"],
                "target": row["target"],
                "target_key": row["target_key"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }
            for row in rows
        ]

    async def lookup(self, *, token: str | None = None, request_id: int | None = None) -> dict[str, Any] | None:
        """Inspect a pending secret request for the authenticated owner dashboard form."""
        await self.expire_pending()
        now = self._now().isoformat()
        row = None
        if token:
            row = await self.app.store.get_secret_request_by_hash(_hash_token(token))
        elif request_id is not None:
            row = await self.app.store.get_secret_request(request_id)
        if row is None or row["status"] != "pending" or row["expires_at"] <= now:
            return None
        overwrites = False
        if row["target"] == "env":
            overwrites = bool(await asyncio.to_thread(self.app.config.secret, row["target_key"]))
        elif row["target"] == "secret":
            overwrites = await self.app.secrets.exists("owner_secret", row["target_key"])
        return {
            "id": row["id"],
            "label": row["label"],
            "reason": row["reason"],
            "target": row["target"],
            "target_key": row["target_key"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "overwrites_existing": overwrites,
        }

    async def submit(
        self,
        *,
        token: str | None = None,
        request_id: int | None = None,
        value: str | None = None,
        site: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Atomically consume a pending secret request and store the secret. Never returns or logs the secret."""
        app = self.app
        await self.expire_pending()
        now_iso = self._now().isoformat()
        row = None
        if token:
            row = await app.store.get_secret_request_by_hash(_hash_token(token))
            if row is not None and not hmac.compare_digest(row["token_hash"], _hash_token(token)):
                row = None
        elif request_id is not None:
            row = await app.store.get_secret_request(request_id)
        if row is None or row["status"] != "pending" or row["expires_at"] <= now_iso:
            raise ValueError("This secret request has expired or was already used.")

        target = row["target"]
        target_key = row["target_key"]
        label = row["label"]

        # Validate everything BEFORE claiming or writing, so invalid input never burns the one-time request.
        effective_site = ""
        clean_username = ""
        if target == "env":
            _validate_requestable_env_key(target_key, app.config)
            if not isinstance(value, str) or not _ENV_VALUE_RE.fullmatch(value):
                raise ValueError("An .env value must be 1 to 512 printable ASCII characters with no spaces.")
        elif target == "login":
            from dom.desktop.permissions import normalize_site

            effective_site = (site or target_key or "").strip()
            if normalize_site(effective_site) is None:
                raise ValueError("Enter a public site hostname like example.org (no IPs, LAN names, or shared suffixes).")
            if not isinstance(password, str) or not password or len(password) > SECRET_VALUE_LIMIT:
                raise ValueError(f"Enter a password (up to {SECRET_VALUE_LIMIT:,} characters).")
            clean_username = (username or "").strip()
            if len(clean_username) > 320:
                raise ValueError("Username is too long.")
        else:
            _validate_namespace_and_key("owner_secret", target_key)
            if not isinstance(value, str) or not value or len(value) > SECRET_VALUE_LIMIT:
                raise ValueError(f"Enter a secret value (1 to {SECRET_VALUE_LIMIT} characters).")

        # Claim (`pending` -> `submitting`), write, and only mark `submitted` once the write succeeds.
        # If writing fails for any reason, revert to `pending` so the owner can retry.
        claimed = await app.store.claim_secret_request(row["id"])
        if not claimed:
            raise ValueError("This secret request has expired or was already used.")
        try:
            if target == "env":
                assert isinstance(value, str)
                await asyncio.to_thread(set_dotenv, app.home.root, target_key, value)
            elif target == "login":
                assert isinstance(password, str)
                await app.desktop_permissions.save_login(
                    site=effective_site,
                    label=label,
                    username=clean_username,
                    password=password,
                )
            else:
                assert isinstance(value, str)
                await app.secrets.put("owner_secret", target_key, {"value": value, "label": label})
                await app.store.upsert_owner_secret(target_key, label, now_iso)
        except BaseException:
            try:
                await app.store.reopen_secret_request(row["id"])
            except Exception:
                log.exception("failed to revert secret request #%s back to pending", row["id"])
            raise

        if not await app.store.complete_secret_request(row["id"], "submitted", now_iso):
            log.warning("secret request #%s was set but its row could not be marked submitted", row["id"])
        await self._settle_card(row.get("card_message_id"), "✅ Secret set")
        # Dom learns ONLY that the secret is set — never its value.
        await app.store.record_owner_event(f"Owner provided secret '{label}' (status: set).")
        return {"ok": True, "id": row["id"], "label": label, "status": "set"}

    async def cancel(self, *, token: str | None = None, request_id: int | None = None) -> bool:
        now_iso = self._now().isoformat()
        row = None
        if token:
            row = await self.app.store.get_secret_request_by_hash(_hash_token(token))
        elif request_id is not None:
            row = await self.app.store.get_secret_request(request_id)
        if row is None or row["status"] != "pending":
            return False
        changed = await self.app.store.resolve_secret_request(row["id"], "cancelled", now_iso)
        if not changed:
            return False
        await self._settle_card(row.get("card_message_id"), "❌ Cancelled")
        await self.app.store.record_owner_event(f"Owner cancelled secret request '{row['label']}' (#{row['id']}).")
        return True
