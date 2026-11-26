# Credentials, Desktop Permissions, and Secure Secret Requests (v0.8.5 W3)

Status: Design & security specification (implements R4, R5, E100, E101; encrypted store shared with W4).

## 1. Goals and scope

v0.8.5 gives Dom hands on his desktop browser without reverting to all-or-nothing trust or asking the owner to paste secrets into Telegram chat. This spec defines four tightly coupled pieces:

1. **Encrypted secret store (`dom/secrets.py`)**: authenticated encryption at rest for saved site logins (R5), owner-supplied secrets (E101), and future integration tokens (W4 Google Calendar/Gmail OAuth tokens).
2. **Desktop browser permissions (R4, E100)**: per-action defaults (`Ask`, `Allow`, `Never`) and per-site rules across five desktop action kinds, owner-DM approval cards with **Reject**, **Allow once**, and **Allow always**, and an auditable approvals history.
3. **Saved logins & zero-model fill path (R5)**: dashboard management of masked site logins, filling into the sandbox browser without the password ever touching a model, a traced sandbox HTTP endpoint, a log line, a turn log, a usage row, or W2's debug tracer, plus auto password mode in the live viewer.
4. **Secure secret requests (E101)**: an owner-DM-only tool (`request_secret`) that sends a one-time link to a dashboard entry form so Dom can ask for a secret and learn only that it is set.

Every capability in this spec is **owner-only**. None of these tools, routes, credentials, or permission cards exist in groups or friends' DMs.

---

## 2. Threat model

### 2.1 Assets to protect
- **Saved login credentials** (usernames and passwords for external sites).
- **Owner-supplied secrets** stored via E101 (`.env` values and encrypted credential-store entries).
- **OAuth tokens** (W4) stored in the same encrypted store.
- **Master encryption key** (`DOM_MASTER_KEY` in `DOM_HOME/.env`).
- **Desktop browser session & host boundaries** (preventing unauthorized navigation, login fill, file exfiltration, or file upload).

### 2.2 Adversaries and mitigations

