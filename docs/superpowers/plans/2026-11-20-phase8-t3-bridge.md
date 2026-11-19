# Phase 8 — T3 Code bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** from the owner's DM, Dom can see the owner's coding work across machines (projects, threads, what each agent is doing) through T3 Code, and pass a message to a thread when the owner confirms it.

## Facts (checked 2026-11-20)

- `npx t3@latest` runs on the Pi (Debian 13 aarch64) with Node 24 installed user-locally in `~/.local/node`. Version `t3 v0.1.10`, the same as the owner's Mac.
- `t3 serve --no-browser --base-dir <dir> --port <p>` starts a headless server bound to `127.0.0.1` (HTTP 200), runs its own migrations, and uses about 400 MB. It configures an agent slice allowing up to ~3 GB for agent sessions, so on the Pi it must not run coding agents and needs a memory cap.
- `npx` leaves the server process running when its parent is killed. A service must run the server binary directly (or via systemd with `KillMode=control-group`).
- The CLI has `t3 environment link|list|unlink`, `t3 project list|add|remove|rename`, `t3 thread list|start|send|show`. `t3 thread show <thread> --json --messages N` prints a thread's state and latest messages. `t3 environment list --json` returns `[{environmentId, label, urls, access, linkedAt, expiresAt, lastReachedAt, lastError, status}]`. Linked environments are reached through T3's relay, not the LAN, so changing IPs don't matter.
- Linking a machine requires the owner to approve it in that machine's T3 settings (Sharing & access).

## Global Constraints

- All earlier Global Constraints hold. Public repo: no real IDs, names, environment IDs, relay URLs, LAN addresses or domains. No network in unit tests.
- Owner DM only. Nothing about the owner's projects ever reaches another chat.
- Reading needs no confirmation but is always audited and reported to the owner. **Sending a message to a thread always needs the owner's tap** on a confirmation button showing the target thread, machine and exact text.
- Thread content is untrusted text (it includes agent output and web content): wrapped as untrusted, size-capped, never followed as instructions.
- The Pi's T3 server never runs coding agents.

## Task 1: T3 server as a user service

**Files:** `dom/t3/__init__.py`, `dom/t3/service.py`, `dom/cli.py` (`dom t3-service [--yes]`), `docs/t3-bridge.md`, tests.

- `dom t3-service <home> [--yes]` writes `t3.service` (systemd user unit): `ExecStart=<node> <resolved t3 server entry> serve --no-browser --base-dir %h/.t3-dom --port <port> --host 127.0.0.1`, `MemoryHigh=450M`, `MemoryMax=600M`, `Restart=on-failure`, `KillMode=control-group`. The entry is resolved once from the npm cache (`npm exec --package=t3@<pinned> -- which t3` or installing `t3@<pinned>` into `~/.local/t3`), pinned to a version in config `[t3] version`.
- Config `[t3]`: `enabled = false`, `port = 39731`, `version = "0.1.10"`, `node = "~/.local/node/bin/node"`.
- Dry run prints the unit and commands; `--yes` writes, reloads, enables.
- `docs/t3-bridge.md`: install Node user-locally, run `dom t3-service`, link machines (`t3 environment link` here, approve there), what Dom can and can't do, how to unlink.
- Tests: unit rendering, dry run writes nothing, pinned version in ExecStart.
- Commit: `feat(t3): run a T3 Code server as a user service`.

## Findings from the Task 1 build and the sign-in spike (2026-11-20)

