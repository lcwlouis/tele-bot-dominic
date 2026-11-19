# Phase 6 — Plugins and core-change briefs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Dom can write small tools for himself (plugins). Each one runs only after the owner approves its exact code, inside a sandbox that can't read Dom's files or secrets. For changes to Dom's own code, Dom writes a brief for the owner to hand to a coding agent.

**Spec:** roadmap `docs/superpowers/plans/2026-11-19-roadmap-phases-3-7.md` (Phase 6, D10) and the design spec §8 (self-evolution tier 2 and 3).

## Facts

- The Pi has `bubblewrap` 0.12 (`/usr/bin/bwrap`) and unprivileged user namespaces work (checked 2026-11-19).
- Plugins are Python, run with the same interpreter as Dom (`sys.executable`), standard library only.

## Global Constraints

- All earlier Global Constraints hold. Public repo: no real IDs, names, chat titles, LAN addresses or domains. No network in unit tests.
- **A plugin never runs unsandboxed.** If `bwrap` is missing or fails its self-test, plugins are refused, on every platform.
- **Approval pins the code.** The SHA-256 of the plugin folder's files is stored on approval; any change re-stages the plugin (status back to `pending`) and it stops being offered until approved again.
- Plugins see only their own arguments. They never receive chat history, memory, `private/` content or secrets.
- Plugin output is untrusted text, wrapped like tool results.
- Owner-only approval (DM button or dashboard), recorded as an owner event.

## Task 1: manifest, staging and hashing

**Files:** `dom/plugins/__init__.py`, `dom/plugins/manifest.py`, `dom/plugins/registry.py`, migration (next number), `dom/store/store.py`, tests.

- A plugin is a folder `DOM_HOME/plugins/<name>/` with `plugin.toml` and `main.py` (other `.py` files allowed; total ≤ 64 KB; ≤ 10 files; no symlinks).
- `plugin.toml`: `name` (`[a-z][a-z0-9_]{1,31}`), `description` (≤ 200 chars, printable), `version`, `[[tools]]` entries each with `name`, `description`, `parameters` (JSON schema object, ≤ 4 KB), `scopes` (subset of `owner_dm`, `friend_dm`, `group`; default `["owner_dm"]`), `network = false` (bool), `timeout_seconds` (1–30, default 10), `memory_mb` (16–256, default 64).
- Table `plugins(name PK, status CHECK IN ('pending','approved','disabled'), hash TEXT, manifest_json, approved_hash, approved_at, updated_at)`.
- `PluginRegistry.scan()` reads every folder, validates the manifest (invalid → logged, skipped), computes the hash (sorted relative paths + contents), and upserts. A changed hash on an approved plugin sets `pending` and clears it from the offered tools.
- `approved_tools(scope)` returns tool schemas for approved plugins whose current hash equals `approved_hash`, with names prefixed `plugin_<plugin>_<tool>`.
- Tests: manifest validation (each rule); hash stability and change detection; symlink and size refusal; scope filtering; re-staging on change.
- Commit: `feat(plugins): manifest, staging and code pinning`.

## Task 2: the sandbox runner

**Files:** `dom/plugins/runner.py`, tests.

