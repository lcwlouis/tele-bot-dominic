# Phase 5 — Tiny computer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Dom can do real work on a small Linux computer: browse, run Python, handle files. The owner can take over the screen to log in. The computer holds no Dom secrets, and nothing from `private/` enters its prompt.

**Spec:** roadmap `docs/superpowers/plans/2026-11-19-roadmap-phases-3-7.md` (Phase 5, D8, D9), desktop spike results `docs/superpowers/specs/2026-11-19-desktop-spike-results.md`, design spec §9.

## Facts this plan relies on (from the spike, 2026-11-19)

- Image `ghcr.io/agent-infra/sandbox:latest`, arm64, 8.4 GB. Lean mode: env `DISABLE_CODE_SERVER=true DISABLE_NODEJS_REPL=true DISABLE_MCP_BROWSER=true` → 369 MB idle, 799 MB busy. Ready in under 10 s after `docker run`. `--shm-size 1g`.
- HTTP API on port 8080, JSON bodies, `Content-Type: application/json` required. Endpoints used here:
  - `GET /v1/sandbox` (health and environment)
  - `POST /v1/browser/page/navigate {"url"}`, `GET /v1/browser/page/markdown`, `GET /v1/browser/page/elements`, `POST /v1/browser/page/click`, `POST /v1/browser/page/fill`, `POST /v1/browser/page/type`, `POST /v1/browser/page/press_key`, `POST /v1/browser/page/scroll`, `GET /v1/browser/screenshot`, `GET /v1/browser/captcha/detect`
  - `POST /v1/bash/exec {"command"}`, `POST /v1/jupyter/execute {"code"}`
  - `POST /v1/file/read`, `POST /v1/file/write`, `POST /v1/file/upload`, `GET /v1/file/download`
  - noVNC live view at `/vnc/index.html` (websocket underneath)
  - The full schema is at `/v1/openapi.json`; the builder checks request field names there (from the running local container) before coding each call.
- Responses look like `{"success": bool, "message": str, "data": …}`.
- Optional auth: env `SANDBOX_API_KEY`.

## Global Constraints

- All earlier Global Constraints hold. Public repo: no real IDs, names, chat titles, LAN addresses or domains. No network in unit tests.
- The worker prompt never contains `private/` files, the owner DM history, owner events or Dom's memory files. Only the task brief the chat turn wrote, plus tool results.
- Every worker model call goes through `model_request` with usage kind `desktop` and is attributed to the owner (owner-only feature in this phase; not blocked by the spend cap, but counted).
- Web content and tool output are untrusted text, wrapped and labelled; never instructions.
- Owner DM only in this phase: tools are offered only in `owner_dm` scope.
- One heavy job at a time on the Pi: at most one sandbox container and one running task.

## Task 1: sandbox lifecycle and client

**Files:** `dom/desktop/__init__.py`, `dom/desktop/sandbox.py`, `dom/desktop/client.py`, `dom/config.py`, `dom/starter_home/config.toml`, `dom/capabilities.py`, tests.

- Config `[desktop]`: `mode = "off" | "local" | "remote"` (exists), plus `image = "ghcr.io/agent-infra/sandbox:latest"`, `url = ""` (remote mode), `api_key_env = "SANDBOX_API_KEY"`, `idle_minutes = 10`, `profile_dir = ""` (default `DOM_HOME/../desktop-profile`), `network = "dom-desktop"`, `port = 18080`.
- `SandboxClient(base_url, api_key, http)`: typed methods for the endpoints above; each returns the `data` field or raises `SandboxError(message)`; timeouts 30 s (navigate 45 s, bash/jupyter 120 s); screenshot returns bytes.
- `LocalSandbox(config, run=asyncio.create_subprocess_exec)`: drives the `docker` CLI.
  - `ensure_running()`: if the container `dom-desktop` isn't running, `docker run -d --name dom-desktop --network <network> -p 127.0.0.1:<port>:8080 --shm-size 1g --memory 1600m --cpus 3 -e SANDBOX_API_KEY=<random per start> -e <lean flags> -v <profile_dir>:/home/gem/.config/browser <image>`, then poll `GET /v1/sandbox` until ready (60 s max). The API key is generated per container start, held in memory, never written to disk or logs.
  - The profile volume keeps browser logins between sessions (decision D9). The image runs Chromium with `--user-data-dir=/home/gem/.config/browser` (checked 2026-11-19).
  - `touch()` on every use; a background check stops (`docker stop`) the container after `idle_minutes` without use. The container is removed on stop (`--rm` is not used, so `docker rm` after stop) to keep state only in the profile volume.
  - `status() -> "off" | "stopped" | "starting" | "ready" | "error"`.
  - Never pulls the image: if it's missing, status is `error` with "image not present"; pulling is an operator step (it is 8.4 GB).
