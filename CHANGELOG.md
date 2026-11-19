# Changelog

All notable changes to Dom are recorded here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions are git tags
named `vX.Y.Z`.

## [Unreleased]

### Added

- **version:** git archive writes the real version into dom/_version.py

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
- Ephemeral replies to the owner inside groups, and stickers chosen from sets the
  owner adds.
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
