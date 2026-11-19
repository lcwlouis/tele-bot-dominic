# Roadmap review — phases 3 to 7

Written 2026-11-19 after phases 1–2 went live on the Pi. It revises §14 of the design spec. Each phase still gets its own task-level plan before it is built; this document fixes scope, order, design gaps and open decisions.

Code state reviewed: `dom-core` at `14fa8d8` (374 tests, about 4,200 lines).

## What changed since the spec was written

- Replies no longer write memory. A background memory pass does (`dom/memory_pass.py`).
- The chat model is the homelab `default` model with DeepSeek on OpenRouter as fallback. **The homelab model reports `vision: false`**, so anything visual must go through the media model.
- The desktop spike is done: AIO Sandbox, run lean (369MB idle, 799MB busy). Not yet confirmed on the Pi itself.
- Owner controls exist in Telegram: `/status`, `/chats`, `/pending`, `/wake`, `/sleep`, Leave buttons, command events in the owner DM.
- Deployment is a systemd user service with release folders, not Docker Compose.

## Gaps found in what is already live

These are small and should be fixed before phase 3 starts ("phase 2.5").

| # | Gap | Why it matters | Fix |
|---|---|---|---|
| G1 | A friend's **DM** memory pass may write `MEMORY.md` and `people/<uid>.md`, which group prompts load. | What a friend tells Dom one-to-one can surface in a group. | A `friend_dm` pass writes only `memory/chats/<dm_id>.md`, which is loaded only in that DM. |
| G2 | `web_search` is in the spec's tool table but in no plan. V1 had search. | Dom can't look anything up. It's a regression from V1. | Build in phase 3 (see 3.1). |
| G3 | Per-chat sliders are imported into `chats.traits_json` but never reach the prompt. Trait names differ in three places (`sarcasm`, `sarcasm_level`, `slang_level`/`singlish`). | Dead data; the per-chat personality the owner asked for doesn't exist. | One canonical name set; prompt merges chat overrides over soul defaults. Phase 3. |
| G4 | No token or cost tracking. | "No free tokens" can't be checked. A loop bug on the fallback model would spend money unseen. | Record `usage` from every model and Jev response; show today's totals in `/status`; a daily spend cap on OpenRouter calls that pauses non-owner chats and DMs the owner. |
| G5 | No backups. Everything lives on one SD card that has already dropped out once. | Loss of all memory and history. | Nightly `sqlite3 .backup` plus a tar of the home, 7 kept on the Pi, and a copy pushed to the homelab. |
| G6 | `dom-core` (about 70 commits) exists only on this Mac. V2 is still uncommitted in the repo root. | One disk failure loses the rebuild. | Push `dom-core`; commit V2 to `archive/v2-attempt-1`. Needs the owner's go-ahead. |
| G7 | README, `Dockerfile` and `docker-compose.yml` on the branch still describe V1; the Dockerfile runs a file that no longer exists. | Misleads anyone opening the repo. | Delete the two Docker files now; short honest README now, full docs in phase 7. |
| G8 | `private/USER.md` is empty; `private/NOTES.md` and the people notes hold V2 boilerplate, several over their limit. | Dom knows little about the owner and wastes prompt space. | One supervised clean-up pass; the owner reviews `USER.md` before it is saved. |
| G9 | The phase 2 live checklist was never finished: voice note, photo, sleep/wake catch-up, bot-to-bot against a second bot. | Phase 3 builds on behaviour that hasn't been seen working. | Run it before phase 3. |
| G10 | While offline, the owner's DM waits for 5 messages or the next session (15–90 min). | An urgent assistant request gets no answer unless the owner remembers `/wake`. | Offline triage in the owner DM gains one Jev question, "does this need Dom now?". Above the threshold he comes online. |
| G11 | Homelab fallback is silent. | The owner can't tell which model answered, or that the homelab is down. | `/status` footer shows the model used last and when fallback last triggered. |

## Phase 3 — Memory, reflection and the things that make Dom feel alive

**3.1 Web search.** One tool, `web_search(query)`, all scopes. Results are untrusted text, wrapped like chat content.
- Decision needed (D1): which provider. Default proposal: OpenRouter's web search on a cheap model, because it needs no new key. Verify with one real call before building.

**3.2 Sliders and presets.**
- Canonical trait names in one place. The prompt shows soul defaults with the chat's overrides applied.
- Presets become files in `DOM_HOME/presets/*.md` (overlay text plus slider values). The owner sets one per chat from `/status` or the UI. This restores the classic, "fboy" and assistant presets the owner asked to keep.

**3.3 Reflection and journal.** One daily job, run shortly before Dom's sleep time.
- **Privacy rule.** Reflection reads only non-owner chats and public memory, and writes `memory/JOURNAL.md`. So the journal is public-safe by construction and Dom may talk about it with friends. Anything about the owner's DM goes to a separate `private/JOURNAL.md` written by a second call that the public one never sees. (D2 confirms this split.)
- The journal's latest entry yields a one-line "mood" that enters the turn note. Otherwise the journal affects nothing, which wastes it.
- Slider nudges: at most ±0.1 per trait per day, logged in the journal entry.
- Soul edit proposals go through the proposals mechanism (4.2), not straight to disk.

