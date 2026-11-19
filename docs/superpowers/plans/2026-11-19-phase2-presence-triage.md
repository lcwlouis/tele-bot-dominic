# Phase 2 — Presence and triage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Dom behaves like a person with a rhythm:
- Each chat has its own state: asleep, offline or online.
- He wakes for floods of messages, for "kapoh" moments, or when the owner says `/wake`.
- While online, Jev decides reply, react or ignore.
- He talks to other bots with a deterministic stop.
- He understands photos, GIFs, videos, voice notes and stickers.

**Architecture:**
- **Presence:** `presence.py` is a pure state machine over the existing `chats` columns. Its inputs are habits from `memory/HABITS.md` and an injectable clock and RNG.
- **Triage:** `jev.py` calls OpenRouter's Decisions API. `triage.py` turns one Jev call into a `Triage` result.
- **Dispatch:** `dispatch.py` replaces Phase 1's `should_respond`. It debounces each chat, runs presence, then triage, then the agent turn.
- **Media:** `media.py` describes media once, when it arrives, and the description is stored with the message.

**Spec:** `docs/superpowers/specs/2026-11-19-dom-core-design.md`, §3, §6 and §7. The Phase 2 row is in §14.

## Global Constraints

- All Phase 1 Global Constraints still apply, including no real IDs in code or tests and no network calls in tests.
- **Jev endpoint:**
  - `POST https://openrouter.ai/api/alpha/decisions` with `model = "~typesafe/jev-latest"`, from config `[models.triage]`.
  - It is **not** chat completions. Never substitute a chat model under Jev's name.
- **Verified Jev response shape** (real call, 2026-11-19):
  - `answers.<key>.type` is `choice` or `score`.
  - A `choice` answer has `choice`, `probabilities{}` and `confidence`.
  - A `score` answer has `score` (float, 0..N-1), `probabilities{"0":..}` and `confidence`.
  - A refused question comes back as `{"type":"refusal"}`.
  - Latency was about 0.5 s and cost about $0.00002 per call.
- **Media model:** `[models.media]` with `model = "qwen/qwen3.8-omni-flash"` on OpenRouter. It accepts text, image, audio and video.
- **Defaults**, each overridable by a line in `memory/HABITS.md`:

  | Setting | Default |
  |---|---|
  | `sleep` | `23:59-07:30` (owner timezone from config) |
  | `online_minutes` | `5-25` |
  | `offline_minutes` | `15-90` |
  | `wake_after_messages` | `5` |
  | `kapoh_wake` | `2.0` (the Jev `score` on the 0..3 kapoh scale) |

  Sleep length is clamped to 4–11 h. A malformed line is ignored and its default kept.
- **Bot-to-bot limits:**

  | Limit | Value |
  |---|---|
  | Consecutive bot↔bot turns in a chat (`chats.bot_depth`) | 6 |
  | Replies to the same bot | at most 1 per 10 s |
  | Identical text from the same bot | ignored within 60 s |

## Review Focus

1. **A sleeping chat that gets 200 messages** makes no model calls and no Jev calls. The messages are stored, and on waking the catch-up is **one** turn. *(Task 3 test `test_asleep_flood_no_calls_single_catchup`.)*
2. **Jev is down** (timeout or 5xx) or returns a refusal. Dom still works: online chats fall back to the main model deciding through `no_response`, and offline chats don't wake on kapoh. *(Task 3 test `test_jev_unavailable_falls_back`.)*
3. **Two bots in an endless loop.** After 6 consecutive bot turns Dom sends exactly one exit turn. After that he makes zero model calls for bot messages until a human speaks. *(Task 4 test `test_bot_loop_stops_after_depth`.)*
4. **Media over the 20 MB Bot API download limit**, or a failed media-model call: the message is still recorded with `media.kind` and `description = null`. The turn proceeds. *(Task 5 test `test_media_failure_records_without_description`.)*
5. **Sleep windows that cross midnight and DST-free timezones.** `23:59-07:30` puts 00:30 and 23:59:30 asleep and 12:00 awake. *(Task 1 test `test_sleep_window_crosses_midnight`.)*

---

### Task 1: Habits and the presence state machine

**Files:** create `dom/presence.py` and `tests/test_presence.py`.

**Interfaces:**
- `Habits(sleep_start: time, sleep_end: time, online_minutes: tuple[int,int], offline_minutes: tuple[int,int], wake_after_messages: int, kapoh_wake: float)`
- `parse_habits(text: str) -> Habits` reads `key: value` lines and applies the defaults and clamps from Global Constraints.
- `State = Literal["asleep","offline","online"]`
- `Presence(store: Store, tz: ZoneInfo, rng: random.Random, now: Callable[[], datetime])` with:
  - `async state(chat_id, habits) -> tuple[State, datetime | None]`. The datetime is when the current state ends.
  - `async go_online(chat_id, habits, minutes: float | None = None) -> datetime`. With no `minutes` it draws `rng.triangular(lo, hi, hi - 0.2*(hi-lo))`, as V1 did.
  - `async go_offline(chat_id, habits) -> datetime`. The draw is triangular with mode near `lo`.
  - `async extend_online(chat_id, minutes: float) -> datetime`. Capped at +30 min per call.
  - `async wake(chat_id, habits, minutes: float = 30) -> datetime`. The owner override works even during sleep and sets `online_until`.
