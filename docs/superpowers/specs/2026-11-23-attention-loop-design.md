# Attention loop and cross-chat queue — design (v0.8.2 W2)

Status: approved by the owner 2026-11-23 (D1 yes; D2 widened, see §4) · docs only · implements E52, D1 and D2 of
`docs/superpowers/plans/2026-11-23-v0.8.2.md` (W2). Baseline: `docs/superpowers/specs/2026-11-19-dom-core-design.md`
and the v0.8.1 plan (E45 early start, restarts and follow-ups; E46 machinery rule). Checked against `dom/dispatch.py`,
`dom/prompt.py`, `dom/tools.py`, `dom/triage.py`, `dom/proposals.py`, `dom/presence.py` and `dom/window.py` as of
`v082-plan` (252f361).

## 1. What changes

Today every chat runs its own turn as soon as its debounce ends: two busy chats run two chat-model calls at the same
time, each turn knows nothing of the others, and nothing can pass between chats. W2 adds two pieces:

1. **The attention loop.** One gate around the part of a turn that uses the chat model. Dom thinks about one chat at a
   time, like flicking between apps, in an order the loop picks (owner DM first, then by age). Each visit leaves a short
   facts-only line, and the next turn's note says what he was just doing elsewhere.
2. **The cross-chat queue.** A persisted queue and one worker task that do every cross-chat action. A turn only adds an
   item; it never takes another chat's lock. There are three item kinds. Two go to the owner DM with no card, rate-limited (D2): an **alert** (code-made,
   when a group asks for the owner) and an **owner note** (Dom's own message to the owner, from any chat). The third, a
   **message** to any other chat, needs an owner approval card first (E52).

Goals, from W2's "done when": two chats active at once are answered in turn and neither starves; a group asking for the
owner pings the owner DM; a DM→group message goes out only after the owner approves its card.

## 2. The attention loop

### 2.1 What a visit is

A **visit** is the compose phase of one turn: from the first chat-model call to the final `Actions`, including tool
rounds, the Jev rescue and the leak guard (`_compose_turn` plus `_hold_for_leak_check`, called from `_run_turn`). The
loop holds one **visit token**, and only the holder may compose.

Outside the visit, and still per chat and parallel as today: ingest, `notify`, Jev arrival triage, reactions, the
owner-DM "On it." line, media descriptions, the debounce, presence and wake decisions, confident `ignore` or `react`
outcomes (they never call the chat model), and **sending**. The token is released once the reply is composed, so chat B
can compose while chat A's bubbles are being typed and sent. Sending still holds A's own dispatch slot.

The loop gates only chat-model compose: memory passes (already one at a time through `_memory_slot`), openers,
reminders, reflection, self-directed work, desktop reports and `/update` reports stay where they are (§10).

### 2.2 States

Loop: `idle` ⇄ `visiting(chat_id)`. A visit ends when the reply is composed, the attempt is cancelled (superseded) or
the attempt fails. The loop then picks the next waiting entry, or goes back to `idle`.

Entry: at most one per chat, because a chat runs one dispatch at a time (`_dispatch_slot`).
`waiting → visiting → done`, or `waiting → withdrawn` when the dispatch is cancelled or gives up before its visit.
An entry carries `chat_id`, `scope`, `tier`, `since` (the dispatch's `waits.start`, which a restart keeps) and how many
visits in a row this chat has had.

The queue lives in memory. After a restart, unread messages come back through the existing tick (`_poll_chats`, the
`ORPHAN_AGE` rule and catch-up), so nothing needs saving.

### 2.3 Scheduling (D1)

**Choice for D1: one loop, owner DM first** (recommended in the plan; §12 asks the owner to confirm). When the token
is free, the loop picks by these rules, in order:

1. **Never interrupt.** A running visit is never cancelled or paused for another chat. A new message in the same chat
   still restarts it (v0.8.1, unchanged).
2. **Resume.** A chat whose visit was superseded, and whose dispatch is back, goes first: its answer was already under
   way.
3. **Owner DM.** The owner DM goes next, always.
4. **Everyone else by age.** The pick is the largest effective wait. For `next` that is `now − since`; for `later` it is
   `now − since − 60 s`. A `later` chat therefore overtakes newer `next` chats after a minute, so none starves.
5. **Per-chat cap.** A non-owner chat that has had 2 visits in a row is skipped while any other non-owner chat waits.
   The owner DM has no cap, so that "first" stays true.

**Jev sorts, without a new call.** The tier comes from what the dispatch already knows when it reaches compose:

| Tier | When |
|---|---|
| `next` | Jev's arrival verdict is a confident `reply`; or the newest message mentions or replies to Dom (`_directly_addressed`); or a follow-up turn (an exchange already under way) |
| `later` | catch-up after coming online (an offline wake, a `kapoh` wake, the background tick); a verdict that was not confident or is missing (Jev down); bot-only turns and the bot exit line |

If the W1 view shows bad orderings, the next step is a "how soon" question added to the same `triage_online` call,
which costs no extra request. It is not built now.

**Room for a second lane.** All loop state sits in one `Attention` object (`dom/attention.py`). A later fast lane
(owner DM and urgent) would be a second instance with its own token and an entry filter. The lanes would share the
system prefix but never one transcript. No lane setting ships in v0.8.2.

### 2.4 How it fits v0.8.1's early start, restarts and follow-ups

| v0.8.1 behaviour | With the loop |
|---|---|
| Debounce, and the Jev "sender done" early start | Unchanged and per chat. When it ends, the dispatch runs as today. It meets the loop only when it is about to compose; if the token is free, it adds no delay. |
| Arrival typing indicator | Starts on arrival only when the loop is idle or already visiting this chat. Otherwise it starts when the chat's visit starts, so a chat waiting behind another does not show "typing…" for the whole wait. Reactions and "On it." stay on arrival. |
| Restart: a message before the first bubble (`notify` cancels the attempt, `MAX_RESTARTS = 2`) | Unchanged. If the cancelled attempt held the token, the token is released and the restarted dispatch re-enters as `resume`. If it was still waiting, its entry is withdrawn and re-enters with the same `since`. This case does not count towards `MAX_RESTARTS`, because nothing was composed. |
| Restart while the composed reply is being typed (token already released) | Re-enters as `resume`. Interrupted tool results and typing credit carry over as today (`_interrupted`). |
| Follow-up: a message after the first bubble (`attempt.later`, then `_followups`) | A new dispatch and a new entry, tier `next`. It counts towards the per-chat cap. |
| `_dispatch_slot` and `app.locks[chat_id]` | Unchanged. A chat holds its own slot while it waits for the token. The loop never takes a chat lock. |
| Jev after reply (`_stay_check`), the memory pass | Outside the loop, unchanged. |
| Spend cap (`ModelCallSkipped`) | Checked before the visit as today. A skipped turn never takes the token. |

The lock order is: dispatch slot, then visit token. Nothing that holds the token waits for a chat slot, so there is no
cycle.

## 3. The "elsewhere" note

**What goes in.** At the end of every group or friend-DM visit, the model adds one fact line about what it just did.
The instruction is in that chat's turn note: *"Also add `"elsewhere"`: one short line of plain facts about what you did
here, for your own memory. No quotes, no names of people outside this chat."* `Actions` gains an optional
`elsewhere: str | None`. `FRAME` stays as it is, so the shared system message does not change. The line costs about 30
output tokens and no extra call. Owner-DM visits write no line, because nothing from the owner DM goes anywhere else.

**Filter, in code.** A line is dropped, not repaired, when either of these is true:
- it shares a run of 6 or more consecutive words (case-folded, punctuation stripped) with any of the last 20 window
  messages, which catches quoting;
- it contains a quoted span of 4 or more words.

A line is cut to 120 characters. It is stored in memory with `chat_id`, `scope`, the chat title and the time.

**Who sees what**, as a line at the very end of the turn note:

| Target turn | Note |
|---|---|
| owner DM | Up to the 3 most recent lines from other chats, each at most 30 minutes old: `- 4 min ago in "Title" (group): <fact>`. |
| group or friend DM | Content-free, and only if the last visit was to another chat within 10 minutes: `you were just in another conversation`. Nothing from any other chat. |

The table follows E52 and the core spec's scope rules. Nothing from `private/` or the owner DM can reach another chat,
because owner-DM visits write no line. Group and friend facts reach only the owner DM, which can already search every
chat (`search_history`). The note is context, not a message. Other chats get no content, so one chat's style cannot
bleed into another.

**Expiry and size.** A line expires after 30 minutes. The note is at most 3 lines, about 400 characters. A restart
drops all lines, which is fine for "what I was just doing".

**As the base for messages.** When the owner asks "tell the group what they decided in the other one", Dom drafts a
`message_chat` text from these lines (and `search_history`). It is still a message carrying one chat's content into
another, so it gets a card (§4.3).

## 4. The cross-chat queue

### 4.1 Item kinds

| Kind | Direction | Text | Card |
|---|---|---|---|
| `alert` | group → owner DM | A code template, one line: `👋 <sender name> is asking for you in "<group title>"`, with a link to the message when Telegram allows one. Never the message text. | No (D2), rate-limited |
| `owner_note` | group or friend DM → owner DM | Dom's own words, at most 1,000 characters, text only, sent as an ordinary bubble from Dom in the owner DM | No (D2), rate-limited |
| `message` | owner DM → group or friend DM; group → group content only through the owner DM | The exact text Dom drafted, at most 1,000 characters, text only | Yes, always |

The owner's D2 answer: "if Dom gets anything that needs my attention he can send anything to me". So anything bound for
the owner DM is free; anything bound anywhere else needs a card. Group-initiated sends to other chats, media and
forwarding are not in v0.8.2 (§10).

### 4.2 Who may add items

- **Alerts: code only.** The Telegram adapter adds `mentions_owner` to `IncomingMessage` when a group message has a
  `text_mention` of the owner's id, mentions the owner's username, or replies to one of the owner's messages. It is
  never set for bot senders or unapproved chats. On ingest, a message with that flag adds an alert, subject to §4.5. No
  model is involved, so this works while Dom is asleep or offline in that group, and under the spend cap.
- **Messages: the owner-DM model only**, through a new owner-only tool, `message_chat(chat, text, reason)`. `chat` is an
  approved chat's id or title. An ambiguous or unknown title returns the matching approved chats so the model can choose.
  The tool is owner-only, so `enabled_tools` puts it after the shared tools and it changes only the owner DM's tool
  list, once. The tool result reads: *"Waiting for the owner's OK on a card; it goes out once approved."*
- **Owner notes: group and friend-DM models**, through `tell_owner(text, reason)`. It exists only in those turns' tool
  lists (added last, so each scope's tool list stays fixed). The tool description: use it when something there needs the
  owner's attention, write it as Dom texting the owner, and don't use it to repeat what an alert already said. Over a
  rate limit (§4.5) it returns a plain error and nothing is queued. The result reads: *"Sent to the owner."* once queued.

### 4.3 Direction rules (E52, D2)

| From → to | Allowed? | Card |
|---|---|---|
| group → owner DM, alert | yes | free, rate-limited |
| owner DM → group | yes | **always.** The card shows the exact text. Approving it is the owner publishing that text, which is how this spec reads E52's "nothing from the owner DM goes into a group": never without the owner approving the exact words. |
| owner DM → friend DM | yes | always |
| group → group (content from A, drafted in the owner DM) | yes | always |
| group or friend → owner DM, owner note | yes | free, rate-limited (D2) |
| anything → a pending, ignored or unknown chat | no | — |

**Leak checks when the card is made**, all before any card exists:
- `check_local_private_text` refuses any text that contains a line of `private/` notes verbatim or an instance
  credential, and the tool returns that error;
- for a group target, Jev `leak_score` runs against `guard.private_terms` and the score is shown on the card. If Jev is
  down, the card says "leak check unavailable". The owner still decides.

### 4.4 The card

The card reuses `proposals` with a new kind, `cross_chat`. Its body is `{item_id, source_chat_id, target_chat_id,
target_title, text, reason, leak_score}`. The card shows the target, the exact text, the reason, the leak score and
"expires in 2 h". It has Approve and Reject buttons, on Telegram and on the dashboard's approvals.

- It is left out of "Approve all", like `memory_edit`.
- `_apply` for `cross_chat` sends nothing; approval only records the decision. Then `Proposals.decide` calls
  `cross_chat.approved(item_id)`, which moves the item to `queued` and wakes the worker. If the process dies between the
  two steps, startup reconciliation (§7) repairs it.
- **Expiry: 2 hours.** "Tell them I'm late" is wrong a day later. The worker refuses an item whose card is older than
  2 h, marks it `dropped (expired)` and settles the card. The 14-day sweep in `Proposals.expire` stays for the other kinds.

### 4.5 Rate limits

- **Alerts:** at most 1 per group every 15 minutes, 1 per (group, asker) per hour, and 6 an hour in all. No alert when
  the owner has posted in that group in the last 15 minutes. During Dom's sleep hours an alert goes out silently
  (`disable_notification`).
- **Owner notes:** at most 1 per source chat every 10 minutes and 6 an hour in all, counted with alerts so a busy group
  cannot flood the owner DM. During Dom's sleep hours a note goes out silently (`disable_notification`).
- **Messages:** at most 3 pending cards, which refuses a fourth `message_chat` with a plain tool error. At most 10 sends
  a day, which refuses at card creation. One target per call.

### 4.6 The worker and locking

There is one worker task (`dom/cross_chat.py`), started with the dispatcher. It drains `queued` items oldest first.
Each send:

1. Takes **only the target chat's dispatch slot**, through the same ordering as `_dispatch_slot` (split out as
   `Dispatcher.chat_slot(chat_id)`), so it never interleaves with a turn there. It waits for a running turn in that chat
   to finish.
2. Sets the item to `sending` and commits.
3. Sends one bubble with `executor.perform`: human-like typing and an ordinary send, not a reply. The bubble is recorded
   in the target's history as Dom's own message. No event in the target chat names where it came from.
4. Sets `sent` with the Telegram message id. A `message` item also gets an owner event in the source chat: `Sent to
   "<title>": <first 80 characters>`.

The invariants:
- A turn never takes another chat's slot or lock. It only inserts a row.
- The worker holds at most one chat slot at a time and never the visit token. It never runs while a source turn holds
  its own slot on the worker's behalf, because the source turn has already returned when it adds an item.
- No code path holds two chat slots, except `migrate_chat`, which already orders them by id and is unaffected.
- A migrated target (`migrate_to_chat_id`) is rewritten through `migrate_chat` like every other chat-keyed row.

### 4.7 Audit

The `cross_chat` table is the audit trail. It has one row per item with every status change time, and rows are kept
30 days (the turn-log retention job prunes them). Owner events record: card created, approved or rejected (existing
proposal events), sent, and dropped with its reason (expired, refused, chat gone, may not have been sent). Every alert
sent is also an owner event. The alert itself lands in the owner DM and is recorded there as an event, not as Dom's own
words.

## 5. Dom never states machinery (E46)

- The note's wording is in-life: "you were just in another conversation", "4 min ago in "Title": …". It never says
  loop, queue, visit, turn or timing. Dom can use it as an in-life reason ("sorry, was talking to someone").
- An owner note is Dom texting the owner in his own voice, so `MACHINERY_RULE` applies to it as to any reply. It is
  recorded in the owner DM's history as Dom's own message, so the owner can answer it there and the owner-DM turn has
  the context.
- Alerts and card text are system notices, like "🔎 Checked your coding work" and the `/update` report. They are not
  in Dom's voice, so E46's exemption for system messages applies.
- The `message_chat` result lets Dom say "I'll send it once you OK it" in the owner DM. That talks about the owner's
  own control, not Dom's internals, and the existing owner-DM exception in `MACHINERY_RULE` covers it. The tool
  description tells Dom to write the text as he would text that chat himself, without "the owner asked me to" unless
  that is what the owner wants said.
- `FRAME` and `MACHINERY_RULE` do not change.

## 6. Spend cap and usage attribution

- The loop adds no model calls and makes no call twice. The `elsewhere` line rides on the turn's last call, attributed
  to that chat as `chat`.
- An alert uses no model, so it still goes out when the spend cap is reached.
- `message_chat` runs inside an owner-DM turn, so its tokens are the owner DM's. The card's leak check is a `guard` call
  attributed to the **source** chat (the owner DM) and checked with `model_allowed(source)`. If the cap is reached the
  check is skipped, and the card says the leak check is unavailable.
- Sending an approved item costs nothing.

## 7. Failure and restart

Item statuses: `awaiting_approval → queued → sending → sent`, or `→ dropped(reason)` from any status before `sent`.

| At restart the item was | Then |
|---|---|
| `awaiting_approval`, card approved (crash between the two steps) | moved to `queued` |
| `awaiting_approval`, card rejected or expired | `dropped` |
| `awaiting_approval`, card still pending | unchanged; the card still works |
| `queued` | sent by the worker, unless past its 2 h expiry |
| `sending` | **never resent.** Set to `dropped (may not have been sent)`, with an owner event naming the target and the first words, so the owner can check and ask again. Nothing is sent twice. |

- **A send that raises.** Retries before the request (flood waits, connection refused) stay inside
  `retry_telegram`/`perform`. Any other error after `sending` drops the item with an owner event, with no automatic
  retry. A gone or forbidden chat drops with "chat gone".
- **A crashed worker** is restarted by the dispatcher's tick, like `_heartbeat`. Items stay in the table, so none are lost.
- **The loop itself** has no persisted state. A visit that raises releases the token in a `finally`. The existing
  per-chat failure counter (`MAX_TURN_FAILURES`) still gives up on a chat that keeps failing, and a stuck chat cannot
  hold the token past its own turn.
- **Shutdown** (`Dispatcher.close`): the worker finishes a send in flight (the same rule that protects committed
  attempts today) and stops before taking the next item.

## 8. Cache layout and eviction

The layout stays:

| Part | Shared by |
|---|---|
| system message (FRAME, SOUL, MEMORY, HABITS, skills) | every chat |
| tool list | one scope (`message_chat` added last in the owner DM; `tell_owner` added last in groups and friend DMs) |
| user message: persona, notes, window, then the turn note, with the elsewhere note last | one chat |

Chats are never merged into one transcript.

**Measure eviction, don't assume it.** vLLM evicts least-recently-used blocks, so a chat's cached window may be gone
when the loop comes back to it. The first model call of each visit records, in its `model_calls` entry (JSON, so no
migration):
- `cached_tokens` and `prompt_tokens`, as today;
- `gap_ms`: the time since this chat's previous chat-model call;
- `between_tokens`: the prompt tokens of other chats' calls since then.

The W1 view (§9) shows the cache hit rate on return visits by gap. If returns after short gaps miss, the cause is ours
(W1's prefix-hash work). If only long gaps or large `between_tokens` miss, that is eviction. This evidence decides
whether to tune vLLM's cache size or build the second lane.

## 9. What the W1 timing view shows

- A new stage, `queue_wait` ("Waiting for another chat"): from the moment the dispatch is ready to compose to the start
  of its visit. Each turn row gets a step for why it was picked (`resume`, `owner`, `next`, `later`, `aged`, or another
  chat skipped by the cap) and which chat it waited behind. `lock_wait` keeps meaning a wait within the same chat.
- **A loop strip**: the last 50 visits on a time axis, one bar per visit (chat, compose time), with the queue depth at
  each pick. It shows at a glance whether two chats took turns and whether either starved.
- **Cache on return**: the hit rate (`cached_tokens / prompt_tokens`) of first calls, grouped by `gap_ms` (under 1 min,
  1–5 min, 5–15 min, over 15 min), with the median `between_tokens` for each group.
- **Cross-chat**: the last 20 items with kind, source, target, status and times. The text is shown only on the
  owner-only dashboard, which is already behind Cloudflare Access.

## 10. Out of scope

- A second lane. There is room for one (§2.3), but it is not built.
- Merging chats into one transcript or one prompt.
- Putting memory passes, openers, reminders, reflection, self-directed work and desktop or `/update` reports into the
  loop. They keep their own locks. Moving the memory pass in as a `later` visit is a candidate after W2's timings.
- Cross-chat tools in group or friend-DM turns other than `tell_owner`; rewriting a
  message in the target chat's voice; media, files and forwards.
- Saving the note across restarts.
- Pausing a visit for the owner DM.

## 11. Migration

Migration `0022_cross_chat.sql` (`user_version` 22 → 23; W6 took 0021), in two parts.

**The new table:**
```sql
CREATE TABLE cross_chat (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('alert', 'owner_note', 'message')),
    source_chat_id INTEGER NOT NULL,
    target_chat_id INTEGER NOT NULL,
    sender_user_id INTEGER,            -- the asker, for an alert's rate limit
    text TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('awaiting_approval', 'queued', 'sending', 'sent', 'dropped')),
    drop_reason TEXT,
    proposal_id INTEGER REFERENCES proposals(id),
    sent_msg_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX cross_chat_status ON cross_chat(status, created_at);
```

**The `proposals` rebuild**, so that its `kind` CHECK allows `cross_chat`. The rebuild follows the same pattern as
0019.

**Rollback.** The migration only adds things. An older release ignores `cross_chat`, reads `cross_chat` proposals
(it shows their title) and refuses to apply them ("This proposal kind is unavailable"), so no message can go out from
an older build. Items in `queued` or `sending` would wait until the release is reinstalled, and then §7 applies. No data
is removed.

Mixed versions cannot happen: one process owns the database.

## 12. Test plan

Unit tests with the fake clock and the fake bot, as in the dispatch tests:

- **Scheduling:**
  - owner DM before an older group;
  - `resume` before the owner DM;
  - a `later` chat overtakes a newer `next` chat after 60 s;
  - the cap of 2 in a row yields to a waiting chat, but not to nothing, and never to the owner DM;
  - a running visit is never cancelled by another chat's message;
  - two chats flooding for 10 simulated minutes both get visits, with no wait over a set bound.
- **Visit boundary:**
  - the token is released before `executor.perform`, so chat B composes while chat A's bubbles type;
  - a visit that raises frees the token;
  - a spend-capped turn never takes it.
- **v0.8.1 interplay:**
  - a restart inside a visit re-enters as `resume`;
  - a supersede while waiting keeps `since` and does not count towards `MAX_RESTARTS`;
  - a follow-up becomes a new `next` entry;
  - arrival typing is deferred while another chat is visited, and starts at the visit;
  - the early start adds no delay when the loop is idle.
- **Note:**
  - a group fact shows in the owner DM note;
  - no group or friend note ever has another chat's content (a property test over random visit sequences);
  - the owner DM writes no line;
  - the quote filter (6-word run, quoted span);
  - 120-character cut, 3 lines, 30-minute expiry, a restart drops them;
  - the system message and the tool list stay byte-identical with and without a note.
- **Cross-chat:**
  - an alert from mention, `text_mention` and a reply to the owner; none for bots, unapproved chats or a recent owner
    post; every rate limit;
  - `message_chat` exists only in the owner DM's tool list, last; `tell_owner` only in group and friend-DM lists, last;
  - an owner note goes out with no card, is recorded as Dom's message in the owner DM, and every rate limit refuses with
    nothing queued;
  - the card is created, Approve sends exactly once, and Reject or expiry never sends;
  - text with private content is refused before any card;
  - "Approve all" skips these cards;
  - the worker waits for a running turn in the target chat;
  - an instrumented slot registry asserts no task ever holds two chat slots and no turn takes another chat's slot.
- **Restart:** every row of the §7 table, including a crash between approval and `queued`, and that `sending` is never
  resent.
- **Migration:** a version 22 database with proposals of every kind migrates to 23 unchanged; an older build reads the
  migrated `proposals` table.
- **E46:** a replay over the turn notes and tool results finds none of loop, queue, visit, card or timing in group or
  friend scopes.

Live tests on Dominic Dev (W8), each row recording what was live and what was tests only:
- two busy chats at once, checked on the loop strip;
- someone in a test group asking for the owner;
- a DM→group message through its card, approved once and rejected once;
- the cache-on-return table, after an hour of mixed use.

## 13. Decisions (the owner approved this spec, W2-a to W2-e included, on 2026-11-23)

| # | Question | Recommendation |
|---|---|---|
| D1 | One loop, or a fast lane from the start? | One loop, with the owner DM always first, and no cap on the owner DM. Decide on a second lane from the cache-on-return and `queue_wait` numbers. **Decided: yes.** |
| W2-a | Where does a visit end? | When the reply is composed, so the next chat thinks while this one's bubbles type. Holding the token through sending would make every waiting chat wait for typing too (10–16 s median). |
| W2-b | Can a group that has waited long go ahead of the owner DM? | No: the owner DM is always first, as decided. Starvation is bounded because the owner DM queues only when the owner has written. Alternative: a chat waiting over 2 minutes gets one visit ahead of the owner DM. |
| D2 | Which alert is free, and what triggers it? | **Decided (owner, 2026-11-23):** anything to the owner DM is free. The code-made alert when a group asks for the owner stays, and Dom may send his own owner note from any chat (§4.1), both rate-limited. Everything sent anywhere else needs a card. |
| W2-c | E52 says nothing from the owner DM ever goes into a group. Is a DM→group message the owner approves word for word allowed? | Yes, and only like that. Text that contains `private/` notes verbatim is refused before any card; Jev's leak score is shown on the card for paraphrases. |
| W2-d | Facts from groups and friends' DMs reach the owner DM's prompt as context, with no card. | Yes: it is context, not a message, and the owner DM can already search every chat. |
| W2-e | Card expiry and limits | 2 h expiry; 3 pending cards; 10 sends a day; alerts 1 per group per 15 min and 6 an hour. |
