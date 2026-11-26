# Changelog

All notable changes to Dom are recorded here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions are git tags
named `vX.Y.Z`.

## [Unreleased]

- **Dom sees for himself.** When his chat model takes images, audio or video, he is sent the real photo, voice note or clip (the newest turn's, and his own latest generated media) instead of a description, at most 2 images, 1 recording and 1 clip per prompt (`media_input_*`) and up to `media_input_max_mb`. Media rides after the prompt text, so the cached prefix is unchanged. Older media stays a tag. What a model accepts comes from `input = [...]` on its route, the provider's model list (OpenRouter's input types), or a one-off background test with a generated image for other OpenAI-compatible servers (re-run it with `POST /api/media/inputs/probe`); unknown means text-only. A model that can't take a type, or refuses the request, gets the media model's description instead, and the Turn log says which.
- **Photos are described only when needed.** Incoming media is no longer described by default: only when the chat model can't take it, and once for the triage model, which can't see. Global search and the dashboard never showed descriptions, so they are unchanged; media he saw himself is described just before the memory pass reads its batch (within the spend cap, off the reply path), media that won't fit the per-prompt limits is described when catch-up reads it, and a photo that can't be downloaded gets a description or a "couldn't be loaded" note in its tag instead of silence.
- **Dom keeps what he sent.** Photos, voice notes and video notes are saved as files under `media/<chat id>/<month>/` with their scene or speech text, kind, time and Telegram `file_id`. The folder is owner-only, files are written atomically, and the owner API lists and serves them (`/api/media/files`). `studio.keep_cap_mb` (default 2048) caps the total: the oldest files are removed first and keep their `file_id`. They are left out of `dom backup`. At startup, files no message records (a crash between saving and recording) are removed.
- **Dom checks his photos before sending.** In the owner DM (`studio.look_owner_dm`, on by default; `look_friend_dm` and `look_group` are off), a model that takes images looks at a generated photo, or a video note's first frame when `ffmpeg` is installed, and regenerates once with an adjusted scene if it doesn't match. A cloud video note is redone, once and with the same inputs, only when its frame is clearly wrong (the wrong person, a broken frame, a wildly wrong setting); its check and redo count toward media usage and the spend cap. He sends the better of the two and never loops.
- **`/fresh`.** In the owner DM, `/fresh` starts that chat's window from now and `/fresh undo` puts it back; `POST /api/chats/fresh` does the same for any chat. Earlier messages leave the prompt but nothing is deleted and search still finds them. Dom is told in that chat that it was started fresh.
- **Dashboard design baseline.** The component library now shows the pieces list pages will be rebuilt from: a strip list whose details open beside it on a laptop or in a bottom sheet on a phone, icon buttons that show their label on hover and focus, info glyphs for descriptive text, status glyphs, a filterable picker, and tidied tokens for surfaces, status, type and motion. The folded sidebar opens for a look while the mouse rests on it, and its logo is the fold button.
- **Long lists stay calm.** The strip list groups its strips under Today, Yesterday, This week, This month and month names, takes a filter bar (text and status chips, filtered in place or by the page), counts what is loaded, offers back to top, and skips drawing strips that are off screen. On a laptop it scrolls in its own pane that fills the screen, beside its details; on a phone the page shows the newest ten and See all opens the rest as its own screen, which the back gesture closes. Tooltips no longer repeat a button's name to screen readers, a pinned info glyph closes on a tap elsewhere, and the picker goes back to the chosen option on Esc.
- **Unreachable chats and reachability checks.** Dom checks current chats on startup and on demand via the dashboard API, moving unreachable chats (including stale pending approvals) to a separate list while keeping their prior status and searchable history, with hide and forget controls; a chat that messages Dom again becomes reachable automatically.
- **Progressive recent messages and Telegram card cleanup.** The recent-messages API now returns messages newest first with cursor pagination (`before`), and Telegram menu cards (including `/status` and `/chats`) gain a Close button and clear their buttons once used or stale.
- **Hardening, AUTOINCREMENT IDs, and pinned CI.** Refused media keys that match a chat key's value at config validation and against running clients, serialized video spend-cap reservation before network submits, added a startup reconciler for persisted cloud video jobs, guarded settings views against malformed `.env` files, fixed search provider rewrites and OpenRouter search role-type validation, rebuilt `desktop_tasks` and `reminders` with `AUTOINCREMENT`, and pinned GitHub Actions by SHA and the Playwright CI image by digest with `npm ci --ignore-scripts`.
- **Dom knows what happened.** Successful updates put the installed release's full changelog section into the owner DM history once; and background failures leave short, rate-limited notes only in the affected chat.
- **The journal is archived, not dropped.** Entries past the last 30 move, one at a time, into `memory/journal/YYYY-MM.md` (and `private/journal/` for the private one) instead of being deleted. An interrupted move loses nothing. Global search covers the archive; the private archive stays in the owner's scope.
- **Dated facts, kept when replaced.** Note lines may end with `(as of YYYY-MM)` or `(until YYYY-MM-DD)`, and the memory pass is asked to write them. A line the pass replaces or removes goes to a sibling `.former.md` file with the date and reason, and a daily job moves lines past their `until` date there too.
- **Source-tagged memory.** Each line in people and chat notes records the chat it came from, or `any` for general facts. A tag counts wherever it stands in the line and is always stripped before a model reads it; former files are read-only to every edit; a pinned private line stays blocked from public text; and the memory pass can no longer forge a pin, carry a tag across a replace, or delete the line after the one it named. The prompt builder drops other chats' lines and strips the tags in code, and MEMORY.md refuses chat-tagged lines. Older untagged lines count as `any`, and tags don't count toward a note's size limit.
- **Rolling summaries.** Each completed archived week gets a short summary at the top of its month file, months roll up from weeks and years from months, using the memory model in the background under the spend cap. A week or month is summarised the day after it ends, straight from the live journal if its entries haven't moved yet (the live file is only read). The owner's DM sees a "recent life" block after the chat window: the last 4 weeks, 6 months and 3 years, any older period whose coarser summary doesn't exist yet, and the latest few days from the live journal that no summary covers, so there is no gap anywhere. The summary file changes about weekly. Raw entries are never changed.
- **Recent messages rank higher in search.** History search re-ranks its top hits with a soft 60-day half-life, so a fresh mention beats an old one without hiding it.
- **Core memories.** Lines you add or change by hand on the Memory page become pinned, and so can lines Dom chooses himself (no approval card; each records when and why). The memory pass never rewrites or drops a pinned line, Dom can't unpin yours, and `core_memory_cap` (default 30) means he must drop one of his own to add another. Core memories never expire, and searches don't fade messages from the day one was pinned. `GET /api/memory/core` lists them all.
- **Goals and noticings.** Set personal goals in your DM (`goals` tool) or through `/api/goals`, with optional categories, target dates, check-in rhythms, status history (`active`, `paused`, `done`, `dropped`) and linked reminders; Dom notes progress during nightly reflection and sends at most one gentle check-in per goal per rhythm under the opener daily cap. During the nightly reflection window Dom also writes at most 3 owner-only noticings (including Own work ideas) in one small cap-respecting call, keeping group context to R13-compliant one-line paraphrases with no verbatim group text; at most one noticing a day goes to your DM as a card with **Let's do it** (acts via existing tools, Own work, reminders, or an approval card outside chat) and **Not now** (dismisses and informs future reflection), and all of them are exposed through `/api/noticings` and Home's `for_you` feed.

## [0.8.3] - 2026-11-25

Dom gets his own studio: voice notes, round video notes and photos made on your own Mac, with cloud backups when it is away. Providers and models are set up from the dashboard, everything he remembers can be exported and searched, and he can message first in more chats.

- **Dom's own studio.** Dom can send voice notes, round video notes, photos and occasional "special" video notes made on your own Apple Silicon Mac, each behind its own switch in Settings → What Dom may do. His Telegram profile photo can follow the Everywhere preset. The studio runs as its own service (see studio/README.md), refuses anything but the listed bot hosts before the token check, and keeps memory free for the Mac's other users. When it is away, cloud backups take over or Dom says not right now.
- **Memory export.** Download every memory file as one zip from the dashboard or with `/export` in your DM. Secrets, the database and backups are never included.
- **Search everything.** One search box in the dashboard (and the phone menu) finds chats, memory files, briefs, reminders, desktop tasks, skills and plugins. Other chats show a one-line preview only. In your DM Dom's `search` tool finds the same things; in a group or a friend's DM it still searches only that chat.
- **Group memory stays in its group.** A group's memory pass follows stricter rules, so what a friend says in one group is filed for that group.
- **Highlighted code.** Plugin reviews and their diffs, skills, briefs, memory files and desktop task results colour their code, in dark and light.
- **Longer desktop tasks.** Set the step budget for your desktop tasks in Settings (10 to 100, default 30). A task about to run out asks in your DM: Keep going or Wrap up now. Extensions show on the Desk page.
- **Providers tab.** Add, edit, test and remove providers in Settings → Providers. Each model slot picks a provider and a model from its fetched list, with the reasoning levels that model offers.
- **Reminders bulk select.** Select several reminders and act on them at once.
- **Edit media limits from the dashboard.** Settings → What Dom may do → Media limits sets how many voice notes, video notes, photos and special videos Dom may send in your DM, friends’ DMs and groups, per day or per week, plus the gap between media and the total across all chats. It also shows what's left in your DM now and when the next media can go, such as "Next media from 22:37". A save applies at once. In config.toml, `video_daily = false` or `image_daily = false` now uses the weekly limit when that tier's daily default is set.
- **Studio over Tailscale.** The studio can allow bot hosts by their Tailscale name, and its setup guide covers running it on a tailnet.
- **Version label.** A development copy shows the release being built, such as "v0.8.3 pre-release", instead of the last release.
- **Photos in your own DM.** The leak guard no longer checks Dom's photos, voice notes and video notes in the owner's DM, as it already didn't for his messages. It had held back an ordinary "morning pic".
- **No pretend media.** When Dom can't send a voice note, video note or photo right now, he's told so and says not now. A bubble that writes media out as text ("[photo] a scene") becomes the real photo, voice note or video note when one is allowed, and is never sent as text.
- **Typed providers.** Each provider says what it is for: chat, media or decisions (web search services stay as they are). Add OpenRouter once per type, each with its own key variable. Roles offer only providers of their type: chat roles take chat providers, triage takes decisions providers, and studio backups take media providers. Existing configs load unchanged; an old `kind = "jev"` table reads as an OpenRouter decisions provider.
- **Pick the triage model from a list.** Triage now has the same searchable model picker as the chat roles, filled from OpenRouter's decision models and cached for a day with Refresh. A typed ID still works. A refused Test says why in one sentence, including a refusal under your zero data retention policy.
- **Dom can message first in more chats.** Turn on openers for approved groups or friends’ DMs in Settings → What Dom may do. Both switches start off. The daily limit applies to each chat.
- **Provider presets.** Start with editable connection details for 13 cloud and local chat providers in Settings → Providers.
- **Direct DeepSeek chat.** Use `deepseek-flash` or another model from DeepSeek’s own API. Thinking is off unless the route picks low, high or max. Usage estimates USD cost from cache hits, cache misses and output tokens. Cloud chat routes show a zero-data-retention recommendation where applicable.
- **Provider budget notices.** When every chat provider is blocked by a supported budget or credit limit, the owner gets one private notice per reset window. Home and `/status` show the active restriction and its local reset time when available. Testing or saving a model in Settings says the same instead of a bare HTTP 403. On the desktop, a refused screenshot description costs only that look: Dom reads the page's text instead of the task failing.
- **Real reasoning levels.** The model picker now offers the levels each OpenRouter model lists (for example none, low, high and max for DeepSeek V4.1 Flash) and shows the provider default, instead of a generic low, medium and high.
- **Folding sidebar.** On a desktop the dashboard's sidebar folds to a rail of icons, with each page's name as a tooltip. Use the button at its top or press `[`. The dashboard remembers the choice in this browser.
- **Markdown preview for memory files.** Each memory file has Preview and Edit tabs. HABITS.md and the journals open in Preview, and unsaved edits survive switching tabs. Raw HTML in a note shows as text, and only web and mail links work.
- **Talk to Dom about his soul, as a chat.** The soul panel is now a conversation beside SOUL.md as it is now. Dom answers in his own voice. When he has a concrete change, it shows as a diff you can approve or reject right there. A failed reply shows on your message with Retry. "He" in this chat always means Dom, so saying what he's like proposes the change. When Dom quotes SOUL.md loosely, he gets one retry to quote it exactly.
- **Cloud video backups.** Avatar 6 Turbo and Avatar 6 are available in the picker. Replace existing Avatar IV backups after its inline audio refusal.
- **Cloud voice costs.** The Usage log records speech calls immediately and fills in costs when OpenRouter reports them.
- **Smaller cloud photos.** Seedream 5.5 requests 1K square output. Oversized photos are resized and encoded as JPEG for Telegram.

- **Edit studio backups.** Studio cloud backups are edited on Settings → Models: add, edit, test, remove and reorder voice, photo and video backups, with OpenRouter's model list for each kind. Saving checks the key and the model without making any media.
- **Video note backup.** Round video notes can fall back to OpenRouter video when the local studio can't make them. Dom makes the voice line first and lip-syncs the approved outfit portrait to it. If the avatar fails, voice fallback reuses the generated audio. Replace legacy text-to-video backups with a supported lip-sync avatar.
- **Backup data policy.** Data policy for studio cloud backups only: zero data retention (the default), no training, or any endpoint. Every chat route through OpenRouter stays on zero data retention, and a backup's key variable may not be one a chat route uses. See docs/privacy.md for a separate media key.
- **Upgrade note.** A studio backup that shares a key variable with a model role (such as `OPENROUTER_API_KEY`) now stops config.toml from loading. Give the backups their own key variable (for example `OPENROUTER_MEDIA_API_KEY`) before upgrading.
- **Video costs count at once.** A cloud video job's estimated cost counts toward the daily spend cap as soon as it is accepted, and is corrected to the reported cost when the job ends.
- **Switches need a server.** Studio switches turn on only when the studio or a backup with its key set can serve them, and `/status` says when one is on with nothing behind it. A backup alone now serves its switch without the local studio. `/studio` shows availability per kind; worker and memory details stay on the dashboard.
- **Profile photo interval.** The profile photo changes at most every 1, 7, 14 or 30 days (default 14) instead of every hour. A refused change is tried again after an hour.

- **Rollback note: effort.** A reasoning effort other than `low` or `medium` (such as `high`) saved from Settings makes v0.8.2 and older refuse to start. Before starting the older release, set each `reasoning_effort` in `config.toml` back to `low` or `medium` (or delete it), and check again after restoring any backup copy (see docs/deploy-pi.md, "Roll back by hand").

## [0.8.2] - 2026-11-23

A new dashboard, and Dom keeps up with several chats at once: one conversation at a time in his head, nobody waits on a slow one, and replies read like someone typing.

- A new dashboard. Every page moved to a new app (Quiet Orbit, built in Svelte), and the old server-rendered dashboard is gone; old addresses redirect to their new pages. It works on a phone, with a menu instead of a bottom bar, and on desktop with a sidebar, in dark and light. Home shows what Dom is doing and his local time; Chats lists each chat's recent messages (your DM in full, other chats as one-line previews) and links to its turns; Usage segments open their breakdown; rejected and retired plugins are labelled and can be sent back for review. Writes need your login and come only from the dashboard's own pages.
- One chat at a time. Dom composes for one chat at a time, your DM first, then the others oldest first, while triage, reactions and sending stay parallel. The Turn log shows the waits, visits, cache use on return and cross-chat deliveries.
- Across chats. A mention of you or a reply to you in a group can alert you, and Dom can text you from a group or a friend's DM; both are rate-limited. A message from your DM to another chat needs your approval of the exact text and expires after two hours. In a group Dom knows a message from you is from you and talks to you, not about you.
- Readable messages. Replies render Markdown as bold, links, code blocks and aligned tables, and long ones split at sensible boundaries with code kept as code.
- Pacing. The first bubble goes out sooner (it counts the time already spent thinking); later bubbles of a long answer type at a believable pace instead of arriving a second apart.
- Search with fallbacks. Search is its own role in Settings with an ordered chain: SearXNG, OpenRouter web, DeepSeek and Tavily, each with a test call.
- Bigger model when stuck. In your DM a task that drags past two model rounds and a minute offers to switch to the escalation model, with a cost estimate; a task that failed can be retried on it once.
- The desktop on your phone. A full-page viewer with zoom, pan, scroll mode, a key bar and explicit copy and paste. Stopping the computer no longer holds the dashboard. Task reports come in Dom's words, and he proposes a skill only when asked to remember or after finding a workaround, with a short card and the full procedure attached.
- Fewer false alarms. Names and handles of people in a chat no longer count as private terms in the leak check, so a group reply that names you is not held back.
- Edge cases. Forum topics are caught up one by one; a group upgraded to a supergroup mid-reply keeps the unsent rest; critical reminders resume mid-send after a restart; flood-limited catch-ups retry; Dom never reacts twice.

## [0.8.1] - 2026-11-23

Faster, and yours to control: replies start sooner, you pick the models, and you can clean up and approve.

- Faster replies. Jev reads a message the moment it arrives, so "typing…", a reaction and, in your DM for a long request, a short "On it." start right away. When Jev is at least 70 % sure the sender is done, Dom skips the 2–6 s wait (never past 15 s). A message that lands before his first bubble restarts the answer with it included; one that lands later gets a follow-up that knows his earlier bubbles had not seen it. Typing time counts the time he already spent thinking, and task mode types twice as fast.
- Fewer model calls. Jev picks the fitting skill while it triages, Dom is told when a vibe switch cannot work, and Jev decides after a reply whether he stays online longer (`extend_online` is gone). A reply written as plain prose is usually sent as it is, not asked for again; this sends the reply text to Jev (`docs/privacy.md`). Notes change in a chat's prompt only when its window is trimmed or hourly, with a "notes updated" line and a `read_notes` tool between, so more of each prompt is cached.
- Where the time goes. The Turn log shows each reply's stages and cache use, and in your DM Dom can explain a slow reply when you ask.
- Still in character. Dom no longer mentions minutes left, restarts, versions, timings or tool names; he gives a reason from his life. In your DM he answers plainly when you ask about his setup.
- Models, your choice. A Settings page sets a provider per job (chat, fallback, media, drafting), each with a fallback chain, local or cloud. A new provider gets a real test call before it is saved; keys stay in `.env`. Search now asks OpenRouter for zero data retention too. The starter media model is `perceptron/perceptron-mk1.5`; an existing `config.toml` keeps its own.
- Clean-up. Retire or delete skills and plugins, and delete reminders, task history and settled approval cards, from the dashboard and your DM. Deleted items are archived for up to 30 days.
- Memory edits, if you want them. Off by default (`memory_self_edit`). When on, Dom proposes a change to his notes in your DM as a diff you approve or reject, never applied silently.
- Plugins and skills. An approved offline plugin can keep a 64 KiB JSON blob. Each staged plugin is trial-run in the sandbox and the result shows on its approval card. Claude Haiku 5.5 drafts plugins and skills.
- Stickers. Each sticker is described once by the media model (so `[models.media]` sees sticker images), and Dom picks by what it shows.
- Fewer silent failures. Telegram flood limits are waited out only briefly; long bubbles split safely; replies keep their forum topic after a restart; a group that becomes a supergroup keeps its settings; a friend hears a short in-character line when Dom has no answer; a failed sticker or GIF falls back to text; a critical reminder whose chat is gone stays active, marked undeliverable.

## [0.8.0] - 2026-11-22

Drift to zero: every decision the owner made since the rebuild is built and live-tested on the Pi, and two independent drift reviews (Gemini 4 Neon and Claude Fable 5.2) report zero drift.

- Dom remembers his own self-directed work: the owner's DM history now notes each plan's goals and reasons and how each task ended (without the web summary, which is untrusted page content). Before, he told the owner he had never researched a topic his own task had just finished. A desktop task the host refuses now tells the model plainly that nothing was started or queued; Dom had told the owner a refused task was "on the waiting list".
- Owner feedback from the phone: sticker sets can be added by their share link as well as their short name, and the dashboard lists each set as a link to preview it; the Own work page no longer squeezes each task into narrow columns on a phone; chat approval requests (in the DM and on Waiting for you) show the chat's Telegram ID and its first message, so the owner can tell who is asking.
- Self-directed tasks can read the pages they open. The first live run (2026-11-22) planned only `browse`, which opens a page without reading it, ran out of its 4 steps and filled the gap from memory. Every task now gets the read-only browser tools and at least 10 steps (or `max_steps` if lower), and its report may only say what the pages said. `read_page` returns a long page in parts of 4,000 characters (`part` reads further); before, only the top 6,000 characters ever came back, and scrolling did not change that. A self-directed task that runs out of steps but still reports what it found ends as done, marked partial, keeps the screenshots it names, and its DM summary includes the report.
- The Usage page draws the Desktop bar and its legend swatch in their own colour instead of black.
- A chat approval button tapped after a restart (Telegram calls the tap too old to answer) still approves or ignores the chat and updates the card, instead of failing before either.
- `/update` looks for releases at the repository's new address, `https://github.com/lcwlouis/tele-bot-dominic.git` (the old name still redirects). An instance whose `config.toml` sets `update.repo_url` to the old address keeps working through the redirect; change it when convenient.
- Dashboard: on the Now page the Turn log scrolls inside its panel (at most about 28 lines high) instead of stretching the page; the Turn log page still shows all of it.
- Errors redirected instead of swallowed: a private reply moved to the owner's DM is split to fit Telegram's 4096-character limit (counted in UTF-16, as Telegram does) and, if the DM refuses it too, the owner event gives the real reason. A primary chat model this key may not use (HTTP 401, 402, 403 or 404: an expired key, no credit, a withdrawn model) now falls back to the second model. When a chat's unread messages are given up on, the owner hears why: a plain line in his own DM, an event for other chats. A chat where Dom may not write (no rights, a closed or deleted topic) is given up on after the first failure instead of three.
- Dom knows whether he is an admin of a group (kept from Telegram's membership updates, else asked once per group). Where he is not, the turn note tells him a private answer goes to the owner's DM, and it goes there directly instead of after a refused in-group send.
- After a private reply in a group (shown to the owner, or moved to the owner's DM), the group's history gets an event saying the owner was answered privately, without the answer. Dom no longer thinks the owner is still unanswered and repeats the take publicly.
- Ephemeral group replies need Dom to be a group admin (Telegram answers `BOT_NOT_ADMIN` otherwise). A private reply Telegram refuses now goes to the owner's DM with a line saying why, instead of being lost, and the log names Telegram's reason. It is never sent publicly in the group.
- `/status`: the per-chat wake button reads "☀️ Wake <chat>" instead of just "☀️ <chat>", so it is clear what it does.
- Ephemeral group replies: a `"private": true` written inside the bubbles (where the home-lab model puts it) now makes the whole answer private instead of failing validation three times and leaving the owner with no answer; the turn note shows the exact shape. A malformed final answer's correction now says what was wrong by shape only (keys and error kinds, never text), so the retry can fix it instead of repeating it.
- Logins on the desktop go through the handoff: `start_desktop_task` tells Dom to have the worker call `handoff` at a login page (the owner logs in on the live view and taps Done) and never to ask for a password or code in chat, and the worker is told to call `handoff` instead of finishing when the brief needs a login it is not past. Image results (`.png`, `.jpg`, `.jpeg`, `.webp` up to 10 MB) are sent as photos that open inline; one Telegram refuses as a photo (too many pixels, a very long page) goes as a file.
- A desktop task that runs out of steps gets one last call, offering only `finish`, to say what it found, how far it got and what is left; the owner hears that instead of a bare "Stopped after 30 steps". A reply whose bubbles are bare strings (`["…"]` instead of `[{"text": "…"}]`) is read as those texts instead of being dropped, so a desktop report in Dom's voice no longer falls back to plain text for that, and an unusable report logs its shape.
- Desktop tasks on a model on the owner's own network (a home-lab vLLM) are no longer stopped by the $0.50 task ceiling: calls to a private, loopback or `.local` endpoint count as free, so such a task is bounded by its step budget. The log-in card's Done button turns into "✅ Resumed" or "Task ended" however the task moved on (Done on the dashboard or in Telegram, a cancel, a timeout or a restart).
- Desktop live view on a phone, a first step (full phone interaction is reworked in v0.8.1): the page fits the screen, and a "Type into the desktop" field below it brings up the phone's keyboard and sends what is typed as keystrokes, with Enter, Tab and Backspace buttons. Its Password mode, switched by hand, makes it a password field a password manager can fill; the text is cleared after sending, and noVNC's logging stays at "warn" so keystrokes never reach the browser console.
- A /proposals or /reminders list card is rebuilt from the database after each tap, so two quick taps no longer bring back the first row's buttons, and "Approve all on this page" goes once nothing on the page is pending. A bare `/import` sends the export prompt as its own code block, so one tap copies it with its line breaks.
- Dashboard: plugin panels and Own work on the Now page span the full width (they were squeezed into one of twelve columns); the Turn log chat filter lists only chats Dom is in, plus any in the turns shown; and deciding a proposal anywhere (dashboard, Plugins page or expiry) also turns its Telegram card's buttons into the outcome.
- Plugins: `draft_plugin` now tells Dom that every call starts fresh (no files, history or settings survive), that `version` is a short string and that the description must match `plugin.toml`. A plugin command or panel that fails records an event in the owner's DM with only its name and the kind of failure (never its output), so Dom knows on his next turn and can offer a fix, and a failure from trying to save a file says so.
- Telegram cards show what was done: after Approve or Reject on a proposal, or Cancel on a reminder, that button row turns into "✅ Approved", "❌ Rejected" or "Cancelled #N" (a button that does nothing), so a stale button cannot be tapped again; other reminders on a /reminders list keep their buttons.
- A second `skill_manage` proposal for a skill that already has one waiting is refused until the owner decides, so the owner's DM gets one card per skill. `skill_manage` now says skills are used in every chat and must carry no names or personal details, and `remind` says it is only for a reminder the owner asked for or agreed to, never a placeholder or test.
- The turn log records a turn cut off by a restart or shutdown as "Interrupted" instead of "Stayed quiet"; its unread messages are answered after the restart as before.
- Dom shows "typing" while he works out a reply (the model calls and any tools), not only while he types it, so a slow answer no longer looks like a crash; that time counts towards the reply's typing. One reply now types for at most 30 seconds in all (each bubble still up to 20), then each later bubble follows a 1-second pause, so a long task-mode answer no longer arrives minutes after it was written. The dashboard puts a separator before a chat's "Task mode:" line.
- A reply with more than 6 bubbles (a long itinerary, say) is no longer dropped as malformed: the extra bubbles join the 6th, and the reply format now tells the model the limit. A malformed reply is logged by its shape only (which fields failed and how long it was, never its text).
- `task_mode` takes `action` = `start` or `end` instead of `active` true or false: with the boolean the home model often sent "end" for a planning request. A tool call that returns an error now adds a "<tool> refused" step to the turn log and its reason to the journal, and `task_mode` is described as never for small talk or a one-word reply.
- Assistant mode for important tasks (W16): in the owner's DM only, Dom can call the new `task_mode` tool to wear the `assistant` preset for planning, research or admin, saying so in character, and return to his vibe when he calls it off or after 30 quiet minutes. A preset pinned on the DM still wins; it does not use the once-a-day `switch_vibe` and does not change the day's Auto pick. The Chats panel and `self_inspect` show it ([docs/presets.md](docs/presets.md#task-mode-owner-dm-only)).
- Cache-friendly prompt (W17): a chat's window no longer slides by one message per turn. It grows until the chat's whole prompt reaches `window_budget_tokens` (default 32,000, measured from the model's own reported `input_tokens`), then one trim drops the oldest messages down to about `window_keep_tokens` (default 20,000), so everything after the cached prompt prefix stays the same between trims. The window's start is stored per chat (`kv` entry `window:<chat id>`, no migration), so it survives restarts and does not move when messages are edited or deleted. A trim never drops a message the memory pass has not read: a due trim first starts the pass, and the trim lands in the next turn together with the note rewrite. `window_size` is now only a safety bound (default 500 messages); **an existing `config.toml` that still says `window_size = 80` keeps capping windows at 80 messages, so remove that line**. Dom's openers use the same window.
- The turn log records how long each step took: triage, each model call, each tool and the typing and sending, as `name@milliseconds` entries in the existing `turns.trace` (no migration). The dashboard's Turn log shows them as small chips ("Model 2.7 s", "Typing 4.2 s").
- Dashboard: the per-chat "Traits" section is now labelled "Personality sliders … tap to edit" and shows how many sliders differ for that chat, so the slider editor is easier to find.
- Self-directed work (W13): in his nightly daydream Dom may plan up to three small research tasks of his own, each on at most four named public sites. The plan is announced in the owner's DM (every site shown in full) and on the new dashboard **Own work** page, each task with a Cancel button; a plan whose announcement was not delivered never runs, and a task starts only after `[self_work] delay_hours` (default 3) while Dom is awake and idle. Tasks run only in a fresh local `self-work` container with an emptied browser profile, while no other local container is running, after a network guard verified from inside it shows LAN and private addresses unreachable; otherwise they are refused and the owner is told why. They are browser research only, with no shell or code: `browse` only to the planned sites (ambiguous URLs, ports other than 80/443 and public-suffix sites are refused), `click` only on the index of a link from a freshly read element list, `fill` only search text in a search box with Enter right after it, and files only under `/tmp` and the outbox; step and time budgets are enforced in code. Results and screenshots go to the dashboard, with one short summary to the owner, labelled as web content and stripped of links and mentions. A LAN probe that does not answer from the Pi no longer counts as proof of the block, for the guard and for every local container's startup check. `dom doctor` reports the last guard check. On by default (`[self_work] enabled`); inert without a local desktop host. Adds migration 0015 (`self_tasks`, schema 16), additive, so the previous release still runs on it ([docs/desktop.md](docs/desktop.md#self-directed-work)).
- Typing time now keeps scaling with a bubble's length up to 20 seconds (it capped at 8), and the "typing" indicator is re-sent every 4 seconds during a long wait so it does not lapse.
- Openers (Dom texting first in the owner's DM) are off by default: `[openers] enabled` now defaults to `false`, in the starter config too. An instance that sets it keeps its setting.
- `self_inspect` reports the public repository URL (`[update] repo_url`) with the running version, and the desktop worker is told it may `git clone` that repository in the sandbox to read code and history, read-only: no push, no commits, no credentials.
- New "Auto" mode for Everywhere presets: in the nightly reflection Dom picks the next day's preset from his mood and journal (never the assistant preset), with a reason, from his wake-up; a failed or invalid pick falls back to a weighted random one. He may switch once more a day with the `switch_vibe` tool when a conversation calls for it. Chat pins still win, `/preset` and the dashboard can set Auto or lock a preset, the Chats panel shows today's vibe and reason, and `self_inspect` and the prompt tell Dom which preset he picked ([docs/presets.md](docs/presets.md)).
- A second `dom deploy` refused because one is already running no longer overwrites the running deploy's saved state, so the post-restart report is not lost.
- T3 bridge: thread lookup accepts the numeric paging cursor the live T3 server returns, instead of failing with "Invalid T3 cursor."
- Every configured desktop host has a container from the start: remote hosts show on the Desktop page, home tile and capabilities (and open at `/desktop?container=<name>`) before first use, and `start_desktop_task` stays offered while any host is usable. Starting containers is still on demand.
- `dom doctor` warns about misspelt keys in every config table (now including `[presence]`, `[t3]` and `[update]`, derived from the config model) and fails when a remote desktop host's `api_key_env` secret is not set.
- Approving a skill proposal re-checks it against private notes as well as credentials, since the notes may have changed since the proposal.
- Fix the daily reflection running in the daytime when the learned sleep start is between 01:00 and 06:59: it now runs only in the hour before sleep starts.
- Single-attempt model calls (dashboard soul chat, reminder phrasing, desktop task reports, the asleep plugin grumble) now fall back to the fallback model, with one call, when the home endpoint is down.
- Public skills and plugin inputs are now screened against every configured credential, derived from the config (every `*_env` setting, including `KLIPY_API_KEY` and each `[[desktop.hosts]]` `api_key_env`).
- Dashboard: plugin panels no longer disappear from the Plugins page after approving, rejecting or disabling a plugin.
- Dashboard: `lan_address` proposals have a kind filter and a card (old → new address, where it is used, device name and MAC); Telegram "Approve all on this page" also applies skills and LAN address changes, each through its own checks.
- find_devices and approved address changes also cover `[desktop] url` and every `[[desktop.hosts]]` url; `dom desktop-check` accepts `<name>.local` as well as an IPv4 address.
- Normal reminders are phrased in Dom's own voice like critical ones, falling back to the stored text if the model fails; casual ones still fold into conversation.
- Fix the turn note cutting Dom's tool list mid-word in the owner DM: it now lists every enabled tool by name, and drops whole names with "+N more" only when they would not fit.
- Dashboard: show a Desktop tile on the home view with each container's host, state, truncated task brief, live viewer link and host limits, polling every 4 seconds when enabled.
- Dashboard: edit each chat's sliders and reset them to default.
- Dashboard: upload a `.txt` or `.md` export through the same import as `/import`; adds `python-multipart`.
- Dashboard: approve or ignore pending groups and stranger DMs.
- `gif` action: a KLIPY search result sent as an animation; set `KLIPY_API_KEY` ([docs/gifs.md](docs/gifs.md)).
- Skills work in chat: `skills_list`, `skill_view` and `skill_manage` (a proposal the owner approves) in every chat, with each skill's name and description in the prompt.
- Plugins can add Telegram slash commands and dashboard panels; declared surfaces are reviewed and re-approved like code (`docs/plugins.md`); while Dom is asleep they only run in the owner's DM, and he grumbles about being woken.
- `/update` (owner DM): shows the newest `vX.Y.Z` release tag of `[update] repo_url` with its changelog section and, on confirmation, deploys it in a detached `dom-update` unit. Dom reports the outcome after the restart.
- `dom deploy <home> vX.Y.Z` stages a tag, runs `uv sync --frozen --no-dev`, backs up, repoints `app` and restarts `dom.service`; if the new release is not ready within 120 seconds it rolls back to the previous one. After a successful deploy only the current and previous release folders are kept.
- `find_devices` (owner DM, read-only): LAN devices with names and addresses from mDNS and the neighbour table. A configured LAN host that moved becomes a config-change proposal (`lan_address`) for the owner to approve.
- `[desktop] lan_probes` accepts `<name>.local:port`; the names are resolved on the Docker host before every firewall probe and an unresolvable name fails closed.
- Run more than one desktop container. `start_desktop_task` takes an optional container name (letters, digits, hyphens); each container has its own task slot, browser profile, port and idle timer, and the default container behaves as before. Desktop hosts are a list (`[[desktop.hosts]]`, each with `max_containers` and `min_free_memory_mb`); the single `[desktop] mode` config still works as one host named `default`.
- Refuse, with the numbers, to start a container past a host's limit or that would leave it below its free-memory floor (local: `MemAvailable`; remote: read through the sandbox's shell). The Pi allows one container and keeps 512 MB free besides the container's 1600 MB.
- Handoff links, task reports, screenshot file names, the live viewer and the dashboard Desktop page name the container; the page lists every container with its host, state and task. `self_inspect` reports `desktop_hosts` with limits and containers.
- Migration `0014` adds `desktop_tasks.container` and makes the one-active-task limit per container.
- Add a pinned, loopback-only T3 Code systemd user service with a 600 MiB memory limit.
- T3 Code bridge (owner DM): Dom signs in to a T3 Code server over MCP with Supervised links and can read coding threads (`coding_overview`, `coding_search`, `coding_thread`). `coding_send` stages a message that is sent only after the owner confirms it with a button.
- Every bridge call is audited in a `t3_audit` table; `/t3` shows the sign-in, linked machines and audit history, with a Reconnect button.
- Desktop: `screenshot` saves the screen as a file Dom can send; a stale Chromium profile lock is cleared before the browser starts.
- Document Raspberry Pi desktop startup timings, the Chromium seccomp profile and the desktop connectivity check.
- Wait for the local sandbox API and browser within a configurable 180-second startup deadline, with a distinct error for each stage.
- Disable Jupyter for faster local desktop startup and run Python scripts through the sandbox Bash API.
- Ship a Docker seccomp profile that lets Chromium start its namespace sandbox on the Raspberry Pi.
- `/preset` and the dashboard Chats panel can set one preset for every chat ("Everywhere"); a chat's own preset still wins, and a chat can go back to "Same as everywhere". `self_inspect` reports it.
- Fix replies lost after a tool call: the home-lab model usually answered in prose once it had read tool results. Dom now repeats the answer format after every tool result, accepts a JSON answer wrapped in prose or a fence, and asks for a bad final answer once more without tools.
- Never drop a reply silently: when the retry also fails, the fallback model writes the final answer; if that fails too, Dom logs a warning with the turn's trace and, in the owner's DM, says it could not put a reply together.
- Reactions are limited to the emoji Telegram allows for bots. The prompt lists them, and any other emoji becomes no reaction instead of a rejected API call.
- The journal's daily mood comes from a fixed list of feelings and is never blank.
- New "seen" reaction: while a DM is online (never in groups), when Dom reads a batch and answers with nothing, he reacts 👀 to its newest message. It is never sent while he is asleep or offline, at most once per batch, and skipped in chats that refuse reactions. Turn it off with `presence.seen_reaction = false` in `config.toml`.

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