- **Rules:**
  - Inside the sleep window the chat is `asleep`, unless `online_until` comes from a `wake` that's still active.
  - When an online session expires, the chat is `offline` until `offline_until`.
  - Once `offline_until` passes, the chat stays offline until something wakes it: a message plus the wake rules in Task 3.
- **Storage:** uses the existing `chats` columns `is_sleeping`, `sleep_until`, `is_offline`, `offline_until` and `online_until`, with new store methods `get_presence(chat_id)` and `set_presence(chat_id, **cols)`. No migration is needed.

- [ ] Write the tests:
  - `test_sleep_window_crosses_midnight`
  - `test_parse_habits_defaults_and_clamps`, covering a malformed line and a 2 h sleep clamped to 4 h
  - `test_online_session_expires_to_offline`
  - `test_wake_overrides_sleep`
  - `test_extend_online_capped`
  - `test_triangular_uses_injected_rng`
- [ ] Run the tests and confirm they FAIL. Implement. Confirm they PASS.
- [ ] Commit: `feat(presence): habits parsing and per-chat presence state`.

### Task 2: Jev client and triage

**Files:**
- Create: `dom/jev.py`, `dom/triage.py`, `tests/test_triage.py`
- Modify: `dom/config.py` (default `[models.triage]` and `[models.media]` entries) and `dom/starter_home/config.toml`

**Interfaces:**
- `JevClient(http: httpx.AsyncClient, api_key: str, model: str)`:
  - `.decide(state: str, questions: dict) -> dict[str, dict | None]` returns each answer, or `None` for a refusal, a missing key or an unknown type.
  - It raises `JevUnavailable` on transport errors, non-2xx responses or a timeout. The timeout is 8 s, with no retries; triage must be fast.
- `persona_card(home: Home) -> str`: the SOUL body up to the end of its first `##` section, capped at 600 chars.
- `Triage(action: Literal["reply","react","ignore"] | None, emoji: str | None, kapoh: float | None, confident: bool)`
- `triage_online(jev, card: str, window_xml: str) -> Triage`. One Jev call with 2 questions:
  - `action` (choice): `reply`, `react`, `ignore`, with criteria `reply`: "Write a reply; he is addressed or has something worth adding.", `react`: "Just react with an emoji; acknowledgement is enough.", `ignore`: "Stay silent; nothing for him here."; instructions "What should Dom do with the newest messages?"
  - `emoji` (choice): `😂 💀 🔥 👍 🤡 ❤️ 😭 👀`
  - `confident = action.confidence >= 0.70`
- `triage_offline(jev, card, window_xml) -> Triage`. One question: `kapoh` (score) with criteria `["boring","mildly interesting","juicy","must jump in now"]`.
- Window passed to Jev: the last 15 messages, rendered with the existing XML formatter.

- [ ] Write the tests with `httpx.MockTransport`, using the verbatim response shape from Global Constraints:
  - `test_decide_parses_choice_and_score`
  - `test_refusal_and_missing_key_are_none`
  - `test_jev_timeout_raises_unavailable`
  - `test_low_confidence_not_confident`
  - `test_persona_card_capped`
- [ ] Run the tests and confirm they FAIL. Implement. Confirm they PASS.
- [ ] Commit: `feat(triage): Jev Decisions API client and triage`.

### Task 3: Dispatch (debounce → presence → triage → turn) and catch-up

**Files:**
- Create: `dom/dispatch.py`, `tests/test_dispatch.py`
- Modify:
  - `dom/telegram/handlers.py`: replace the `should_respond` + `_run_chat_turn` call path with `Dispatcher.notify(msg)`. Owner commands `/wake` and `/sleep` in the owner DM work for any approved chat id given as an argument; with no argument they apply to the owner DM.
  - `dom/tools.py`: add the `extend_online` tool for all scopes. Its argument is `minutes`, up to 30.
  - The prompt turn note. It now includes:
    - `state`
    - online minutes left
    - messages that arrived while away
    - `"you just came online"` on catch-up
    - `desktop: off`, a fixed string until Phase 5

