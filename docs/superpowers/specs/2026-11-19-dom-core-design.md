# Dom core — design

Status: phases 1–2 live on the Pi · 2026-11-19 · replaces the uncommitted V2 attempt on `feat/v2-modernisation`

**Phases 3–7 are revised in `docs/superpowers/plans/2026-11-19-roadmap-phases-3-7.md`.** Where the two disagree, the roadmap wins.

## 1. What Dom is

Dom is a Telegram **bot account** that acts like a friend in groups and DMs, and as a personal assistant to its owner in the owner's DM. It runs on a Raspberry Pi 4B with 4GB. Everything except the AI models runs on the Pi.

The code is a **general base** that anyone can set up. Personal details live only in the instance's home folder, never in the code. the owner's Dom is one instance with a soul written around him.

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

**Models**, all through OpenAI-compatible endpoints:
- Main chat model: any OpenAI-compatible endpoint in `[models.chat]`, with an optional `[models.chat_fallback]` used when the first is down. the owner's instance uses the homelab model `default`, falling back to `deepseek/deepseek-v4.1-flash-1017` on OpenRouter.
- A chat model may lack vision (the homelab one does). Anything visual goes through the media model.
- Media: `qwen/qwen3.8-omni-flash` for images, GIFs, video and voice.
- Triage: TypeSafe Jev via `POST /api/alpha/decisions` with `~typesafe/jev-latest`. This is not a chat model.

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
    imports/             raw memory exports waiting for review
  skills/<name>/SKILL.md procedures; only name + description are in the prompt
  plugins/{staging,active}/<name>/
  state.db               SQLite
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

**Ephemeral replies (Bot API 10.2+).** When the owner asks something assistant-ish inside a group, Dom can answer with an ephemeral message only the owner sees.

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
   - then a per-turn note: time, online-time-left, queued count, desktop status
2. **Run the loop:** the model calls tools until it returns the final JSON action list. That list contains:
   - `bubbles[]`, each with text, an optional `reply_to` and an optional `delete_after`
   - `react`
   - `no_response`
3. **Guard check** (§5), then execute with humanlike timing.
4. **Log** everything to the store and the transcript.

**Core tools.** Each one carries its own usage guidance in its description.

| Tool | Scope |
|---|---|
| `web_search` | all |
| ~~`memory`~~ replaced by the background memory pass (`dom/memory_pass.py`): after an online session ends, or when unprocessed messages are about to leave the chat window (window size − 5), one model call turns new messages into add / replace / remove ops on the scope's note files, limits enforced; `private/` files in `owner_dm` only. Replies never write notes. | all scopes |
| `search_history` | FTS over the current chat; the owner can search all chats |
| `skills_list` / `skill_view` / `skill_manage` | all |
| `remind` | owner, plus whitelisted friends |
| `extend_online` | all |
| `desktop_*` | only when a desktop is connected (§9) |
| `self_inspect` | read-only: version, enabled tools, plugins, desktop, models, recent errors |

Media is transcribed or described once by the media model when it arrives. The description is stored with the message.

## 8. Self-evolution (three tiers)

1. **Text, changed freely:** `memory/*`, `HABITS.md`, `JOURNAL.md`, `skills/`. Notes change through the background memory pass. `HABITS.md` is written by code from message-time statistics, never by a model. The journal and slider nudges come from a daily reflection job.
   - Reflection also proposes slider nudges, at most ±0.1 each time.
   - Reflection may *propose* `SOUL.md` edits. The owner approves them in DM or in the UI.
2. **Plugins:**
   - Dom drafts a plugin into `plugins/staging/`.
   - The owner gets the full code in DM, chunked, plus a diff on updates. Only the owner's button press in their DM activates it.
   - Plugins run in a subprocess with an empty environment (no secrets) and talk over JSON-lines on stdio.
   - A plugin can add tools only. Telegram and UI extension points come later, when there is a real need.
3. **Core code:**
   - Dom writes an issue-style brief.
   - The owner forwards it to a coding agent (T3 Code with Claude, Codex or Antigravity), which opens a PR.
   - Dom never edits his own core.

## 9. Tiny computer (optional)

**Modes**, set in `config.toml` → `desktop.mode = "local" | "remote" | "off"`:
- `local`: a container on the Pi, started on demand and stopped after idle (default 10 min).
- `remote`: the same container on another host, such as the homelab, reached by URL + API key.
- `off`: no desktop.

**Dom always knows the status.** A health check sets `desktop: connected | starting | unavailable | off` in the per-turn note. `desktop_*` tools exist only when it is usable. Otherwise, Dom says in character that his computer isn't with him.

**Contents.** A real Linux box: Chromium, shell, Python/Jupyter, files. It holds no secrets.

**Logins.** Dom calls `desktop_handoff(reason)`. The owner gets a link to the live view (VNC behind the UI's auth), logs in, and taps "done". Cookies stay in the container profile and never reach Dom's core.

**Candidates:** AIO Sandbox (`ghcr.io/agent-infra/sandbox`) and Cua (`ghcr.io/trycua/linux:24.04[-slim]`). Both have arm64 images.
- **Chosen: AIO Sandbox, run lean** (`2026-11-19-desktop-spike-results.md`). Still to confirm on the Pi itself.
- Desktop tasks run in a worker agent that never sees `private/`; see the roadmap, phase 5.
- Acceptance: idle under 1.2GB and working peak under 1.6GB on the Pi, with the core still responsive.
- If neither passes, `local` mode uses the slim image without VS Code. The full image goes to `remote`.

## 10. Scheduler

- **Reminders** have three tiers:
  - **critical** fires on time, even if Dom is asleep
  - **normal** fires within ±5 min, while he's awake
  - **casual** is folded into the next natural conversation
- **Heartbeat** jobs: reflection or daydream, habit learning, and memory-import review.
- Jobs live in `scheduled_jobs` (salvaged schema) and are created only through `remind` or the system.

## 11. Memory import

1. The owner uploads an export from another AI service in DM or the UI. It lands in `private/imports/`.
2. A job splits it into facts. Each fact is classified `private | shareable | discard` (Jev `choice`), and the owner reviews them in the UI.
3. Accepted facts are merged into `private/USER.md` and `private/NOTES.md`, within their limits. Shareable facts go to `memory/` only after review.
4. Nothing goes public by default.

## 12. Web UI

- FastAPI serves a small single-page UI.
- Auth: Cloudflare Access in front, plus a local session token. It binds to localhost.
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

Done on 2026-11-19: read-only look, backup, old pilot stopped (not removed), persistent logs, per-minute health log, Dom as a systemd user service. Still open: remove old containers and images, DHCP reservation, swap check.

Original list:

1. Look around read-only.
2. Back up `state.db`, `runtime.env` and the session file.
3. Stop and remove `dominic-dm-v2`, `dominic-dashboard` and `dominic-dm-pilot`, plus the stale `dominic-v2:*` images. They use the same bot token.
4. Keep the Cloudflare tunnel and its Access policy.
5. Give the Pi a DHCP reservation and update the ssh config.
6. Check that swap isn't thrashing the SD card. Consider zram.

## 16. Open questions for the owner

The current list is in the roadmap (D1–D15). The two below are kept for history.

1. Dom's voice: what in the current soul reads as "not Dom"? The rewrite in phase 1 starts from V1's prompt plus the classic preset.
2. Should Jev's leak check send private headings to OpenRouter? This is the same trust level as the main model, which already sees them in your DM.