| Adversary | Capabilities | Risks | Mitigations |
|---|---|---|---|
| **Prompt-injected or misbehaving model** (chat model or desktop worker model) | Chooses tool names and arguments; reads tool results, page markdown, element lists, and screenshots; writes `finish` summaries and skill drafts. | Exfiltrating a password into a chat bubble, skill draft, web request, or `finish` result; filling a login onto a phishing site; navigating to attacker domains; downloading/uploading files without consent; spamming secret requests or overwriting critical `.env` keys (`TELEGRAM_BOT_TOKEN`, `DOM_MASTER_KEY`). | Passwords never enter any model prompt, tool argument, or tool result. `fill_saved_login` takes only an opaque `login_id` and verifies the active tab's `https://` origin matches the saved login's site. `fill_saved_login` fills via VNC keystrokes only after verifying focus is on a top-level `input[type="password"]`. R4 gates site visits, login fills, downloads, uploads, and background work. `request_secret` is rate-limited, forbids reserved env vars, and shows the exact destination to the owner. |
| **Malicious web page** | Controls HTML/JS/CSS rendered in Chromium inside the sandbox; can redirect, open iframes, swap input types (`password` → `text`), or inject prompt text into page markdown. | Tricking Dom into filling a saved login on the wrong origin or into a visible text box (readable by `read_page` / `look`); cross-origin iframe credential theft; tricking the worker into uploading local files or downloading malicious files. | Origin check reads the browser's top-level active tab URL (`current_url()`) and enforces `https://` + site match. Pre-fill JS verifies the focused element is a top-level (non-iframe, non-cross-origin) `input[type="password"]` and returns only a strict boolean (`True` literal) treated as hostile input. All page output stays wrapped in `<untrusted_tool_result>`. Self-directed tasks (`work_own`) run with an empty profile and have no access to saved logins or shell tools. |
| **Group member or friend in DM** | Sends messages in approved groups or friend DMs; may try prompt injection or forged callback queries. | Discovering saved logins, triggering `request_secret`, triggering desktop tasks, or approving cards. | Enforced in code (never prompt wording alone): `request_secret` and `start_desktop_task` are omitted from non-owner tool schemas and rejected inside their handlers (`ctx.is_owner_dm()`). Telegram callbacks verify `query.from_user.id == app.owner_id`, `not query.from_user.is_bot`, `query.message.chat.id == app.owner_id`, and `chat.type == "private"`. Dashboard endpoints sit behind Cloudflare Access / owner dev-token auth and same-origin checks. |
| **Debug tracer (W2), logs, turn log, usage rows** | W2 records full model requests/responses and tool calls across all chats; Python logs and SQLite `turns`/`usage` persist metadata. | Accidental capture of a plaintext password or one-time secret token in traces, logs, exceptions, or DB audit columns. | Saved-login plaintext never passes through the model client, tool argument/result serialization, `turns`, `usage`, or Python log arguments. Any error during VNC fill is caught and replaced with a generic `SandboxError` containing no secret bytes. A shared `redact_secrets(text, extra=...)` helper (covering `.env` secrets including `DOM_MASTER_KEY` plus any registered active secret values) is provided for W2's tracer and Dom's own redaction boundaries. One-time E101 tokens are transmitted in the URL fragment (`#t=`), submitted via JSON `POST`, and stored only as SHA-256 hashes. |
| **Sandbox HTTP API request tracing** | `agent-infra/sandbox` traces/logs HTTP API request payloads (including `/v1/browser/page/type`, `/v1/browser/page/fill`, `/v1/browser/page/evaluate`, `/v1/bash/exec`). | A password sent over HTTP API ends up in sandbox API logs, traces, or container telemetry. | Saved-login usernames and passwords are **never** sent to any `/v1/*` sandbox HTTP endpoint. They are injected exclusively as RFB `KeyEvent` frames over the authenticated `/websockify` WebSocket (the same transport the live viewer uses for manual password entry). |
| **Stolen `state.db` alone** | Offline read access to SQLite database (e.g. from a stray copy or crash snapshot). | Reading saved logins, owner secrets, or OAuth tokens. | All secret payloads are encrypted with AES-256-GCM using a key stored in `DOM_HOME/.env`, never in `state.db`. Ciphertexts include random 96-bit nonces and AAD bound to `(namespace, key)`. |
| **Stolen `.env` file alone** | Offline read access to `DOM_HOME/.env` (contains `DOM_MASTER_KEY` and service API keys, mode `0600`). | Without `state.db`, attacker gets `.env` tokens (Telegram, OpenRouter, etc.) but not saved logins or OAuth tokens. With **both** `.env` and `state.db` (e.g. a full `dom backup` archive, which is mode `0600`), attacker can decrypt stored secrets. | Key rotation (`dom rotate-key` or dashboard button) replaces `DOM_MASTER_KEY` and re-encrypts every row in one atomic transaction so an old leaked `.env` cannot decrypt newly rotated rows. Users should store backups encrypted or on trusted storage and rotate `DOM_MASTER_KEY` plus external credentials if `.env` is ever exposed. |
| **Hostile / compromised sandbox container** | Full control inside the sandbox container (Chromium process, DOM, cookies). | Once a password is typed into Chromium or a session cookie is established, code inside the container (or `run_shell`/`run_python` during an owner task) could inspect Chromium's memory/profile. | Accepted residual risk of any browser automation inside a container, mitigated by: (a) container isolation (no LAN/host access, verified before every start), (b) per-site explicit approval before filling (E100), (c) default `submit=true` pressing `Enter` immediately after filling so the password does not linger in the input DOM, and (d) self-directed work (`work_own`) running in an isolated container with an emptied browser profile and no shell/Python/saved-login tools. |

---

## 3. Key handling and encrypted store (`dom/secrets.py`)

