# Changelog

All notable changes to Dom are recorded here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions are git tags
named `vX.Y.Z`.

## [Unreleased]

- Dashboard: edit each chat's sliders and reset them to default.
- Dashboard: upload a `.txt` or `.md` export through the same import as `/import`; adds `python-multipart`.
- Dashboard: approve or ignore pending groups and stranger DMs.
- `gif` action: a KLIPY search result sent as an animation; set `KLIPY_API_KEY` ([docs/gifs.md](docs/gifs.md)).
- Skills work in chat: `skills_list`, `skill_view` and `skill_manage` (a proposal the owner approves) in every chat, with each skill's name and description in the prompt.
- Plugins can add Telegram slash commands and dashboard panels; declared surfaces are reviewed and re-approved like code (`docs/plugins.md`).
- `/update` (owner DM): shows the newest `vX.Y.Z` release tag of `[update] repo_url` with its changelog section and, on confirmation, deploys it in a detached `dom-update` unit. Dom reports the outcome after the restart.
- `dom deploy <home> vX.Y.Z` stages a tag, runs `uv sync --frozen --no-dev`, backs up, repoints `app` and restarts `dom.service`; if the new release is not ready within 120 seconds it rolls back to the previous one. After a successful deploy only the current and previous release folders are kept.
- `find_devices` (owner DM, read-only): LAN devices with names and addresses from mDNS and the neighbour table. A configured LAN host that moved becomes a config-change proposal (`lan_address`) for the owner to approve.
- `[desktop] lan_probes` accepts `<name>.local:port`; the names are resolved on the Docker host before every firewall probe and an unresolvable name fails closed.
- Run more than one desktop container. `start_desktop_task` takes an optional container name (letters, digits, hyphens); each container has its own task slot, browser profile, port and idle timer, and the default container behaves as before. Desktop hosts are a list (`[[desktop.hosts]]`, each with `max_containers` and `min_free_memory_mb`); the single `[desktop] mode` config still works as one host named `default`.
- Refuse, with the numbers, to start a container past a host's limit or that would leave it below its free-memory floor (local: `MemAvailable`; remote: read through the sandbox's shell). The Pi allows one container and keeps 512 MB free besides the container's 1600 MB.
- Handoff links, task reports, screenshot file names, the live viewer and the dashboard Desktop page name the container; the page lists every container with its host, state and task. `self_inspect` reports `desktop_hosts` with limits and containers.
- Migration `0013` adds `desktop_tasks.container` and makes the one-active-task limit per container.
- Add a pinned, loopback-only T3 Code systemd user service with a 600 MiB memory limit.
- Document Raspberry Pi desktop startup timings, the Chromium seccomp profile and the desktop connectivity check.
- Wait for the local sandbox API and browser within a configurable 180-second startup deadline, with a distinct error for each stage.
- Disable Jupyter for faster local desktop startup and run Python scripts through the sandbox Bash API.
- Ship a Docker seccomp profile that lets Chromium start its namespace sandbox on the Raspberry Pi.
- `/preset` and the dashboard Chats panel can set one preset for every chat ("Everywhere"); a chat's own preset still wins, and a chat can go back to "Same as everywhere". `self_inspect` reports it.

## [0.7.0] - 2026-11-19

The rebuild: Dom is now a single asyncio service with a privacy-scoped prompt, a
presence model, an owner dashboard and an approved-code plugin system. It runs on
a Raspberry Pi as a systemd user service.

### Phase 1 — Core chat

- Dom answers as a Telegram bot account. Each prompt draws only on what its chat's
  scope allows: owner DM, approved friend DM or approved group.
- Unknown chats stay pending until the owner approves them. Setup works for anyone
  through `dom init` and the `/claim` code.
- Notes and memory live in a plain-file home (`SOUL.md`, `memory/`, `private/`). The
  `private/` folder is never readable by a non-owner prompt.