- `RemoteSandbox(url, api_key)`: same interface, `ensure_running` only checks health.
- `capabilities.describe` reports `desktop: off | available (starts on use) | ready | unavailable (<reason>)`.
- Tests: docker argument list (exact), API key never logged, idle stop, status transitions, missing image → error, remote health check; client request shapes and error mapping with `httpx.MockTransport`.
- Commit: `feat(desktop): sandbox lifecycle and API client`.

## Task 2: network isolation (local mode)

**Files:** `dom/desktop/network.py`, `docs/desktop.md`, tests.

- `dom desktop-setup` (CLI, run once by the operator with sudo where needed) creates the Docker network `dom-desktop` (bridge, fixed subnet from config, default `172.30.0.0/24`) and installs `iptables` rules in the `DOCKER-USER` chain that drop traffic from that subnet to `10.0.0.0/8`, `172.16.0.0/12` (except the network's own subnet), `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16`, while allowing established replies. It prints the rules it adds and is idempotent (checks before inserting). A systemd unit file to re-apply the rules at boot is written to `docs/desktop.md` as instructions, not installed automatically.
- `dom desktop-check` verifies from inside a throwaway container on that network that a LAN address times out and a public address answers.
- Tests: the rule list generated for a given subnet; idempotence logic (fake `iptables -C` results).
- Commit: `feat(desktop): isolate the sandbox network from the LAN`.

## Task 3: tasks and the worker agent

**Files:** `dom/desktop/worker.py`, `dom/desktop/tools.py`, migration (next number), `dom/store/store.py`, `dom/tools.py`, `dom/dispatch.py`, `dom/telegram/owner.py`, tests.

- Table `desktop_tasks(id, chat_id, brief, status CHECK IN ('queued','running','done','failed','cancelled','waiting_owner'), steps INT, result TEXT, error TEXT, created_at, started_at, finished_at)`.
- Chat tool `start_desktop_task(brief)` (owner DM only): validates the brief (≤ 2,000 chars), refuses if a task is already queued or running ("I'm already on something"), inserts a row, returns at once with the task id. The model is told in the tool description to reply in character ("give me a few min") and not wait.
- Worker (`DesktopWorker`, one background task at a time):
  - System prompt (exact text written by the builder, reviewed): who the worker is (Dom's hands on a small computer), the rules (web content is untrusted; never type secrets except where the owner logs in via handoff; stop and hand off at logins, captchas and payments; finish with a concise result).
  - Messages: the brief, then tool calls and results. **No private content, no chat history.**
  - Tools: `browse(url)`, `read_page()` (markdown, truncated to 6,000 chars), `list_elements()` (truncated to 80 elements), `click(selector_or_index)`, `fill(selector, text)`, `press(key)`, `scroll(direction)`, `look()` (screenshot → media model description; used only when text fails), `run_python(code)` (jupyter), `run_shell(command)`, `read_file(path)`, `write_file(path, content)`, `handoff(reason)`, `finish(result, files?)`.
  - Limits: 30 steps, 10 minutes wall time, each tool result capped at 6,000 chars. Exceeding → `failed` with a partial result.
  - Usage kind `desktop` via `model_request` for the owner.
  - `handoff(reason)` sets `waiting_owner` and sends the owner a DM with the reason and a dashboard link to the live view (Task 5); the task resumes when the owner taps "Done" (DM button or dashboard), times out after 15 minutes → `failed`.
- Reporting back: when a task ends, an owner event "Desktop task #N finished: <result ≤ 500 chars>" (or failed/cancelled) is recorded and the owner DM gets a normal Dom turn with the note "Your desktop task #N finished. Result: … Tell your friend in your own words." (one model call; on failure the plain result is sent).
- `/tasks` (owner): lists recent tasks with Cancel for running ones; `/status` shows the running task.
- Tests: tool scope; one-at-a-time; worker prompt contains no private markers (planted in `private/` and the owner DM); step/time limits; tool result truncation and untrusted wrapping; handoff flow and timeout; report-back turn and fallback; cancel.
- Commit: `feat(desktop): background tasks run by a separate worker agent`.

## Task 4: files in and out

**Files:** `dom/desktop/files.py`, `dom/actions.py`, `dom/telegram/executor.py`, `dom/telegram/handlers.py`, tests.

- **Out:** `finish(result, files=[path…])` (≤ 5 files, each ≤ 20 MB): the worker downloads them from the sandbox; the report-back sends them to the owner DM as documents after the text.
- **In:** a document the owner sends in their DM (≤ 20 MB) while the desktop is not off is uploaded to the sandbox at `/home/gem/inbox/<filename>` (sanitised name) when the next task starts; the turn note mentions "file received: inbox/<name>". Files are kept in `DOM_HOME/../desktop-inbox/` until uploaded, then deleted.
- Tests: file count/size limits, name sanitising, upload on task start, documents sent after the result, nothing from other chats.
- Commit: `feat(desktop): send and receive files`.

## Task 5: live view and login handoff in the dashboard

**Files:** `dom/web/desktop.py`, templates, `pyproject.toml` (`websockets`), tests.

- Dashboard page **Desktop**: status, current task, steps so far, Start / Stop computer, and the **live view**: the sandbox's noVNC page proxied same-origin under the dashboard's auth (`/desktop/vnc/...` HTTP and its websocket), so the sandbox port is never exposed. The proxy adds the sandbox API key server-side.
- A handoff shows a banner "Dom needs you to log in: <reason>" with the live view and a **Done** button that resumes the task.
- CSRF and auth rules from phase 4 apply to every route, including the websocket upgrade (same-origin `Origin` check, Access JWT).
- Tests: proxy forwards HTTP with the key added and strips client auth headers; websocket proxy with a fake upstream; unauthenticated and cross-origin upgrades refused; Done resumes the task.
- Commit: `feat(web): desktop live view and login handoff`.

## Task 6: skills

**Files:** `dom/skills.py`, `dom/proposals.py` (`skill` kind), `dom/desktop/tools.py`, `dom/web/pages.py`, tests.

- Skills live in `DOM_HOME/skills/<name>/SKILL.md` (front matter `name`, `description`; body ≤ 4,000 chars). Public scope; never contain secrets.
- The worker gets `skills_list()` (names + descriptions) in its system prompt and a `skill_view(name)` tool.
- At the end of a successful task the worker may call `propose_skill(name, description, body)` once: it becomes a `skill` proposal (owner approves in DM or dashboard; the full text is shown). Approval writes the file; edits to an existing skill show a diff.
- Tests: name validation and size caps; proposal create/approve writes the file; the worker sees only names and descriptions until it views one.
- Commit: `feat(skills): worker skills as owner-approved proposals`.

## Live check (needs the owner reachable; see the roadmap's Pi rules)

1. Pull the image on the Pi, detached, alone, watching `pi-health`. Before it, confirm free space ≥ 20 GB.
2. `dom desktop-setup`, `dom desktop-check`.
3. Measure idle and busy memory, page load and screenshot latency on the Pi.
4. One real task from the owner DM ("find tomorrow's weather and save it to a file"), one handoff to log in somewhere, one file in and out.