### 3.1 Cryptographic design
- **Master key**: 32 cryptographically random bytes (`secrets.token_bytes(32)`), stored as URL-safe base64 without whitespace in `DOM_HOME/.env` under `DOM_MASTER_KEY`.
- **Subkey derivation**: per-namespace 32-byte key derived with HKDF-SHA256:
  - `IKM`: 32-byte decoded master key
  - `salt`: `b"dom-secret-store-v1"`
  - `info`: `f"namespace:{namespace}".encode("utf-8")`
- **Cipher**: AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`) with a fresh 96-bit random nonce (`os.urandom(12)`) per write.
- **Associated Data (AAD)**: `f"v1:{namespace}:{key}".encode("utf-8")`. Swapping ciphertext between rows or namespaces fails GCM authentication.
- **Key ID (`kid`)**: first 12 hex characters of `SHA-256(b"dom-key-id:" + raw_key)`. Stored alongside each ciphertext so Dom knows which key encrypted each row without storing any key material.
- **Envelope format**: `v1:<kid>:<base64url(nonce_12 || ciphertext || tag_16)>`.
- **Fail closed**: if `DOM_MASTER_KEY` is missing on read, malformed (not valid base64url decoding to 32 bytes), or GCM authentication fails, `SecretStore` raises `SecretStoreError`. It never falls back to plaintext or a weak/default key.
- **Bootstrapping**: on the first `put(...)` when neither `DOM_MASTER_KEY` in `DOM_HOME/.env` nor `os.environ["DOM_MASTER_KEY"]` is set, Dom generates a fresh 32-byte key and atomically writes it to `DOM_HOME/.env` with mode `0600` (using `dom.config.set_dotenv`).
- **Config protection**: `DOM_MASTER_KEY` and `DOM_MASTER_KEY_PREVIOUS` are added to `KNOWN_SECRETS` in `dom/config.py`, so `config.secret_names()`, `check_local_private_text()`, and redaction automatically protect them.

### 3.2 Crash-safe key rotation
Rotation runs via `SecretStore.rotate_key()`, CLI `dom rotate-key <home>`, or the dashboard's **Rotate encryption key** action (with confirmation).

1. Load current active key `old_key` (and `DOM_MASTER_KEY_PREVIOUS` if a prior rotation was interrupted) and verify **every** row in `encrypted_secrets` decrypts cleanly in memory before touching `.env` or SQLite.
2. Generate `new_key = secrets.token_bytes(32)`.
3. **Atomically update `.env`** in a single `os.replace` (mode `0600`) to set **both** `DOM_MASTER_KEY=<new_key>` and `DOM_MASTER_KEY_PREVIOUS=<old_key>`.
   - *Crash before step 4*: SQLite still holds rows encrypted with `old_key`; reads succeed via `DOM_MASTER_KEY_PREVIOUS`, and re-running rotation finishes cleanly.
4. **Atomically re-encrypt all rows in SQLite** inside one `BEGIN IMMEDIATE ... COMMIT` transaction, rewriting every ciphertext with a fresh nonce under `new_key` and updating `key_id` and `updated_at`.
   - *Failure in step 4*: SQLite rolls back; Dom restores `DOM_MASTER_KEY=<old_key>` and removes `DOM_MASTER_KEY_PREVIOUS` in `.env`, raising `SecretStoreError`.
5. **Atomically remove `DOM_MASTER_KEY_PREVIOUS`** from `.env` (mode `0600`).
   - *Crash before step 5*: all rows already decrypt with `DOM_MASTER_KEY`; `DOM_MASTER_KEY_PREVIOUS` is harmlessly unused until the next cleanup or rotation.

### 3.3 Storage schema & reusable API (for W3 and W4)
All encrypted blobs live in one table (`encrypted_secrets` in migration `0033_credentials_and_permissions.sql`):

```sql
CREATE TABLE IF NOT EXISTS encrypted_secrets (
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    key_id      TEXT NOT NULL,
    ciphertext  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
```

Public API of `dom.secrets.SecretStore`:
- `await secrets.put(namespace, key, payload)` — encrypts JSON-serializable `dict`/`str` and atomically upserts.
- `await secrets.get(namespace, key)` — decrypts and returns the payload, or `None` if absent.
- `await secrets.delete(namespace, key)` — removes the row; returns `bool`.
- `await secrets.exists(namespace, key)` — checks presence without decrypting.
- `await secrets.list_keys(namespace)` — returns `list[SecretMeta(namespace, key, key_id, created_at, updated_at)]` without decrypting.
- `await secrets.key_status()` — returns `{configured: bool, key_id: str | None, previous_present: bool, total_entries: int}` without exposing any key bytes.
- `await secrets.rotate_key()` — executes the 3-step rotation above and returns `(new_key_id, count)`.

Namespaces:
- `"login"` — W3 saved login secrets (key = `str(login_id)`, payload = `{"username": ..., "password": ...}`). Non-secret metadata (`id`, `site`, `label`, `username_masked`, `match_subdomains`, timestamps) lives in `saved_logins`.
- `"owner_secret"` — W3 E101 secrets stored in the credential store (key = slug, payload = `{"value": ..., "label": ...}`). Non-secret metadata lives in `owner_secrets`.
- `"google_oauth"` — W4 Google Calendar & Gmail OAuth tokens (reuses `SecretStore` directly with zero schema changes).

---

## 4. Zero-model, untraced saved-login fill path (R5)

### 4.1 Invariants
1. A saved login's plaintext password (and full username) **never** appears in:
   - any LLM request (`messages`, system prompt, tool definitions, tool call arguments, or tool call results);
   - any sandbox HTTP API request (`/v1/browser/page/type`, `/v1/browser/page/fill`, `/v1/browser/page/evaluate`, `/v1/bash/exec`, `/v1/file/write`, etc.);
   - any `turns` row, `usage` row, W2 debug trace, Telegram message, or Python `logging` record;
   - any dashboard `GET` response (passwords are write-only; usernames are returned masked, e.g. `al***` or a masked local-part with its domain).
2. Self-directed / background tasks (`work_own`) are never offered `saved_logins` or `fill_saved_login`.

### 4.2 How the worker uses saved logins
Owner-requested desktop tasks gain two tools:
- `saved_logins(site?: str)`: returns non-secret metadata only — `[{"id": 1, "site": "example.org", "label": "Main account", "username_hint": "al***", "has_username": true, "has_password": true}]`.
- `fill_saved_login(login_id: int, username_selector?: str, password_selector?: str, submit?: bool = true)`:
  1. Looks up `saved_logins` metadata for `login_id`.
  2. Reads `client.current_url()` from the sandbox and verifies:
     - scheme is strictly `https` (never `http`, `file`, `data`, `javascript`, `about`);
     - host matches `login.site` (`host == site` or `host == f"www.{site}"`, or subdomain only if `login.match_subdomains` is explicitly enabled and `site` is not a shared public suffix).
  3. Checks R4 permission for `("fill_login", login.site)`:
     - `never` → blocks immediately, records `blocked` in history, returns refusal text.
     - `ask` → pauses task, sends owner-DM approval card (**Allow once** / **Allow always** / **Reject**), waits up to `handoff_seconds` (pause time does not count against `work_seconds`).
     - `allow` (or approved) → proceeds.
  4. **DOM focus & verification (constant JS, zero secrets)**:
     - If `has_username`: calls a constant `/v1/browser/page/evaluate` script (or validated simple selector from `username_selector`) to focus the top-level username/email `<input>` (must not be `type="hidden"`, `readonly`, `disabled`, or inside an iframe). Verifies focus succeeded, then connects to `ws(s)://<sandbox>/websockify` with `Authorization: Bearer <sandbox_api_key>`, completes the RFB 3.8 handshake (shared mode), sends `Ctrl+A`, `BackSpace`, and the username characters as RFB `KeyEvent` frames.
     - Focuses the password input via validated `password_selector` or auto-detected visible top-level `input[type="password"]`.
     - Calls `client.is_password_focused()` (which checks `document.activeElement` is `INPUT` with `type="password"`, refusing cross-origin iframes and treating the response as hostile — only literal JSON `true` passes). **If not strictly `True`, aborts without sending a single password keystroke.**
     - Re-checks `client.current_url()` immediately before typing to narrow TOCTOU navigation races.
     - Decrypts the password transiently in Dom's process memory, sends `Ctrl+A`, `BackSpace`, and the password characters purely as RFB `KeyEvent` frames over `/websockify`, and if `submit` is `True`, sends `Enter` (`0xff0d`).
     - Closes the WebSocket immediately; drops local plaintext references.
  5. Records the audit entry in `desktop_permission_history` and returns **only**:
     `"Filled saved login #<id> for <site>."` (or a sanitized error string with zero secret bytes).

### 4.3 Live viewer auto password mode
The live VNC viewer (`/desktop/vnc/index.html`) already polls `/desktop/viewer/password-focus` during active handoffs and after touch/click/Tab/Enter events, switching the Type box to `type="password"` (`autocomplete="current-password"`) automatically when Chromium focuses an `input[type="password"]`, and sends typed characters only over VNC `/websockify` keystrokes (never via `/v1/browser/page/type`), with noVNC logging capped at `warn` so keysyms are never printed to the browser console.

---

## 5. Desktop permissions and approval UX (R4, E100)

### 5.1 Action kinds and defaults
Each of the five desktop action kinds has a global default policy of `ask`, `allow`, or `never`:

| Action ID | Display label | Default | Per-site rules? | Enforcement point |
|---|---|---|---|---|
| `visit_site` | Visit unfamiliar sites | `allow` | Yes | `DesktopWorker` before `browse(url)` and on post-action redirects; `SelfWork` refuses plans containing a site with `visit_site = never`. |
| `fill_login` | Fill saved logins | `ask` | Yes | `DesktopWorker` before `fill_saved_login` (E100). |
| `download` | Download files from desktop | `ask` | Yes (current browser host, or `desktop`) | `DesktopWorker` before `download_results` for non-screenshot files in `finish(files=...)`. |
| `upload` | Upload files | `ask` | Yes (current browser host) | `DesktopWorker` before `upload_file` to a web page; `never` also blocks `upload_inbox` staging into the sandbox. |
| `background_work` | Background work (Own work) | `allow` | Global (and respects `visit_site = never`) | `SelfWork.plan` / `SelfWork._run`. When `ask`, sends an approval card before launching a planned self-directed task; when `never`, refuses planning and execution. |

### 5.2 Policy resolution precedence
For `(action, site)`:
1. Normalize `site` (`lowercase`, strip leading `www.` and trailing `.`).
2. If `site` is non-empty and a per-site rule exists for `(site, action)`, that rule (`allow` | `ask` | `never`) wins.
3. Otherwise, the global policy for `action` applies.
4. On the dashboard, whenever a global policy is changed to `never` while per-site `allow` overrides exist for that action, the UI clearly displays the count of overriding site rules with a one-click **Clear site allows** control.

### 5.3 Approval cards (Telegram + Dashboard Waiting)
When an action resolves to `ask`:
1. Dom creates a `pending` row in `desktop_permission_history` with a single-use random token (stored as SHA-256 hash in DB; raw token held only in memory and callback payload) and updates the desktop task to `waiting_owner` (or pauses `SelfWork` before container start).
2. Dom sends an owner-DM card via `notice.send_notice`:
   - Title: e.g. `Permission needed: Fill saved login`
   - Details: site (`example.org`), brief action context (e.g. login label or filename, never any password), container name if non-default.
   - Buttons:
     - Row 1: `Allow once` (`dperm:once:<id>:<token>`) · `Allow always` (`dperm:always:<id>:<token>`)
     - Row 2: `Reject` (`dperm:reject:<id>:<token>`) · optional `Review on dashboard` URL button (`/app/waiting`) if `web.public_url` is set.
3. The same pending request appears on the dashboard's **Waiting for you** (`/app/waiting`) and **Access** (`/app/system/access`) pages with **Allow once**, **Allow always**, and **Reject** buttons.
4. **Outcomes**:
   - **Allow once**: resolves this single request (`status='allowed_once'`), leaves policy unchanged (`ask`).
   - **Allow always**: resolves this request (`status='allowed_always'`) and upserts `(site, action, 'allow')` into `desktop_site_permissions` (or sets global `background_work = 'allow'`).
   - **Reject**: refuses this action (`status='rejected'`), leaves policy unchanged (`ask`).
   - **Timeout / Cancel / Restart**: marks `status='expired'` and refuses the action.
5. As soon as decided from either Telegram or the dashboard, the Telegram card's inline buttons are replaced with a single `noop` outcome badge (`✅ Allowed once`, `✅ Allowed always`, `❌ Rejected`, or `Expired`).
6. `restart_refusal()` in `dom/web/api_system.py` refuses dashboard restarts while any desktop permission approval is waiting in memory.

### 5.4 Approvals history
Every check that asks, blocks, or auto-allows a governed action writes a row to `desktop_permission_history`:
- `id`, `task_id` (nullable), `self_task_id` (nullable), `action`, `site`, `detail` (sanitized, never secrets), `status` (`pending`, `allowed_once`, `allowed_always`, `auto_allowed`, `rejected`, `blocked`, `expired`), `decided_via` (`telegram`, `dashboard`, `policy`, `timeout`), `created_at`, `decided_at`.
- Visible newest-first on the dashboard's **Access → History** tab, filterable by action kind and decision status.

---

## 6. Secure secret requests (E101)

### 6.1 Owner-DM tool: `request_secret`
- Scopes: `{"owner_dm"}` only. Omitted from `friend_dm` and `group` tool lists; handler enforces `ctx.is_owner_dm()`.
- Parameters:
  - `label` (required string, 1–80 chars): human-readable name shown to the owner.
  - `reason` (required string, 1–240 chars): why Dom is asking.
  - `target` (optional enum `"secret" | "env" | "login"`, default `"secret"`):
    - `"secret"`: encrypts and stores in `SecretStore` under namespace `"owner_secret"`.
    - `"env"`: writes to `DOM_HOME/.env` via `set_dotenv`. Validated against `^[A-Z][A-Z0-9_]{1,63}$` and **refuses** reserved keys (`DOM_MASTER_KEY`, `DOM_MASTER_KEY_PREVIOUS`, `TELEGRAM_BOT_TOKEN`, `SANDBOX_API_KEY`, and `config.web.dev_token_env`).
    - `"login"`: stores as a saved desktop login (username + password) in `saved_logins` + `SecretStore("login", ...)`.
  - `key` (optional string, max 64 chars): target identifier (env var name for `env`, normalized site hostname for `login`, or slug for `secret`).
- Guardrails:
  - At most 5 pending secret requests at once (prevents model spam).
  - Each request has a 256-bit random one-time token (`secrets.token_urlsafe(32)`), stored in `secret_requests` **only as its SHA-256 hash**, expiring after **15 minutes**.
  - Tool result returns only `{"request_id": id, "status": "requested", "message": "Sent a secure entry card to the owner..."}` — never the token or URL.

### 6.2 Telegram card & dashboard one-time entry form
1. Dom sends an owner-DM card:
   - Title: `Secret requested: <label>`
   - Details: `<reason>`, explicit destination (`Encrypted credential store (<key>)`, `.env variable <KEY>`, or `Saved desktop login for <site>`), and `Expires in 15 min · Never paste secrets in chat.`
   - Buttons:
     - `Enter secret securely` URL button → `<web.public_url>/app/secret#t=<token>` (fragment `#t=` so the token is never sent in HTTP `GET` URLs or proxy access logs!).
     - `Cancel` callback button (`sreq:cancel:<id>`).
   - Pending requests also appear in `/app/waiting` (so owners without `public_url` can open them directly on their local dashboard via `id` without needing a link).
2. **Dashboard entry form (`/app/secret`)**:
   - Protected by full dashboard authentication (Cloudflare Access / local bearer token), strict CSP (`default-src 'self'`), `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`.
   - Reads `#t=<token>` (or `?t=<token>` fallback) on mount and immediately scrubs the URL with `history.replaceState(null, "", "/app/secret")`.
   - Calls same-origin JSON `POST /api/secrets/request/lookup` with `{"token": ...}` (or `{"request_id": ...}` when opened from Waiting) to fetch non-secret metadata (`label`, `reason`, `target`, `target_key`, `expires_at`, `overwrites_existing`).
   - Displays a minimal, high-clarity form:
     - `secret` / `env`: one masked password input (`type="password"`, `autocomplete="off"`, `spellcheck="false"`, show/hide toggle with accessible label & tooltip).
     - `login`: site (pre-filled/editable), optional label, username input, masked password input.
   - Submits via same-origin JSON `POST /api/secrets/request/submit`.
   - Server atomically verifies `status == 'pending'`, `expires_at > now`, and `token_hash` (constant-time), stores the value in its destination, marks `status = 'submitted'`, clears any Telegram card buttons to `✅ Secret set`, and records an owner event:
     `"Owner provided secret '<label>' (status: set)."` — **never** the value.
   - Dom learns only from that owner event on his next turn that `<label>` is set.

---

## 7. Dashboard UI layout

1. **System → Access (`/app/system/access`)** — new tab under `System`:
   - **Saved logins**: masked login list (`site`, `label`, masked username, `••••••••`, subdomain badge, last updated), Add / Edit / Delete modal/panel (password write-only), Owner secrets list (from E101, masked, deletable), and Encryption Key card (`kid`, count of encrypted entries, Rotate key button with confirmation).
   - **Permissions**: the 5 desktop action policies (`Ask` / `Allow` / `Never`), plus the per-site rules table (add, change policy, remove, clear site allows).
   - **History**: newest-first approvals history with status/action filters.
2. **Waiting for you (`/app/waiting`)**:
   - Surfaces pending desktop permission requests (**Allow once**, **Allow always**, **Reject**) and pending secret requests (**Enter secret**, **Cancel**) alongside existing proposals and chat approvals.
3. **One-time secret form (`/app/secret`)**:
   - Dedicated focused entry page opened from E101 Telegram cards or Waiting.

---

## 8. Verification requirements

Tests must cover every security rule in this spec:
1. **Zero-leak end-to-end test**: save a login with a unique canary password, run a desktop task that lists logins, requests approval, approves, fills the login via VNC, and finishes; assert the canary password never appears in ANY model request, tool argument, tool result, sandbox HTTP request (`/v1/*`), SQLite `turns` row, `usage` row, `desktop_permission_history`, `messages`, owner events, or Python log records.
2. **Crypto & rotation tests**: AES-256-GCM round-trip, AAD swap rejection, tamper detection, missing/malformed key fail-closed, key bootstrapping with `0600` perms, crash-safe key rotation (`rotate_key`), and recovery from interrupted rotation with `DOM_MASTER_KEY_PREVIOUS`.
3. **Origin & focus enforcement tests**: `fill_saved_login` refuses `http://`, refuses wrong host, refuses subdomain when `match_subdomains=False`, and aborts without sending any VNC keystrokes when `is_password_focused()` returns `False` or non-boolean hostile JSON.
4. **R4 permission tests**: all 5 action kinds (`visit_site`, `fill_login`, `download`, `upload`, `background_work`), `Ask` / `Allow` / `Never`, per-site overrides, Telegram Reject / Allow once / Allow always callbacks (plus non-owner / group / wrong-token rejection and card settling), timeout handling, and restart refusal while pending.
5. **E101 `request_secret` tests**: owner-DM-only scope enforcement (hidden and blocked in group/friend DM), reserved `.env` key rejection, pending spam cap, 15-min expiry, single-use consumption, hashed token storage, `secret`/`env`/`login` targets, and verification that the submitted value never reaches any model request, tool result, owner event, or log.
6. **Dashboard API & Playwright screen tests**: same-origin / auth guards on all new `/api/access/*` and `/api/secrets/*` routes, masked GET responses, zero CSP violations, zero axe accessibility violations, and icon button tooltips/aria-labels.
