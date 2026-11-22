# Dom core — design

Status: phases 1–8 live on the Pi; v0.8.0 released; v0.8.1 workstreams merged and in drift review, not yet tagged (`docs/superpowers/plans/2026-11-22-v0.8.1.md`; the Svelte dashboard, track F, is not merged) · first written 2026-11-19, updated 2026-11-22 · replaces the uncommitted V2 attempt on `feat/v2-modernisation`

**Phases 3–7 are revised in `docs/superpowers/plans/2026-11-19-roadmap-phases-3-7.md`.** Where the two disagree, the roadmap wins.

## 1. What Dom is

Dom is a Telegram **bot account** that acts like a friend in groups and DMs, and as a personal assistant to its owner in the owner's DM. It runs on a Raspberry Pi 4B with 4GB. Everything except the AI models runs on the Pi.

The code is a **general base** that anyone can set up. Personal details live only in the instance's home folder, never in the code. The author's own Dom is one instance with a soul written around him.

Out of scope for the core: userbot features, MCP connectors (Google Workspace, iCloud), Office file generation. They come later as skills or plugins, if they come at all.

## 2. Principles (lessons from the drift)

- Check every premise against the real API before building on it. No mocks stand in for a decision.
- Build no placeholder features. If something isn't wired end to end, it doesn't ship.
- Enforce rules in code, not in prompts. Privacy, presence, permissions and approval live in code. Prompts describe character.
- The prompt prefix stays stable so prompt caching works. Memory is snapshotted when a session starts, the way Hermes does it.
- Use current models only. Model IDs live in config, not code.

## 3. Runtime shape

A single Python 3.12 asyncio process, plus an optional "tiny computer" container.

```
Telegram ⇄ aiogram adapter ─► store (SQLite WAL + FTS5, JSONL transcripts)
                 │
                 ▼
           presence (per chat: asleep / offline / online)
                 │  wake? reply?
                 ▼
           triage (Jev, Decisions API) ──► agent loop (main model + tools)
                                              │
                    outbound guard ◄──────────┘
                         │
                 Telegram actions: bubbles, typing, react, reply_to,
                 send-then-delete, ephemeral

side services: scheduler (reminders, heartbeat/daydream), web UI (FastAPI),
               desktop client (tiny computer: on Pi / remote / off)
```

**Libraries:**
- `aiogram` 3 for Telegram, replacing Telethon. A bot account gets nothing extra from MTProto.
- `httpx` for every model call.
- `fastapi` + `uvicorn` for the UI.
- No agent framework. The loop is small and ours.

**Models**, all through OpenAI-compatible endpoints. Since v0.8.1 each role has a primary provider (a local endpoint or a cloud one) and an ordered chain of fallbacks (`[[models.<role>.fallbacks]]`), in `dom/llm.py` (`routed_client`):
- `chat`: the main model. Its chain is its own providers, then `[models.fallback]`'s. An existing `[models.chat_fallback]` is read as `fallback`. The author's instance uses the homelab model `default`, falling back to `deepseek/deepseek-v4.1-flash-1017` on OpenRouter.
- `media`: images, GIFs, video, voice, and each sticker's one-line description. It must work under the instance's OpenRouter zero-data-retention setting; the starter default is `perceptron/perceptron-mk1.5`.
- `drafting`: skill and plugin drafts. The starter uses Claude Haiku 5.5 on OpenRouter with low thinking. Its chain is drafting, then chat, then fallback.
- `search`: optional OpenRouter web search, a single provider.
- `triage`: TypeSafe Jev via `POST /api/alpha/decisions` with `~typesafe/jev-latest`. This is a classifier, not a chat model, so it has no chain and the settings page shows it read-only.
- A chat model may lack vision (the homelab one does). Anything visual goes through the media model.
- A provider that fails (connection error, 5xx or 429 after retries, or 401, 402, 403 or 404) hands over to the next one. The serving provider is recorded in the turn log and usage, and the spend cap is checked before every attempt.
- Requests to an OpenRouter host (chat, media, drafting, search) send `provider: {zdr: true}`, so a model without a zero-data-retention endpoint fails instead of leaking. Other cloud providers are the owner's responsibility.
- The owner sets providers on the dashboard's Settings page (`/api/models`, owner-only, same-origin JSON). A save is validated, revision-checked and probed: each added or changed slot gets one short real call (8 s timeout, no redirects), and any failure refuses the save with a per-slot error. A new key variable that is not in `.env` yet saves untested. `config.toml` is written atomically with a `.bak`, and routes apply without a restart. API keys stay in `.env`; the page shows only whether one is set.