- `PluginRunner(bwrap="/usr/bin/bwrap")` with `self_test()` (runs a trivial plugin and checks it can't read `DOM_HOME`; cached per process) and `run(plugin_dir, tool, args, manifest) -> str`.
- Invocation: `bwrap --unshare-all [--share-net only if manifest.network and approved] --die-with-parent --new-session --clearenv --setenv PATH /usr/bin:/bin --ro-bind /usr /usr --ro-bind /lib /lib (+ /lib64, /bin, /etc/alternatives, /etc/ssl when network) --ro-bind <python prefix> <same> --ro-bind <plugin_dir> /plugin --tmpfs /tmp --proc /proc --dev /dev --chdir /plugin -- <python> -I -S /plugin/main.py`. No home, no DOM_HOME, no `/etc/passwd`-style host files beyond what Python needs (the builder determines the minimal set on Linux and documents it).
- Protocol: one JSON line on stdin `{"tool": …, "args": {…}}`; the plugin prints one JSON line `{"ok": true, "result": "<text>"}` or `{"ok": false, "error": "<text>"}`. Output capped at 16 KB; anything else → error.
- Limits: wall timeout from the manifest (kill the process group); memory via `prlimit --as` (or `resource.setrlimit` in a pre-exec shim) at `memory_mb`; CPU time limit = timeout; at most one plugin process at a time.
- A tiny stdlib helper `dom_plugin.py` is copied read-only into `/plugin/_dom/` so plugins can `from _dom import tool, run` without boilerplate.
- On non-Linux or when `self_test()` fails, `run` raises `PluginsUnavailable`; plugins are never offered.
- Tests: argument list construction (exact); protocol parsing; output cap; timeout handling with a fake process; unavailable path. One Linux-only integration test (skipped elsewhere and when `bwrap` is missing) runs a real plugin and asserts it cannot read a file planted in a fake DOM_HOME and cannot open a socket without `network`.
- Commit: `feat(plugins): run plugins under bubblewrap`.

## Task 3: tools, drafting and approval

**Files:** `dom/plugins/tools.py`, `dom/tools.py`, `dom/proposals.py` (`plugin` kind), `dom/telegram/owner.py`, `dom/web/pages.py` + template, tests.

- Approved plugin tools are offered in the scopes their manifest allows; calls go through the runner; results wrapped as untrusted; errors returned as text; usage not recorded (no model call).
- **Drafting:** owner-DM tool `draft_plugin(name, description, files: {path: content}, manifest_toml)` writes the folder under `DOM_HOME/plugins/<name>/` (validated as in Task 1) and creates a `plugin` proposal. Network-enabled plugins are flagged in the proposal ("This plugin can reach the internet").
- **Review:** the DM carries a summary (name, tools, scopes, network, file list with sizes, hash prefix) plus Approve / Reject and a dashboard link; the full code is reviewed on the dashboard (escaped, with a diff against the last approved version). Approving from Telegram is allowed only after the owner has opened the dashboard review once for that hash (recorded), so code is never approved unseen.
- Approve → `approved_hash = hash`, status `approved`. Reject → status `disabled`, folder kept.
- `/plugins` (owner): lists plugins with status and Disable buttons.
- Tests: scope filtering in turns; draft → proposal; Telegram approve refused until the dashboard review was opened for that hash; changed code re-stages; reject; disable; results wrapped.
- Commit: `feat(plugins): Dom drafts plugins, the owner approves the exact code`.

## Task 4: core-change briefs

**Files:** `dom/briefs.py`, `dom/tools.py`, `dom/web/pages.py` + template, tests.

- Owner-DM tool `write_brief(title, problem, proposal, acceptance)` (each field length-capped) writes `DOM_HOME/briefs/YYYY-MM-DD-<slug>.md` in a fixed template (title, problem, proposed change, acceptance criteria, "written by Dom on <date>") and DMs the owner the file as a document plus a one-line summary.
- The template never includes private notes; the tool description tells the model a brief is for a coding agent working on Dom's public code.
- Dashboard: a Briefs list with the rendered markdown (escaped) and a "copy" button.
- `self_inspect` (read-only, owner DM): version (git describe written at build time into `dom/_version.py`, or "unknown"), enabled tools per scope, approved plugins, desktop status, models, last 20 errors from the turn log. This completes the self-knowledge started in phase 3.
- Tests: brief file content and slug; DM with document; no private content; self_inspect output shape.
- Commit: `feat(briefs): core-change briefs and self_inspect`.

## Live check (owner present)

1. On the Pi: the runner's self-test passes; the integration test passes.
2. In the owner DM: ask Dom to write a small plugin (e.g. a unit converter); review it on the dashboard; approve; use it in a chat.
3. Ask Dom to write a brief for a change; the file arrives as a document.