**Interfaces:**
- `Dispatcher(app, presence, jev: JevClient | None, sleep, now)` with:
  - `notify(msg: IncomingMessage)` schedules `_decide(chat_id)` after a debounce of `rng.uniform(2, 6)` seconds. A new message for the same chat restarts the timer.
  - `_decide(chat_id)`. Under the chat's turn lock, re-check the gate. Then branch on the presence state:
    - **asleep:** do nothing; messages are already recorded.
    - **offline:** count unread messages since `last_read_msg_id`.
      - If that count is at least `wake_after_messages`, wake.
      - Otherwise, if it's a group and Jev is available, run `triage_offline`; if `kapoh >= kapoh_wake`, wake.
      - A wake calls `go_online`, then runs one catch-up turn over the window, and the turn note says how many messages arrived.
    - Offline DMs (owner's included) wake only through the count rule, `/wake`, or the background loop's next session; kapoh triage applies to groups only.
    - **online:** run `triage_online`.
      - `ignore` with `confident`: do nothing.
      - `react` with `confident`: call `set_message_reaction` with the emoji on the newest message.
      - `reply`, or not `confident`, or Jev unavailable: run the agent turn. The model may still choose `no_response`.
    - After any turn, call `store.mark_chat_read(chat_id, newest_msg_id)`.
  - A background loop wakes every 60 s. It moves chats whose online session has expired to offline. If a chat has unread messages when its offline period ends, the loop starts a new online session plus catch-up. This is "coming back online" from V1.

- [ ] Write the tests with a fake clock, fake sleep, fake Jev and a fake model, counting calls:
  - `test_asleep_flood_no_calls_single_catchup`
  - `test_offline_wakes_after_n_messages`
  - `test_offline_kapoh_wakes_group`
  - `test_online_ignore_confident_no_model_call`
  - `test_online_react_sets_reaction`
  - `test_low_confidence_goes_to_model`
  - `test_jev_unavailable_falls_back`
  - `test_debounce_coalesces_burst`
  - `test_owner_wake_command_only_in_owner_dm`
  - `test_extend_online_tool`
- [ ] Run the tests and confirm they FAIL. Implement. Confirm they PASS.
- [ ] Commit: `feat(dispatch): presence-aware dispatch with Jev triage and catch-up`.

### Task 4: Bot-to-bot

**Files:** modify `dom/dispatch.py`, `dom/telegram/handlers.py` and `dom/telegram/convert.py` (stop dropping `is_bot` senders). Add `tests/test_bots.py`.

**Interfaces and rules:**
- Messages from other bots are recorded and dispatched like any other.
- **Depth counter:**
  - A message from a human resets `chats.bot_depth` to 0.
  - Each Dom turn triggered by a bot message increments it.
  - At depth 6, Dom runs one final turn with the turn note `"Leave this bot conversation now, in character, in one short message."`, and sets depth to 7.
  - At depth 7 or more, bot messages are recorded but trigger nothing.
- **Rate limit:** at most one reply per bot sender per 10 s.
- **Dedup:** identical text from the same bot within 60 s is ignored.
- Dom's own messages are never dispatched.
- **Setup notes in the README** (Phase 7 will polish): enable "Bot-to-Bot Communication Mode" in BotFather, and disable privacy mode or make Dom an admin in the groups.

- [ ] Write the tests:
  - `test_bot_loop_stops_after_depth`
  - `test_human_message_resets_depth`
  - `test_bot_rate_limit`
  - `test_bot_duplicate_ignored`
- [ ] Run the tests and confirm they FAIL. Implement. Confirm they PASS.
- [ ] Commit: `feat(bots): bot-to-bot conversations with deterministic stop`.

### Task 5: Media understanding

**Files:**
- Create: `dom/media.py`, `tests/test_media.py`
- Modify:
  - `dom/telegram/convert.py`: `media = {"kind": ..., "file_id": ..., "file_size": ..., "mime": ...}` for photo (largest size), animation, video, video_note, voice, audio and sticker. Use the sticker's `emoji` too.
  - The store's `format_chat_window_xml`: render `<media kind="photo">description</media>` inside `<msg>`.

**Interfaces:**
- `MediaDescriber(client: ChatClient, bot)`:
  - `.describe(media: dict) -> str | None`
  - Skip files larger than 20 MB.
  - Download with `bot.download(file_id)`.
  - Send an OpenAI-style content part:
    - photo or sticker: `image_url`, as a data URL
    - voice or audio: `input_audio`, base64 with a format from the mime type
    - video, animation or video_note: `video_url`, as a data URL
  - Prompt: `"Describe this for a friend reading a group chat: what it shows/says, any text, the vibe. Voice/audio: transcribe, then one line on tone. Max 80 words."`
  - Return `None` on any error, and log it.
- Recording flow: record the message first. If it has media, run `describe` as a background task. Store the result with a new `Store.set_media_description(chat_id, msg_id, description)`, which updates `media_json`. Dispatch waits up to 15 s for pending descriptions in that chat before deciding.

- [ ] Write the tests with a fake bot download and a fake client:
  - `test_photo_described_and_stored`
  - `test_media_failure_records_without_description`
  - `test_oversize_skipped`
  - `test_window_renders_media`
  - `test_voice_uses_input_audio_part`
- [ ] Run the tests and confirm they FAIL. Implement. Confirm they PASS.
- [ ] Commit: `feat(media): describe photos, GIFs, video, voice and stickers on arrival`.

### Task 6: Live check (manual; needs the owner)

- [ ] Run `uv run pytest` and confirm every test passes.
- [ ] Make one real Jev call and one real media call through the Phase 2 clients, using a script in the scratchpad. Record latency and cost.
- [ ] With the real bot, run through the following, writing results to `docs/superpowers/plans/2026-11-19-phase2-presence-triage.results.md`:
  - sleep, the wake rules, react vs reply, catch-up after offline
  - a voice note
  - a bot-loop check against a second test bot