**3.4 Habit learning without a model.**
- `HABITS.md` is written by code, from numbers only: an hourly histogram of incoming human messages over the last 14 days.
- Sleep start and end move toward the quiet window by at most 30 minutes a day. Personality sets the bias (a night-owl soul leans later).
- Because no chat text is involved, a group can't talk Dom into staying awake. This closes the injection hole that got `HABITS.md` taken away from the memory tool.

**3.5 Proactive messages.** Missing from the spec entirely, and the biggest single contributor to "feels like a person".
- The heartbeat may produce an *opener* in an approved chat: a morning text, a follow-up on something a friend mentioned, a link he found.
- Hard limits in code: off by default per chat; at most N openers per chat per day; never while that chat is asleep; never two unanswered openers in a row.
- Decision needed (D3): which chats, and how often.

**3.6 Self-knowledge.** A short capabilities block generated from code (tools available in this chat, desktop status, models) goes in the turn note. It replaces stale claims in memory such as "lives in a container with a display". This is the cheap half of `self_inspect`, moved up from phase 6.

**3.7 Outbound leak check (guard layer 2).**
- Run an evaluation first: about 40 synthetic outgoing messages, half leaking facts from a test `private/` folder. Ship the check only if Jev separates them cleanly. If it doesn't, layer 1 stays the only guard and the spec says so.
- Decision needed (D4): what happens when Jev is down. Proposal: send anyway and log, because the structural boundary is the real guarantee and a mute Dom in every group is worse.
- Decision needed (D5): the check sends headings and key terms from `private/` to OpenRouter.

**Moved out of phase 3:**
- **Skills** move to phase 5. A skill is a procedure for using tools, and until the desktop exists Dom has no tools worth writing procedures for. When they arrive: skills are public-scope content, and every new or changed skill is a proposal the owner approves.
- **Memory import** moves to phase 4, because reviewing facts needs the approvals UI.

## Phase 4 — Assistant features and dashboard v1

**4.1 Design direction first.** One Opus session produces the JARVIS layout and design tokens (a mock, not code) before any page is built. The old order built every page plainly and restyled all of it in phase 7.

**4.2 Proposals.** One table and one flow for everything that needs the owner's yes: soul edits, skills, plugins, imported facts. Each proposal appears as a DM with Approve / Reject buttons and as a card in the UI. Group approval keeps its existing path.

**4.3 Reminders.**
- `remind(when, text, importance, repeat?)`, owner DM only at first. `when` is an ISO time in the owner's timezone; `repeat` is `daily | weekdays | weekly`. No cron syntax and no new dependency.
- `/reminders` lists and cancels.
- **Critical reminders must not depend on a model.** At fire time Dom tries an in-character message; if the model fails or takes over 20 s, the stored text is sent as is.
- Reminders missed while the Pi was off fire on start-up, marked late.
- Casual reminders become a line in the next turn note for that chat.

**4.4 Ephemeral replies in groups** (spec §5, previously in no phase). Verify against the live API first.

**4.4b Stickers and GIFs.** A `sticker` action. Dom chooses from sticker sets the owner adds in the dashboard (by set name); each sticker's emoji is its meaning. GIFs come later, through search.

**4.5 Dashboard v1.**
- Runs inside the Dom process; server-rendered pages with htmx. No Node build step on the Pi.
- Auth: verify the Cloudflare Access JWT (signature and audience) on every request. Bind to localhost.
- Pages: chats and presence; proposals; memory and soul editor; usage and cost; turn log.
- "Talk to Dom to edit his soul" is a chat panel whose only output is a soul-edit proposal with a diff.
- The tunnel currently points at the old dashboard container. Repointing it is a change in the Cloudflare dashboard (D6).

**4.6 Memory import.** Upload in DM or UI → facts split and classified → each fact is a proposal. Needs sample exports (D7).

**4.7 Operations.** Backups (G5) and the spend cap (G4) land here if not done earlier.

## Phase 5 — Tiny computer

**5.1 Confirm on the Pi.** One image, pulled detached, health log watched. Measure page-load and screenshot latency. If the SD card can't take the 8.4GB image, local mode needs a USB SSD, or the Pi uses remote mode. (D8: power supply and storage.)

**5.2 Worker agent, not the chat turn.** This is the main design change.
- A desktop task runs as a separate agent whose prompt contains the task brief and nothing from `private/`. Web pages are hostile input: with private notes in the same prompt, a page could tell Dom to send them somewhere.
- The chat turn starts the task and replies at once ("give me a few min"). The worker posts its result back as an event, and Dom reports in character. The chat's turn lock is never held during a task.
- Tasks are rows in a `tasks` table: status, steps, result. Visible in `/status` and the UI, cancellable.
- Owner DM only at first.

**5.3 Text first.** The chat model can't see images. The worker drives the browser through AIO's page-as-markdown and element-list endpoints. Screenshots go to the media model only when text fails.

**5.4 Network isolation.** The container gets its own Docker network with LAN ranges blocked, so a hijacked browser can't reach the homelab model server, the router or the Pi's own services. API bound to 127.0.0.1 with `SANDBOX_API_KEY` set.