When Jev is down, the main model decides through a `no_response` action. The system never pretends to be Jev.

## 4. The instance home (`DOM_HOME`)

This set is lighter than OpenClaw's. It borrows Hermes's bounded, agent-written memory and keeps Dom's per-person and per-chat notes. **The privacy boundary is a folder.** Code reads `private/` only when building a prompt for the owner's DM.

```
DOM_HOME/
  config.toml            owner id, models, desktop mode, group policy (no secrets)
  .env                   secrets: bot token, API keys
  SOUL.md                who Dom is — written by the owner, Dom may propose edits
  memory/                public-safe; Dom edits freely (tier 1)
    MEMORY.md            world + friend-circle knowledge        ≤ 2,200 chars
    HABITS.md            learned rhythm: sleep, busy hours      ≤ 1,000 chars
    JOURNAL.md           diary; never loaded into chat prompts
    people/<uid>.md      one per person, keyed by numeric id    ≤ 1,000 chars
    chats/<chat_id>.md   one per chat: running jokes, vibe      ≤ 1,000 chars
  private/               owner DM only
    USER.md              the owner: preferences, how to help    ≤ 1,375 chars
    NOTES.md             assistant memory                       ≤ 2,200 chars
  skills/<name>/SKILL.md procedures; only name + description are in the prompt
  plugins/<name>/        one folder per plugin; status (pending, approved, disabled) lives in SQLite
  state.db               SQLite (plugin status, storage blob and trial result included)
  transcripts/<chat_id>.jsonl
```

**`SOUL.md`** holds only:
- a short "who" header: name, age, where he lives, what he does
- character, voice, humour, values
- how he treats friends
- default personality sliders, in the front matter

Things that leave `SOUL.md`:
- owner IDs
- sleep hours (moved to `HABITS.md`, learned)
- action names like `READ_ONLY`, which become tool or JSON schema
- file paths
- privacy rules (now enforced in code)
- prompt-injection handling (moved to code and the system frame)

**Who writes notes.** Notes are written by the background memory pass (§7), never by a reply. Since v0.8.1 (W8, E41) the owner can turn on `memory_self_edit` (off by default). Then, in the owner's DM only, Dom has `propose_memory_edit` to propose an add, replace, remove or set on `private/USER.md`, `private/NOTES.md`, the journals, `memory/MEMORY.md`, and the people and chat notes. Each proposal is a diff with a one-line reason on an approval card (Telegram and dashboard); nothing is written until the owner approves, approval refuses a stale file, and these proposals are excluded from "Approve all". `SOUL.md`, `HABITS.md`, config, skills and plugins are out of scope.

**Prompt cache.** Shared `MEMORY.md` is swapped into the prompt at most hourly. Per-chat notes are swapped only when the chat's window is trimmed or hourly. Between swaps the turn note carries a "notes updated since HH:MM" line, and `read_notes` returns the latest, scope-filtered like the prompt.

**Limits.** When a memory write would go over its limit, the tool returns an error and the current content. Dom must then merge or prune, as Hermes does.

**Per-chat sliders.** Overrides live in the `chats` table and are edited in the UI.