- Replies use human-like timing and several short bubbles, with optional reactions
  and unsend.

### Phase 2 — Presence and triage

- Each chat is asleep, offline or online. Dom wakes for floods of messages, for
  urgent moments, or on the owner's `/wake`, and catches up on what he missed.
- While online, the Jev decisions model chooses reply, react or ignore. Bot-to-bot
  chats stop deterministically after a bounded exchange.
- Photos, GIFs, videos, voice notes and stickers are described by a media model.
- Owner controls in Telegram: `/status`, `/chats`, `/pending`, `/wake`, `/sleep`.
- Gap fixes: a friend's DM can no longer leak into shared memory, token usage and
  a daily spend cap are tracked, `dom backup` and `dom restore` exist, and the
  status page shows which model answered last.

### Phase 3 — Memory and feeling alive

- A background memory pass writes notes; replies no longer edit memory themselves.
- Per-chat personality sliders and preset files, with one canonical set of trait
  names.
- A daily reflection writes a public-safe journal and a separate private one, sets
  a mood and nudges sliders by small amounts.
- Sleep and wake times drift toward the chat's quiet hours, learned from message
  counts alone, so no chat text can keep Dom awake.
- Dom can text the owner first, a limited number of times a day.
- A `web_search` tool, and a short capabilities block in the prompt generated from
  what Dom can actually do in that chat.
- A second guard checks outgoing messages for private leaks; it passed a 40-case
  evaluation with a clean gap between leaking and clean messages.

### Phase 4 — Assistant features and dashboard

- One approval flow (proposals) for soul edits, imported memories and skills, in
  Telegram and on the dashboard.
- Reminders with repeats; critical reminders always send their stored text, even
  when a model fails.
- Stickers chosen from sets the owner adds, and ephemeral replies to the owner
  inside groups (behind a setting, off by default).
- Memory import from other assistants, each fact reviewed before it is kept.
- A web dashboard for chats, proposals, memory and soul, usage and the turn log,
  protected by Cloudflare Access.

### Phase 5 — Tiny computer

- Dom can start background desktop tasks on a sandboxed Linux desktop: browse, run
  Python and handle files. A separate worker agent runs each task, with nothing
  from `private/` in its prompt.
- The sandbox starts only after a probe proves the local network is unreachable.
  Tasks have budgets, can be cancelled, and are listed in `/status` and the
  dashboard.
- The owner can take over the screen through the dashboard to log in. The browser
  profile persists between sessions.
- Worker skills are procedures the owner approves as proposals.

### Phase 6 — Plugins and briefs

- Dom can draft small plugin tools. A plugin runs only after the owner approves its
  exact code, inside a Bubblewrap sandbox with a syscall filter, no access to
  Dom's files or secrets, and network only if approved.
- `write_brief` turns a proposed change to Dom's own code into a markdown brief the
  owner can hand to a coding agent.
- `self_inspect` reports version, tools by scope, plugins, desktop status, models
  and recent errors to the owner.

### Phase 7 — Polish and release

- `dom doctor` checks tokens, models, timezone, backups and misplaced secrets.
- `dom install-service` writes the systemd units, with a preview before `--yes`.
- A setup guide, configuration reference and privacy notes written for someone
  setting Dom up from scratch.
- CI runs the tests, the plugin sandbox tests and a personal-data guard that blocks
  IDs, handles and private addresses.
- The dashboard gets its visual pass.

### Release tooling

- Real version numbers in every deploy: `git archive` stamps the tag into
  `dom/_version.py`, and releases are tagged `vX.Y.Z` with this changelog.
- `own_code` lets Dom read his own source, docs and changelog (owner DM only,
  read-only), and `self_inspect` reports the version and latest release.
- Dom describes his personality and abilities in his own words, with exact
  settings only when asked.
- Fixed: buttons from `/preset` and `/plugins` did nothing, because the router's
  prefix list left them out.
