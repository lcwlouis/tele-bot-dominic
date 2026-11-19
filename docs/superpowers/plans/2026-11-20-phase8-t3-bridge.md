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
- Reading is free; **sending a message to a thread always needs the owner's tap** on a confirmation button showing the target thread, machine and exact text.
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

## Task 2: client

**Files:** `dom/t3/client.py`, tests (with recorded JSON fixtures — synthetic IDs).

- `T3Client(cli: list[str], base_dir)` runs `t3 … --json` subprocesses (timeout 20 s, output cap 512 KB) and parses: `environments()`, `projects(environment_id=None)`, `threads(project_id, environment_id=None, limit=20)`, `thread(thread_id, environment_id=None, messages=10)`, `send(thread_id, text, environment_id=None)`.
- The builder confirms each command's real flags and JSON shape by running it read-only against the owner's running T3 server on the development Mac (`~/.t3/bin/t3`), records sanitised fixtures (replace every ID, label, URL, path and message with placeholders), and never runs `send`, `start`, `link`, `unlink`, `add`, `remove` or `rename` there.
- Errors (server down, not linked, expired link) become a typed `T3Unavailable(reason)`.
- Tests against fixtures; subprocess runner injected.
- Commit: `feat(t3): a CLI client for projects, threads and messages`.

## Task 3: Dom's tools and the confirmation flow

**Files:** `dom/t3/tools.py`, `dom/tools.py`, `dom/telegram/handlers.py` + `owner.py` (callbacks `t3send:<token>`), `dom/capabilities.py`, tests.

- Owner-DM tools:
  - `coding_overview()` — machines (label, reachable), projects per machine, and the 10 most recent threads with status (running/waiting/idle/failed) and title. ≤ 3,000 chars.
  - `coding_thread(thread, machine?)` — a thread's status and its last 5 messages, each ≤ 800 chars, wrapped as untrusted.
  - `coding_send(thread, text, machine?)` — does **not** send. It stages the message (in memory, 10-minute expiry, random token) and DMs the owner: "Send to <thread title> on <machine>?" with the exact text and **Send** / **Cancel** buttons. Only the owner's tap sends it; the result (or error) is reported back in the DM and recorded as an owner event.
- `capabilities.describe` says whether the bridge is on and how many machines are reachable.
- Machines and threads can be referred to by label/title or ID; ambiguity returns the candidates instead of guessing.
- Tests: tools offered only in owner DM; overview truncation; untrusted wrapping; send staged not executed; Send tap (owner only, once, unexpired) executes exactly once; Cancel; expired token; non-owner tap; router-level routing test includes `t3send`.
- Commit: `feat(t3): Dom can read coding threads and pass on messages you confirm`.

## Live check (owner present)

1. `dom t3-service --yes` on the Pi; check memory.
2. Owner links the Pi to the Mac and the Framework (approve on each).
3. In the DM: "what's running on my machines?", "what's the latest in <thread>?", and one confirmed send to a harmless thread.