**Starter kit.**
- `dom/starter_home/` ships a neutral starter soul and empty memory, as package data.
- `dom init` copies it and walks the owner through setup.

## 5. Chat scopes

| Scope | Who | Prompt contains | Tools |
|---|---|---|---|
| `owner_dm` | the owner | SOUL, memory/, private/, that chat's notes | all enabled tools |
| `friend_dm` | anyone else, after owner approval | SOUL, MEMORY, that person + chat notes | chat tools only |
| `group` | allowlisted groups | SOUL, MEMORY, participants' people notes + chat notes | chat tools only |

**Group approval.** When Dom is added to a group, or DMed by a stranger, the chat is `pending`. Dom stays silent and asks the owner in DM, with Approve and Ignore buttons. Nothing reaches a model before approval. This prevents free tokens for strangers.

**Owner.** Claimed during setup: `dom init` prints a one-time code, and the owner sends `/claim <code>` to the bot. The owner is never hardcoded.

**Outbound guard.**
- Layer 1, structural: private files and private tools are never in non-owner prompts.
- Layer 2: in non-owner chats, Jev scores each outgoing message for private leakage. The state it sees is the headings and key terms of `private/`. Above the threshold, the message is dropped and the owner is told.
- This replaces the regex DLP.

**Ephemeral replies (Bot API 10.2+).** When the owner asks something assistant-ish inside a group, Dom can answer with an ephemeral message only the owner sees. (E32: Telegram sends these only from a group admin; where Dom is not one, the reply goes to the owner's DM with the reason, and the group history records only that the owner was answered privately.)

## 6. Presence

Based on V1's cycle, which the owner liked, and improved. Each chat has its own state: `asleep | offline | online(until)`.

**Sleep.**
- Comes from `HABITS.md`, which Dom maintains.
- The first-run default is V1's 23:59–07:30 in the owner's timezone.
- During reflection, Dom shifts it based on when people actually message him and on his personality. There are no owner-set bounds; a sanity rule keeps sleep between 4 and 11 hours.
- Messages that arrive during sleep are stored. On waking, Dom catches up.

**Online and offline.**
- Session lengths come from a triangular distribution, as in V1, and are tuned by `HABITS.md`.
- While offline, messages queue.

**Waking early.** Any of these brings Dom online:
- many messages queued (V1's `MAX_OFFLINE_MESSAGES`)
- a Jev "kapoh" score above the threshold
- an owner override (`/wake`)
- a reminder that's due

**Online doesn't mean replying.** Jev picks one of `reply | react | ignore`.

**Humanlike actions**, all real Bot API calls:
- typing indicator, with typing time proportional to reply length
- multi-bubble replies, `reply_to`, reactions
- **send-then-delete** for the "typed something awkward" move

**Not possible as a bot:**
- read receipts (no Bot API method as of 10.4)
- deleted-message events, which are Business-only

Edits are handled, including edits made while Dom was offline.

**Bot-to-bot.**
- The owner enables Dom's *Bot-to-Bot Communication Mode* in BotFather.
- In groups where Dom is admin or has privacy mode off, he then receives all messages from other bots.
- A deterministic stop applies:
  - a per-chat depth counter of consecutive bot↔bot turns, default 6
  - at most 1 reply per bot every 10 s
  - repeated-message dedup
- At the limit, Dom sends one in-character exit line, then ignores bots in that chat until a human speaks.

## 7. Agent turn

1. **Build the prompt:**
   - stable prefix: system frame + SOUL + scope-filtered memory snapshot + tool schemas
   - then the XML chat window (last N messages with ids, sender ids, reply links, media descriptions)
   - then a per-turn note: time, a coarse availability cue, messages that arrived while away, desktop status, and conditional lines (answering late, messages that came in mid-answer, a settled vibe, notes updated)
2. **Run the loop:** the model calls tools until it returns the final JSON action list. That list contains:
   - `bubbles[]`, each with text, an optional `reply_to` and an optional `delete_after`
   - `react`
   - `no_response`
   - **Online window cue (E46).** The note gives no countdown. `online_window_note` maps the minutes left to one of four phrases (about to head off, heading off soon, around a bit longer, around a while), so Dom has no number to quote.
   - **Machinery rule (E46).** The frame (`MACHINERY_RULE`, also in the in-voice report frame) tells Dom never to speak of online minutes left, restarts, versions, timings or tool names, and to give an in-life reason when wrapping up. In the owner's DM he may answer plainly when asked about his setup or why a reply was slow, or in assistant mode.
3. **Guard check** (§5), then execute with humanlike timing. Since v0.8.1 Jev triages a message on arrival (typing, reaction and, in the owner DM, an "On it." line start then, and Jev judges whether the sender is done); a message that arrives before the first bubble restarts the attempt, and a later one gets a follow-up turn.
4. **Log** everything to the store and the transcript.

**Core tools.** Each one carries its own usage guidance in its description. `enabled_tools` (`dom/tools.py`) keeps a fixed order for the prompt cache: shared tools first, owner-only tools last, so a group or friend's DM never sees an owner-only tool name and a tool that comes or goes reshapes only what follows it.

| Tool | Scope |
|---|---|
| `search_history` | FTS over the current chat; the owner can search all chats |
| `skills_list` / `skill_view` / `skill_manage` | all (`skill_manage` makes a proposal the owner approves) |
| `read_notes` | all; the latest notes for the current scope, never `private/` outside the owner DM |
| `web_search` | all; only when `[models.search]` is configured |
| `switch_vibe` | all; only while the Everywhere preset is Auto, once a day, and refused where a chat pins its own preset |
| `plugin_*` | per the plugin's declared scopes (§8) |
| `remind` | owner DM only (friends no longer get it) |
| `draft_plugin` | owner DM |
| `write_brief` | owner DM (§8 tier 3) |
| `self_inspect` | owner DM, read-only: version, repository, enabled tools, plugins, desktop, models, recent errors; `include_timings` adds recent reply timings |
| `own_code` | owner DM, read-only: changelog, list, read and search of the installed code and docs |
| `find_devices` | owner DM, read-only: LAN devices; a moved configured host becomes a `lan_address` proposal |
| `task_mode` | owner DM: `start` or `end` assistant mode for planning, research or admin |
| `propose_memory_edit` | owner DM, only when `memory_self_edit` is on (§4) |
| `start_desktop_task` | owner DM, only when a desktop host is usable (§9) |
| `coding_overview` / `coding_thread` / `coding_search` / `coding_send` | owner DM, only when the T3 bridge is available; `coding_send` asks the owner to confirm the exact text |
| ~~`memory`~~ | replaced by the background memory pass (`dom/memory_pass.py`): after an online session ends, or when unprocessed messages are about to leave the chat window (since W17: when a trim would drop messages the pass has not read, the trim waits for the pass), one model call turns new messages into add / replace / remove ops on the scope's note files, limits enforced; `private/` files in `owner_dm` only. Replies never write notes. |
| ~~`extend_online`~~ | removed in v0.8.1 (W4): after a reply, Jev decides whether Dom stays online longer (`Dispatcher._stay_check`: no, 10 or 30 minutes, within the same limits). The decision runs after the bubbles are out and is shown as "Jev after reply" in the timing view. |
| ~~`desktop_*`~~ | replaced by `start_desktop_task`; the worker agent has its own tools (§9) |

Dom does not pick a skill with a tool call first: Jev's arrival triage names the one approved skill that fits (or none) and its text goes into the turn note; `skill_view` stays as the fallback.

Media is transcribed or described once by the media model when it arrives. The description is stored with the message.

## 8. Self-evolution (three tiers)

1. **Text, changed freely:** `memory/*`, `HABITS.md`, `JOURNAL.md`, `skills/`. Notes change through the background memory pass. `HABITS.md` is written by code from message-time statistics, never by a model. The journal and slider nudges come from a daily reflection job.
   - Reflection also proposes slider nudges, at most ±0.1 each time.
   - Reflection may *propose* `SOUL.md` edits. The owner approves them in DM or in the UI.
2. **Plugins:**
   - Dom drafts a plugin (`draft_plugin`, owner DM; the draft comes from the `drafting` model, §3) into `plugins/<name>/`. Whether it is pending, approved or disabled is stored in SQLite, keyed by the hash of its exact bytes.
   - The owner reviews the full code, plus a diff on updates, on the dashboard's Plugins page (roadmap phase 6; the DM card links there). Only the owner activates it: a button in their DM, or the dashboard's approvals (owner-only, behind Cloudflare Access), which the phase 6 plan added and the owner accepted.
   - **Trial run (W9).** Before the owner approves, the staged bytes are run once in the Bubblewrap sandbox with synthetic input, networking off and disposable storage: the plugin's `self_test`, else its first declared panel, command or tool. The result and elapsed time show on the Telegram approval card and the Plugins page. A failed or pending trial does not block approval; the owner decides.
   - Plugins run in a Bubblewrap subprocess with an empty environment (no secrets) and talk over JSON-lines on stdio.
   - **Storage (W9).** A plugin may declare `storage = true` if it is offline; that is part of what the owner reviews. It then keeps one JSON blob of at most 64 KiB (100 levels deep) through `_dom.load_storage` and `save_storage`. The blob lives in SQLite outside the sandbox, bound to the plugin's name, is shared across approved versions, and is saved only after a valid response. Such a plugin's tools and commands are `owner_dm` only. Deleting the plugin archives and removes its blob.
   - A plugin can add tools, Telegram slash commands and dashboard panels (owner decision E11, 2026-11-20). Commands reply with plain text; panels return structured data that Dom renders with its own templates. Plugins never ship HTML or JavaScript.
3. **Core code:**
   - Dom writes an issue-style brief.
   - The owner forwards it to a coding agent (T3 Code with Claude, Codex or Antigravity), which opens a PR.
   - Dom never edits his own core.

## 9. Tiny computer (optional)

**Modes**, set in `config.toml` → `desktop.mode = "local" | "remote" | "off"`:
- `local`: a container on the Pi, started on demand and stopped after idle (default 10 min).
- `remote`: the same container on another host, such as the homelab, reached by URL + API key.
- `off`: no desktop.

**Dom always knows the status.** A health check sets `desktop: connected | starting | unavailable | off` in the per-turn note. `start_desktop_task` is offered only when it is usable. Otherwise, Dom says in character that his computer isn't with him.

**Contents.** A real Linux box: Chromium, shell, Python/Jupyter, files. It holds no secrets.

**Logins.** The task's worker calls `handoff` with a reason; Dom never asks for a password in chat. The owner gets a link to the live view (VNC behind the UI's auth), logs in, and taps "done". Cookies stay in the container profile and never reach Dom's core.

**Candidates:** AIO Sandbox (`ghcr.io/agent-infra/sandbox`) and Cua (`ghcr.io/trycua/linux:24.04[-slim]`). Both have arm64 images.
- **Chosen: AIO Sandbox, run lean** (`2026-11-19-desktop-spike-results.md`). Confirmed on the Pi: it runs there with the DOM-DESKTOP LAN firewall.
- Desktop tasks run in a worker agent that never sees `private/`; see the roadmap, phase 5.
- Acceptance: idle under 1.2GB and working peak under 1.6GB on the Pi, with the core still responsive.
- If neither passes, `local` mode uses the slim image without VS Code. The full image goes to `remote`.

## 10. Scheduler

- **Reminders** have three tiers:
  - **critical** fires on time, even if Dom is asleep
  - **normal** fires within ±5 min, while he's awake
  - **casual** is folded into the next natural conversation
- **Heartbeat** jobs: reflection or daydream, habit learning, and memory-import review.
- Reminders live in the `reminders` table and are created only through `remind`. Heartbeat jobs record their last run in `kv`. The salvaged `scheduled_jobs` table is unused.

## 11. Memory import

1. The owner uploads a text export from another AI service with `/import` in their DM or on the dashboard, and picks the service. The file is not kept.
2. A line parser splits it into facts, skipping ones Dom already has. Each fact becomes a private `memory_fact` proposal; the owner's review is the classification (E16), with no automatic private/shareable/discard step.
3. Approved facts are merged into `private/USER.md` within its limit; rejected ones are dropped. Nothing moves to `memory/` from an import.
4. Nothing goes public by default.

## 12. Web UI

- FastAPI serves a small single-page UI.
- Auth: Cloudflare Access in front; Dom checks the Access JWT on every request. It binds to the Docker bridge address that the tunnel container reaches, never a LAN address. A dev token exists for local testing only.
- Pages:
  - chats and their presence state
  - approvals: groups, plugins, soul edits, imports
  - memory and soul editor, including "talk to Dom to edit his soul"
  - desktop live view
  - logs
- The JARVIS look gets a dedicated design pass (Opus) after the pages work. The old `server.py` is a visual reference only.

## 13. Salvage from V2

| Take | How |
|---|---|
| `v2/storage/hybrid_store.py`, migrations 0001–0003, tests | Copy. Drop Telethon-specific fields. Re-run its tests. |
| `workspace/` data | Import with a script into the new `DOM_HOME` layout, split into `memory/` vs `private/`. `SOUL.md` gets rewritten (§4). |
| `scripts/migrate_v1_to_v2.py` | Keep as a reference for the importer. |
| `contracts.py`, XML window formatter, approval-gate idea | Rewrite smaller inside the new modules. |
| Everything else | Leave in place. It stays on `feat/v2-modernisation` until the owner decides to archive it. |

## 14. Phases

Each phase ends with a demo on a real test bot, not mocks.

1. **Core chat.**
   - `dom init` + claim, config
   - aiogram adapter, store
   - scope-aware prompt, agent loop with JSON actions
   - approval gate for groups and DMs, outbound guard layer 1
   - importer from `workspace/`
2. **Presence and triage.**
   - sleep, online and offline, wake rules, Jev triage, kapoh
   - humanlike actions, edits, bot-to-bot with the stop
   - media understanding
3. **Memory and tier 1 evolution.**
   - bounded memory tool, people and chat notes, skills
   - reflection, journal, habit learning
   - guard layer 2, memory import
4. **Scheduler and UI v1.**
   - reminders, the approvals UI, the soul editor
5. **Tiny computer**, after the spike.
6. **Plugins (tier 2), `self_inspect`, tier 3 briefs.**
7. **JARVIS redesign, docs, Pi packaging** (compose file, setup guide).

## 15. Pi cleanup

Done on 2026-11-19: read-only look, backup, old pilot stopped (not removed), persistent logs, per-minute health log, Dom as a systemd user service. Still open: DHCP reservation, swap check. (Old containers and images removed 2026-11-21, E31.)

Original list:

1. Look around read-only.
2. Back up `state.db`, `runtime.env` and the session file.
3. Stop and remove the old pilot bot and dashboard containers and their stale images. They use the same bot token.
4. Keep the Cloudflare tunnel and its Access policy.
5. Give the Pi a DHCP reservation and update the ssh config.
6. Check that swap isn't thrashing the SD card. Consider zram.

## 16. Open questions for the owner

The current list is in the roadmap (D1–D15). The two below are kept for history.

1. Dom's voice: what in the current soul reads as "not Dom"? The rewrite in phase 1 starts from V1's prompt plus the classic preset.
2. Should Jev's leak check send private headings to OpenRouter? This is the same trust level as the main model, which already sees them in your DM.
