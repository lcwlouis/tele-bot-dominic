# Dashboard chat and the debug tracer (v0.8.5 W2, R11, E102)

The owner talks to Dom from the dashboard in the same conversation as the owner DM. A Trace toggle turns that
conversation into a full per-turn audit. Every turn in every chat is traced (E102), and the Turn log links each row
to its trace.

## Where the chat lives

The chat gets its own page, **Talk** (`/app/talk`), as the second destination after Home. It doesn't go on Home for three reasons:

- A conversation needs the full height of the screen, with the composer pinned to the bottom. Home is a summary page
  of cards that scrolls.
- The page polls every second while Dom is typing. Home's cards shouldn't refresh that often.
- The Trace toggle and the open turn live in the address (`?trace=1&turn=<id>`), so a Turn log link lands on that
  turn's audit.

On a desktop the side panel sits to the right of the conversation. It shows Dom's profile and presence, the approvals
waiting and what he's working on (his desktop task and own work). On a phone the panel folds into a sheet, opened from
the header. The panel reads `/api/home`, which already gathers this, so nothing is computed twice.

## How a dashboard message enters the owner DM

`POST /api/talk/send` takes `{text}`, or `{text, photo}` where the photo is base64 and at most 8 MB. It runs
`dom.talk.send_from_dashboard`:

1. It takes the owner DM's ingest lock (`app.ingest_locks[owner_id]`), the same lock Telegram ingress uses. A
   dashboard message and a Telegram message therefore record in a strict order.
2. The bot posts a silent mirror bubble into the owner DM: `🖥 <text>`, or the photo with that caption. The
   bot needs this for three reasons:
   - The message needs a real Telegram `message_id`. The window, read marks, reply-to and restarts all order by it, and a made-up id
     would collide with Telegram's next one.
   - The Telegram side reads as one conversation: Dom's answer follows the words it answers.
   - Bots never receive updates for their own messages, so the mirror is never ingested twice.
3. It builds an `IncomingMessage` with the owner as the sender, the mirror's `message_id` and date, and any photo's
   media. It then records it through the same `_record` Telegram ingress uses (`resolve_person`,
   `record_new_message`, `describe_media` and `dispatch.notify`). From there the turn is an ordinary owner-DM turn:
   debounce, Jev triage, attention, the agent loop, the guards and delivery.
4. The stored row has `origin = 'dashboard'` (migration 0033). The dashboard shows a small 🖥 glyph on it. The prompt
   is unchanged: to Dom it's the owner talking.

Messages starting with `/` are refused with "Commands work in Telegram". Commands are parsed in the Telegram handler,
and a command can't safely half-run from the web.

## How replies reach both places

Dom's replies are already sent to Telegram and recorded as assistant rows in `messages`. The dashboard **polls**
`GET /api/talk?after=<row id>`:

- every second while a turn runs in the owner DM (`dispatch.turn_running(owner_id)`, shown as "typing…");
- every 4 seconds otherwise;
- not at all while the tab is hidden (the poll restarts on `visibilitychange`).

Bubbles arrive one by one as the executor records them, so the view streams at bubble granularity. Dom answers in
JSON actions, so tokens never stream.

Why polling and not SSE:

- **CSP:** both fit `default-src 'self'`.
- **Auth:** the local dev token is a header, and `EventSource` can't send one.
- **Server load:** a long-lived response would hold a task in the in-process Uvicorn server through `/update`
  restarts.
- **Cost:** a poll with a cursor is one indexed `SELECT`.

Media from Dom shows inline from the existing kept-media route (`/api/media/files/<chat>/<msg>`). The owner's own
photos show their description.

## The trace record

Traces are stored in their own SQLite file, `<home>/traces.db`. It isn't part of `state.db`:

- **No contention:** the store serialises every call through one lock, so trace writes there would queue behind the
  reply path. A separate connection never does.
- **Easy upkeep:** eviction and `VACUUM` touch only debug data, and backups and memory exports stay small.

The file belongs to `dom/tracer.py` (`CREATE TABLE IF NOT EXISTS`, `PRAGMA user_version = 1`). It isn't a numbered
migration, because nothing in `state.db` refers to it.