**5.5 Files.** A `send_file` action (worker result → Telegram) and document intake (owner sends a PDF → file lands in the sandbox).

**5.6 Login handoff.** Live view proxied through the dashboard's auth. Depends on phase 4.
- Decision needed (D9): keep the browser profile between sessions (convenient; login cookies then sit on the Pi) or wipe it each time (matches "holds nothing"; log in every time).

**5.7 Skills.** As described under phase 3.

## Phase 6 — Plugins and core-change briefs

- **"Empty environment" is not isolation.** A subprocess can still read `DOM_HOME/.env` from disk and reach the LAN. Plugins run under `bubblewrap`: read-only view of their own folder, no home, network only if the manifest asks and the owner approved it. The Pi is the target; on other platforms plugins are refused, not run unsandboxed.
- The manifest declares tools, scopes (default owner DM only) and network need. Approval pins the code's hash; any change re-stages it.
- Review happens in the dashboard with a diff. The DM carries a summary and the Approve button; code in chunks on a phone is not reviewable.
- Core-change briefs: a markdown file in `DOM_HOME/briefs/` plus a DM. Optionally a GitHub issue (D10).

## Phase 7 — Polish and release

- JARVIS visual polish on the pages built in phase 4.
- `dom doctor`: checks tokens, BotFather settings (privacy mode, bot-to-bot), model endpoints, timezone, backups.
- `dom install-service`: writes the systemd unit and release layout now done by hand.
- Setup guide for a new owner, from BotFather to first message. CI running the tests.
- Before the repo is promoted: check git history for secrets and personal data, pick a licence (D11).

## Decisions (answered 2026-11-19)

| # | Question | Decision |
|---|---|---|
| D1 | Web search provider | OpenRouter web search, no new key (default; verify with a real call) |
| D2 | Public-safe journal plus a private one for the owner DM | Yes (default) |
| D3 | Dom texting first | Owner DM only, up to 2 a day |
| D4 | Leak check when Jev is down | Send and log (default) |
| D5 | Send headings and key terms of `private/` to OpenRouter for the leak check | Yes, as an experiment. If Jev can't do it, consider a small local classifier |
| D6 | Cloudflare tunnel | An agent set it up, so it can be repointed the same way when phase 4 is ready |
| D7 | Memory import sources | Muse, ChatGPT, Claude, Siri. No sample files; see "Memory import design" below |
| D8 | Pi power and storage | 30W USB-PD brick; treated as adequate. The health log (`pi-health`) records under-voltage if it happens. SD card stays the storage risk for the 8.4GB image |
| D9 | Desktop browser profile | Keep logins between sessions |
| D10 | Core-change briefs | File plus DM only (default) |
| D11 | Repo visibility and licence | Public, MIT (already so on GitHub). All personal data must be cleaned first; see "Publishing" below |
| D12 | Push `dom-core`, archive V2 | Yes, with the conditions in "Publishing" |
| D13 | Old dashboard container | Stopped on 2026-11-19 (not removed; auto-restart off) |
| D14 | Stickers and GIFs | Yes. Phase 4: Dom sends from sticker sets the owner picks in the dashboard |
| D15 | Fixed address for the Pi | Owner's to do in the router app |

## Publishing (the repo is public)

- **Never push the V2 archive.** Its `workspace/` holds friends' messages, profiles and the owner's private memory. Archive it as a local branch or a tarball outside the repo, and keep `workspace/`, session files and image tarballs out of git.
- **`dom-core` history needs one rewrite before its first push.** Commit `1afe645` put the owner's Telegram ID into a plan document. It is gone from the tip, but still in history; remove it with `git filter-repo --replace-text` when no builder is working on the branch.
- Before every push: scan the diff for Telegram IDs, handles, chat titles, LAN addresses and domains. Add this scan to CI in phase 7.
- Code and tests already use made-up IDs only (checked across all `dom-core` commits).

## Memory import design (D7)

No service offers a clean "export my memories" file, so the import path is a prompt, not a parser.

1. `dom` ships one export prompt. The owner pastes it into each assistant; it asks for everything the assistant remembers about them, one fact per line, in a fixed format (`- [category] fact (when learned, if known)`), with nothing left out and nothing invented.
2. The owner sends the reply to Dom as text or a file, saying which service it came from.
3. Dom splits it into facts; each becomes a proposal (phase 4.2) marked private by default.

Per service, to be checked when built:
- **ChatGPT, Claude, Muse:** the prompt works wherever the assistant can see its own memory. Their settings pages also list saved memories, which can be copied as a fallback. Full data exports (conversation archives) are a different, much larger input and are out of scope.
- **Siri:** has no memory of this kind to export. Skipped unless Apple adds one.

## Suggested order

1. Phase 2.5: G1, G7, G9, G11, then G4 and G5. G6 follows the rules in "Publishing".
2. Phase 3 in this order: 3.2, 3.6, 3.1, 3.4, 3.3, 3.5, 3.7.
3. Phase 4, design direction first.
4. Phase 5, phase 6, phase 7.