- The `t3` CLI only talks to the local server. `t3 project list` is rejected and `t3 thread …` cannot target a linked machine. `t3 environment list --json` works and is the only CLI call Dom keeps.
- Everything else goes through the server's MCP endpoint, `http://127.0.0.1:<port>/mcp`. That is plain JSON-RPC over HTTP POST: `initialize` returns an `mcp-session-id` header, then `notifications/initialized` and `tools/call` are sent. Responses are `application/json`; handle `text/event-stream` `data:` lines too. Results carry `structuredContent`. **No MCP SDK is needed; use httpx.**
- Auth is OAuth 2.1, verified end to end on the Pi:
  1. `POST /oauth/mcp/register` with `{client_name, redirect_uris:[<loopback url>], grant_types:["authorization_code"], response_types:["code"], token_endpoint_auth_method:"none"}` returns `client_id`.
  2. `t3 auth pairing create --base-dir <dir> --scope orchestration:read --scope orchestration:operate --ttl 5m --label Dom --json` returns `{credential, expiresAt, id, label, scopes}`.
  3. `POST /oauth/mcp/decision` with `{authorization:{response_type:"code", client_id, redirect_uri, code_challenge, code_challenge_method:"S256", state, resource:"<base>/mcp"}, decision:{_tag:"pairing-code", access:"approval-required", code:<credential>}}` returns `{redirectTo}`, which carries `code`. `approval-required` is T3's **Supervised** level, as the owner chose.
  4. `POST /oauth/mcp/token` (form-encoded: `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `code_verifier`, `resource`) returns `{access_token, token_type:"Bearer", expires_in:2592000, scope}`. There is no refresh token, so a sign-in lasts 30 days.
- Through MCP, tools take an optional `environmentId`. `t3_environment_links` is not offered to outside agents, so machine discovery stays on the CLI.

## Owner decisions (2026-11-20)

- Access level: **Supervised**.
- "Whatever he does on it he notifies me and there's traceability." So every T3 call Dom makes is recorded and the owner is told about it. See Task 3.

## Task 2: sign-in and client

**Files:** `dom/t3/auth.py`, `dom/t3/client.py`, `dom/cli.py` (`dom t3-connect <home> [--yes]`), `dom/config.py`, `docs/t3-bridge.md`, tests (synthetic IDs and JSON fixtures).

- `dom t3-connect` runs the four steps above against the local server. PKCE is S256 with a random verifier; the redirect URI is a loopback URL that is never listened on, because the code comes back in `redirectTo`. The access token, its expiry and the client id go into `<home>/t3-token.json` with mode 0600, written atomically. The credential and token are never printed or logged. Without `--yes` it prints what it will do. It is also callable as a function so the owner's Reconnect button (Task 3) can use it.
- `T3Client(base_url, token_path, cli)`:
  - `environments()`: CLI `t3 environment list --json`. Returns label, id and status.
  - `call(tool, args)`: MCP over httpx. It initializes once and caches the session id; if the session is gone, it re-initializes once.
  - A **hard allowlist**: `t3_project_list`, `t3_thread_list`, `t3_thread_search` and `t3_thread_read` for reads, and `t3_thread_send` only from the confirmed-send path. Any other tool name raises before any network call.
  - Sends are always `mode:"queue"`, never steer, restart or `freshContext`. They carry `clientRequestId = "dom-<audit id>"`.
  - Timeouts are 5 s to connect and 20 s total; responses are capped at 512 KB. Errors (server down, 401, expired token, tool error, unreachable machine) become `T3Unavailable(reason)`, and a 401 or expiry says to reconnect.
- Config `[t3]` gains `base_url` (default `http://127.0.0.1:39731`). The token path is fixed under the home folder.
- Tests: the sign-in flow against a fake transport (both decision and token payloads asserted, secrets not logged); session reuse and re-init; the SSE and JSON response shapes; allowlist rejection; error mapping; the 0600 token file.
- Commit: `feat(t3): sign in to T3 Code and call its MCP tools`.

## Task 3: Dom's tools, confirmation, notifications and the audit trail

**Files:** `dom/t3/tools.py`, `dom/tools.py`, `dom/store/migrations/0012_t3_audit.sql` (next free number), `dom/store/store.py`, `dom/telegram/owner.py` + `handlers.py` (command `/t3`, callbacks `t3send:<token>`, `t3cancel:<token>`, `t3reconnect`), `dom/capabilities.py`, tests.

- Tools are offered only in the owner DM, and only when `[t3] enabled` and a token exists:
  - `coding_overview()`: machines (label, reachable), projects per reachable machine, and the 10 most recent threads with status and title, at most 3,000 characters.
  - `coding_thread(thread, machine?)`: a thread's status and its last 5 messages, each at most 800 characters, wrapped as untrusted.
  - `coding_search(query, machine?)`.
  - `coding_send(thread, text, machine?)`: does **not** send. It stages the message (in memory, 10-minute expiry, random token) and DMs the owner "Send to <thread title> on <machine>?" with the exact text and **Send** / **Cancel** buttons. Only the owner's tap sends, once. The result or error is reported in the DM and recorded as an owner event.
- Machines and threads can be named by label or title, or by id. If a name is ambiguous, Dom gets the candidates back instead of a guess.
- **Audit trail.** A `t3_audit` table records `(id, at, chat_id, tool, machine, target, summary, outcome, error)` for every T3 call, including reads, failures, staged sends, sends and cancels. The summary is short and never holds tokens; for a send it holds the exact text. Rows are kept for 180 days.
- **Notifications.** After any turn that touched T3, the owner gets one DM note listing what Dom looked at, for example "🔎 Checked your coding work: overview; thread "<title>" on <machine>". A send has its own confirmation and result messages. Notes go out even if the turn's reply fails.
- **`/t3`** (owner only): whether the bridge is on, the sign-in's expiry date, reachable machines, and the last 15 audit rows as a table; plus a **Reconnect** button that runs the Task 2 sign-in. Three days before expiry, Dom DMs the owner once with that button.
- `capabilities.describe` says whether the bridge is on and how many machines are reachable.
- Tests:
  - tools are offered only in the owner DM;
  - overview truncation and untrusted wrapping;
  - every tool call writes an audit row, including failures;
  - one note per turn;
  - a send is staged, not executed;
  - a Send tap (by the owner, once, before expiry) executes exactly once with `mode:"queue"` and is audited;
  - Cancel, an expired token and a non-owner tap;
  - `/t3` output;
  - the expiry reminder is sent once;
  - the router-level routing test includes the new prefixes.
- Commit: `feat(t3): Dom can read coding threads and pass on messages you confirm`.

## Live check (owner present)

1. `dom t3-service --yes` on the Pi; check memory.
2. `dom t3-connect --yes` on the Pi.
3. The owner links the Pi to the Mac and the Framework (`t3 environment link` on the Pi, approved on each machine), and checks that "Dom" shows under Sharing & access with Supervised access.
4. In the DM: "what's running on my machines?", "what's the latest in <thread>?", and one confirmed send to a harmless thread. Check the notes, `/t3` and the audit rows.