```sql
traces(id INTEGER PRIMARY KEY, turn_id INTEGER, chat_id TEXT NOT NULL, scope TEXT NOT NULL,
       kind TEXT NOT NULL,          -- turn | after | background
       label TEXT NOT NULL,         -- "turn", "stay check", "studio", "memory pass", "opener"
       at TEXT NOT NULL, outcome TEXT NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL)  -- zlib JSON
-- indexes: (turn_id), (chat_id, at), (at)
```

A turn has one `turn` part. Work that finishes after its row was saved adds `after` parts with the same `turn_id`:
the staying-online check, and studio jobs with their look-before-send and provenance. Work that isn't tied to a turn,
like the memory pass, is a `background` part, found by chat and time.

The body (version 1):

```json
{"version": 1, "turn_id": 12, "chat_id": "…", "scope": "owner_dm", "kind": "turn", "label": "turn",
 "started_at": "…", "ended_at": "…", "duration_ms": 4210, "outcome": "Replied", "model": "…", "fallback": false,
 "steps": ["…"], "timings": [{"what": "model", "ms": 2100}], "model_calls": [{"kind": "chat", "system_hash": "…"}],
 "events": [{"at": "…", "t_ms": 12, "type": "model_call", "...": "..."}],
 "redactions": 0, "media_stripped": 1, "truncated": false, "dropped_events": 0}
```

| Event `type` | Fields |
|---|---|
| `arrival` | presence state and `until`, catch-up, unread count and total, the newest message's id and time, queue waits (debounce, media, lock, away) |
| `triage` | Jev's full verdict (action, confidence, done, skill, emoji), urgency or offline verdict, skills offered |
| `prompt` | the prompt fingerprint, its message count, the tool names offered, the scope, and the prompt-cache state (tokens measured, whether a trim happened) |
| `model_call` | kind, model, host, URL path, fallback, status, duration, input, output and cached tokens, cost, **the full request body** and **the full response JSON** (reasoning included when the provider returns it), or the error |
| `tool` | name, raw arguments, result, whether it was refused or not offered ("image is not a tool"), duration |
| `actions` | the final actions object the model returned |
| `leak_check` | the leak score, the cutoff, and whether it held the reply |
| `look` | the look-before-send outcome: kept, regenerated or kept the redo |
| `delivery` | each bubble or media item as sent (kind, message id, text) and private-reply fallbacks |
| `reaction` | the emoji and whether Telegram took it |
| `memory_op` | each memory-pass op (file, op, text, entities) and what happened to it |
| `note` | anything else a stage wants to say, as `{text}` |

The steps and timings already in the Turn log, including the attention visits, come along too, so one record holds
everything.

## Where each field is captured

- `turn_log.TurnTrace.events` is a plain list on the trace already carried in a context variable.
  `turn_log.event(type, **fields)` appends a dict to it and does nothing when there's no trace. It is capped at 2,000
  events a trace; past that it only counts `dropped_events`.
- **Arrival and triage:** `Dispatcher._dispatch` (presence, waits), `Dispatcher._triage_arrival` and `_log_triage` (the
  verdict). Arrival and interrupted-attempt events merge into the turn's trace alongside the steps they merge today.
- **Prompt:** `agent.run_turn`, next to the existing fingerprint log line.
- **Model calls:** `turn_log.model_call` takes the request `body` and `url`. `ModelCall.received` keeps the raw
  response object in a non-field attribute. On exit the context manager adds one `model_call` event. This covers
  every client that uses it: chat, escalation, media, search and memory through `ChatClient._post`, and Jev triage
  and the leak guard through `JevClient.decide`. Kinds outside the Turn log's set (memory, summaries) are traced but
  not added to the turn row, as before.
- **Tools:** `agent.execute_tool_call`.
- **Final actions:** `agent.run_turn`'s `succeed`.
- **Output checks:** `Dispatcher._hold_text_for_leak_check` (`leak_check`) and `executor._look_before_send` (`look`).
- **Delivery:** `turn_log.bubble_sent(...)`, which every send site in the executor already calls, now takes the kind,
  message id and text. `executor.react` adds `reaction`.
- **After the turn:** `turn_log.follow_up(label, chat_id, scope)` opens a child trace for work spawned from a turn. On
  exit it's submitted as an `after` part of the parent turn. The parent's row id is read when the part is written, so a
  studio job that outlives the turn still lands on it. `_stay_check` and the studio delivery task use it. The memory
  pass opens a `background` part, and its ops are added in `MemoryPass._apply`. Openers go through
  `turn_log.capture`, so they are traced like any turn.

