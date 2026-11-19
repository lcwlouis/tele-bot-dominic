# Phase 4 — Assistant features and dashboard v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** the owner has a proposals inbox (soul edits, imported memories), reminders that fire even when models fail, stickers, private replies inside groups, memory import from other assistants, and a JARVIS-style web dashboard behind Cloudflare Access.

**Spec:** roadmap `docs/superpowers/plans/2026-11-19-roadmap-phases-3-7.md` (Phase 4, decisions D6, D7, D14, "Memory import design") and the design spec §4–§12.

Split into two builds: **4A** (Telegram side: Tasks 1–5) and **4B** (dashboard: Tasks 6–9). 4B starts from the design direction in `docs/superpowers/specs/2026-11-19-dashboard-design.md` (written by the Opus design session, Task 0).

## Global Constraints

- All earlier Global Constraints hold. Public repo: no real IDs, handles, names, chat titles, LAN addresses or domains anywhere. No network in tests.
- Privacy invariant: content from `private/`, the owner DM or owner events never reaches a non-owner chat.
- Gate and spend cap before every paid call for a non-owner chat. The turn lock is never held across a model call in background work.
- New dependencies allowed in this phase: `jinja2`, `PyJWT[crypto]` (for RS256). Nothing else. No Node build step.
- One commit per task; the full suite passes before each commit.

## Task 0: dashboard design direction (Opus, not a builder)

A written design document plus one static HTML mock, committed under `docs/superpowers/specs/`. Tokens (colours, type scale, spacing, motion), layout grid, and the look of: chat list, proposal card, memory editor, usage panel, turn log. Readable on a phone. 4B implements it.

## 4A — Telegram side

### Task 1 (4.2): proposals

**Files:** `dom/proposals.py` (new), migration `0008_proposals_reminders.sql` (shared with Task 2), `dom/store/store.py`, `dom/telegram/owner.py`, `dom/telegram/handlers.py`, tests.

- Table `proposals(id INTEGER PK, kind TEXT CHECK(kind IN ('soul_edit','memory_fact','skill','plugin')), title TEXT, body_json TEXT, status TEXT CHECK(status IN ('pending','approved','rejected','expired')) DEFAULT 'pending', created_at, decided_at, source TEXT)`.
- `Proposals(app)`:
  - `create(kind, title, body: dict, source) -> int` stores the row and DMs the owner a summary with **Approve** / **Reject** buttons (`prop:yes:<id>`, `prop:no:<id>`). Silent notification. Recorded as an owner event.
  - `decide(id, approve: bool)` applies the change through a per-kind handler only when the row is still `pending` (conditional update), then marks it decided. Callbacks accepted only from the owner.
  - Per-kind handlers in this phase:
    - `soul_edit`: body `{"old": str, "new": str}` — exact unique replacement in `SOUL.md`, refused if `old` no longer matches (status stays pending, owner told "SOUL.md changed since; proposal can't apply").
    - `memory_fact`: body `{"file": "USER.md"|"NOTES.md"|"MEMORY.md", "text": str}` — appended through `Home.memory_write` (`add`); over the limit → status stays pending and the owner is told.
  - `skill` and `plugin` kinds exist in the schema only; creating them raises `NotImplementedError` until phases 5–6.
  - Pending proposals older than 14 days expire (daily job).
- Reflection (phase 3) gains: its public call may return `"soul_edit": {"old":…, "new":…, "why":…}` at most once a week; it becomes a proposal. Never applied directly.
- `/proposals` (owner command) lists pending proposals with their buttons.
- Tests: create → DM with buttons; approve applies once (second tap "already handled"); reject leaves files unchanged; non-owner tap ignored; soul edit with stale `old` refused; memory fact over limit stays pending; expiry; reflection-produced proposal at most weekly.
- Commit: `feat(proposals): one approval flow for soul edits and memory facts`.

### Task 2 (4.3): reminders

**Files:** `dom/reminders.py` (new), migration 0008, `dom/tools.py`, `dom/dispatch.py`, `dom/telegram/owner.py`, tests.