## Off the reply path

- Capture on the reply path only appends dicts and keeps references:
  - the request body (the messages list is shallow-copied, because the agent loop appends to it);
  - the httpx response object.

  Nothing is serialised, hashed or copied deeply.
- `turn_log.capture` saves the turn row as today, then calls `tracer.submit(trace)`. That is `put_nowait` into a
  bounded queue of 32 traces. When the queue is full the trace is dropped, and the drop is logged and counted, so
  memory can't grow without bound. A trace is **sealed** when submitted, so events from spawned tasks that run on
  under its context are ignored and nothing mutates a list that's being serialised.
- One writer task drains the queue. Each trace is encoded in `asyncio.to_thread`, in five steps:
  1. parse the responses;
  2. strip media;
  3. dump the JSON;
  4. redact;
  5. zlib-compress.

  The insert then runs on the trace database's own connection.
- `tests/test_tracer.py::test_tracing_adds_no_latency_to_the_reply_path` measures one full turn with tracing on and
  off over the same fake provider, and asserts the difference stays under 5 ms a turn. It also checks that `submit`
  returns before encoding starts.

## Retention and size cap

`[tracer]` in `config.toml`:

- `retention_days` is 7 by default, from 1 to 90. A daily job deletes older traces.
- `max_mb` is 256 by default, from 16 to 10,000. After each insert the writer keeps a running total and deletes the
  oldest traces until the total is under the cap.
- A single trace over 8 MB (compressed) is shrunk: strings longer than 64 kB are cut and the trace is marked
  `truncated`. A trace that's still over 8 MB is replaced by a stub that keeps its events without bodies.

Forgetting a chat with its history (`/api/chats/forget`) also deletes its traces. A deleted Telegram message stays in
traces until retention removes it, which is noted in docs/privacy.md.

## Redaction

Redaction happens in the encoder, before anything reaches the disk:

1. Media is stripped first. A base64 `data:` URL, or any base64-only string of 4 kB or more, becomes
   `[media image/jpeg, 341 kB, sha256 1a2b3c4d]`. Media isn't a secret, but it would swamp the cap.
2. Each of the instance's actual secret values (`model_lists.secret_values(config)`: every key variable config.toml
   names, plus the Telegram bot token) is replaced by `[redacted]`. The JSON-escaped form is replaced too. Longer
   values go first, so a value that contains another is never left half-redacted.
3. The shapes in `dom/secret_patterns.py` (`API_KEY`, `BEARER_TOKEN`, `TELEGRAM_BOT_TOKEN`) are replaced the same way.
   This catches a key Dom read from a page or a tool result.
4. Request headers are never captured. The `Authorization` header isn't in the body.

`redactions` counts the replacements, so the viewer can say "2 values redacted".

## Export

- `GET /api/traces/turn/<id>/export` returns every part of one turn as a JSON attachment.
- `GET /api/traces/export?chat=<id>&from=<iso>&to=<iso>` returns the parts in a time range, oldest first, with at
  most 500 parts. When more matched, the response says `truncated: true`.

Both are owner-dashboard GETs behind the usual auth and `fetch_site_ok` check, so another site can't read them.

## UI

- **Talk:** bubbles in Quiet Orbit's style (Dom's on the left, the owner's on the right, 🖥 on dashboard-sent
  ones), date separators, a "typing…" row and a composer with a photo attach button. Enter sends, Shift+Enter adds a
  line.
- **Trace toggle** (in the header): after each of Dom's replies, the turn that produced it appears as an expandable
  audit card, with sections in the order of the W2 list:
  1. Arrival and triage
  2. Prompt
  3. Model calls
  4. Tools
  5. Output checks
  6. Delivery
  7. After the turn

  Request and response bodies are folded `<details>` blocks with JSON highlighted by `$lib/highlight` and a copy
  button. Each turn has an export button.
- **Turn log:** each turn's details get an "Open trace" link to `/app/talk?trace=1&turn=<id>` for the owner DM's
  turns. For other chats it goes to `/app/system/turns/<id>`, a page that shows the same `TraceView` alone.