- Table `reminders(id PK, chat_id TEXT, text TEXT, due_at TEXT (UTC), importance TEXT CHECK(importance IN ('critical','normal','casual')), repeat TEXT CHECK(repeat IN ('','daily','weekdays','weekly')), status TEXT CHECK(status IN ('active','done','cancelled')), created_at, fired_at)`.
- Tool `remind(when, text, importance="normal", repeat="")`, **owner DM only**. `when` is an ISO-8601 local time in the configured timezone (no offset) or with an offset; past times are refused with an error result; at most 50 active reminders.
- Firing (background loop, every tick):
  - **critical:** at `due_at` regardless of presence. Dom first tries an in-character message (one model call, 20 s timeout, the reminder text in the turn note). If that fails, times out or returns no_response, the stored text is sent as `⏰ <text>`. Never skipped.
  - **normal:** fires within ±5 minutes when the owner DM is not asleep; while asleep, it waits for wake-up (then fires, marked late if > 30 min).
  - **casual:** never sends on its own; from `due_at` on, the next owner-DM turn note includes `reminder: <text>` once, then it is done.
  - Missed while the process was down: on start-up, past-due critical and normal reminders fire immediately with "(late)".
  - Repeats schedule the next occurrence in local time (DST-safe via zoneinfo); `weekdays` skips Saturday and Sunday.
- `/reminders` lists active reminders with a Cancel button each (`rem:cancel:<id>`), owner only.
- Tests: tool scope (owner DM only); parsing and past-time refusal; each importance's firing rule; critical fallback on model failure and timeout; late firing after downtime; repeats across DST and weekends; casual appears once in the turn note; cancel.
- Commit: `feat(reminders): critical, normal and casual reminders`.

### Task 3 (4.4b): stickers

**Files:** `dom/stickers.py` (new), `dom/actions.py`, `dom/telegram/executor.py`, `dom/prompt.py` or turn note, `dom/telegram/owner.py`, tests.

- Sticker sets are listed in `DOM_HOME/stickers.toml` (`sets = ["name1", "name2"]`). `/stickers add <set_name>` and `/stickers remove <set_name>` (owner DM) edit it; the dashboard edits it in 4B.
- On start-up and when the list changes, `bot.get_sticker_set(name)` loads each set; the map `emoji → [file_id]` is cached in memory (at most 10 sets, 120 stickers each).
- The turn note lists the available sticker emoji (unique, at most 40) when any set is loaded.
- `Actions` gains `sticker: str | None` (an emoji). The executor sends a random sticker for that emoji after the bubbles; an unknown emoji is ignored and logged.
- Tests: set loading (fake bot), emoji map caps, action validation, executor sends the right file_id, unknown emoji ignored, owner commands.
- Commit: `feat(stickers): Dom can send stickers from owner-chosen sets`.

### Task 4 (4.4): private replies inside groups

**Files:** `dom/telegram/executor.py`, `dom/dispatch.py`, `dom/actions.py`, tests.

- **Verify first** (by the human running phase 4's live check, not in tests): Bot API 10.2+ `sendMessage` with `ephemeral_message_parameters={"receiver_user_id": <owner>}` in a group the bot is in. The builder implements against the documented fields of the installed aiogram (check `EphemeralMessageParameters` in the installed package) and keeps it behind config `ephemeral_replies = false`.
- When enabled: in a **group**, if the triggering message is from the owner and the reply is assistant-like, the model may set `private: true` in the actions; the bubbles are then sent as ephemeral messages visible only to the owner. The model is told this option exists only in that situation.
- Private replies in groups still never contain `private/` content: the group prompt doesn't load it. They only keep the answer out of friends' sight.
- Tests: option offered only for owner-triggered group turns; executor passes the ephemeral parameters; disabled by default; never in DMs.
- Commit: `feat(telegram): ephemeral replies to the owner inside groups`.

### Task 5 (4.6): memory import

**Files:** `dom/memory_import.py` (new), `dom/starter_home/EXPORT_PROMPT.md`, `dom/telegram/handlers.py`, `dom/telegram/owner.py`, tests.

- `/import` in the owner DM replies with the export prompt (the file's text) and instructions: paste it into ChatGPT, Claude or Muse, then send the answer back with `/import <service>` as a reply, or as a `.txt`/`.md` document with caption `/import <service>`.
- `EXPORT_PROMPT.md` asks for everything the assistant remembers about the user, one fact per line, format `- [category] fact (when learned, if known)`, nothing left out, nothing invented.
- Parsing: lines matching `- [category] fact` (lenient: also `- fact`); at most 300 facts; each at most 300 chars; duplicates of existing note lines skipped.
- Each fact becomes a `memory_fact` proposal targeting `USER.md`, source `import:<service>`. To avoid 300 DMs, an import creates proposals **without** individual DMs and sends one summary DM: "Imported N facts from <service>. Review them in the dashboard or with /proposals." `/proposals` pages 10 at a time with "Approve all on this page".
- Siri: `/import siri` replies that Siri has no exportable memory.
- Documents over 1 MB are refused. The text is never sent to any chat other than the owner DM; the only model call is none (parsing is code).
- Tests: parsing variants; caps; duplicate skipping; proposals created without per-fact DMs; summary DM; non-owner `/import` ignored; Siri reply; oversized document refused.
- Commit: `feat(import): import memories from other assistants as proposals`.

## 4B — Dashboard

### Task 6: web server and auth

**Files:** `dom/web/app.py`, `dom/web/auth.py` (new package), `dom/cli.py`, `dom/config.py`, `dom/starter_home/config.toml`, `pyproject.toml`, tests.

- Config `[web]`: `enabled = false`, `bind = "127.0.0.1"`, `port = 8080`, `access_team = ""` (Cloudflare Access team name), `access_aud = ""` (application audience tag), `dev_token_env = ""` (optional: name of an env var holding a token for local use without Access).
- Runs inside the Dom process (FastAPI + uvicorn `Server` on the same asyncio loop), started by `dom run` when enabled.
- **Auth on every request:** the `Cf-Access-Jwt-Assertion` header is verified with RS256 against `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (keys cached 1 h, refreshed on unknown `kid`), checking `aud`, `exp`, `iss`. Failure → 403. When `dev_token_env` is set and the request carries `Authorization: Bearer <that token>`, it is accepted instead (for local testing). No auth configured → the server refuses to start.
- CSRF: state-changing requests are POSTs with htmx and must carry `HX-Request: true` plus a same-origin `Origin`/`Referer`.
- Security headers: CSP (self only, no inline script except htmx's), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- Tests: valid JWT (keys from a test key pair) accepted; wrong aud, expired, wrong signature, missing header → 403; dev token; refuses to start without auth; CSRF rule; headers present.
- Commit: `feat(web): dashboard server with Cloudflare Access auth`.

### Task 7: pages — chats, proposals, usage, turn log

**Files:** `dom/web/pages.py`, `dom/web/templates/*.html`, `dom/web/static/*` (htmx vendored, CSS from the design doc), migration `0009_turn_log.sql`, `dom/agent.py`, tests.

- Turn log table `turns(id, at, chat_id, scope, calls INT, trace TEXT, model TEXT, fallback INT, duration_ms INT, outcome TEXT)` written by `agent.run_turn` (from the existing trace) and the dispatcher. Kept 30 days (daily job).
- Pages (server-rendered Jinja2 + htmx, implementing the design doc):
  - **Chats:** the `/status` table plus per-chat preset, traits, wake/sleep/leave actions (same code paths as the Telegram buttons).
  - **Proposals:** pending cards with approve/reject, bulk approve for imports, filters by kind and source.
  - **Usage:** today and last 30 days by kind and model, spend vs cap, fallback events.
  - **Turn log:** last 200 turns, filter by chat.
- The owner's private content is shown (the dashboard is the owner's), but friend DMs' notes and the owner DM are clearly labelled.
- Tests: each page renders with fake data; actions call the same functions as the Telegram handlers; turn rows written; retention.
- Commit: `feat(web): chats, proposals, usage and turn log pages`.

### Task 8: memory and soul editor

**Files:** `dom/web/pages.py`, templates, tests.

- View and edit every memory file with its character limit shown; saving goes through `Home.memory_write`-equivalent validation (limits, path rules). `HABITS.md` is read-only here (code-owned).
- `SOUL.md` editing has two paths: direct edit (owner's own text), or **"Talk to Dom about his soul"**: a chat panel where each owner message gets one model call with the soul and the request; the only output is a `soul_edit` proposal (with a diff view) — never a direct write.
- Tests: limits enforced; private files only under the private section; HABITS read-only; soul chat creates a proposal and never writes SOUL.md.
- Commit: `feat(web): memory and soul editor`.

### Task 9: serving through the existing tunnel

**Files:** `docs/deploy-dashboard.md`, `dom/web/app.py` (bind), tests for bind config only.

- The Cloudflare tunnel's ingress points at `http://<forwarder>:8080` on the Docker network `<tunnel-network>` (gateway <bridge-gateway>), and `cloudflared` already enforces Access. No Cloudflare change is needed:
  - Dom's web server binds to the Docker bridge gateway address (config `bind`), reachable from that network but not from the LAN.
  - A tiny forwarding container named `<forwarder>` (alpine + socat, `--restart unless-stopped`, memory limit 16 MB) joins `<tunnel-network>` and forwards :8080 to the gateway address.
- The doc describes this with placeholders (no real hostnames), plus the plain alternative of pointing a tunnel at `127.0.0.1`.
- Commit: `docs: serving the dashboard through a Cloudflare tunnel`.
