# Privacy and data storage

## On the Dom host

The instance home holds `config.toml`, the `.env` secrets, the bot's personality
in `SOUL.md`, notes, skills and plugins. `state.db` stores chat state, searchable
messages, jobs, usage and approvals. `transcripts/` holds per-chat JSON Lines
records. Protect both the database and the transcripts as conversation data.

General notes, habits and the public-safe journal live in `memory/`, with
separate notes about people and chats. Owner preferences, personal notes and the
private journal live in `private/`. Keep personal information out of `SOUL.md`
and other shared notes. A memory import keeps no copy of the uploaded text: each
fact becomes a proposal, and only the facts the owner approves are added to
`private/USER.md`. Nothing from an import goes to `memory/`. This holds for an
export sent with `/import` or as an attached file. An export pasted into the
owner DM as an ordinary message, to reply to with `/import` later, is stored as
chat history like any other DM message.

Memory self-edit (`memory_self_edit = false` by default) is an optional,
owner-DM-only path alongside the background memory pass. When enabled, Dom can
propose edits to notes (`USER.md`, `NOTES.md`, the private and public journals,
`MEMORY.md`, people notes and chat notes) as a unified diff with a one-line
reason on an approval card. Nothing is written until the owner approves, and
approval refuses if the file changed since the proposal. The tool is never
offered outside the owner DM, screens public note edits locally so credentials
and `private/` notes cannot be moved into `memory/`, respects each file's size
bounds, and cannot touch `SOUL.md`, `HABITS.md`, `config.toml`, skills or
plugins.

## Memory across chats

The background memory pass writes notes after a chat goes quiet. Which files a pass may write is fixed in code, by
the chat's scope:

- the owner DM writes only `private/USER.md` and `private/NOTES.md`;
- a friend's DM writes only that DM's own chat notes (`memory/chats/<id>.md`);
- a group writes that group's chat notes, the people notes of the people who spoke in the batch
  (`memory/people/<id>.md`) and the shared `memory/MEMORY.md`.

Every chat reads `MEMORY.md`, and a group or DM reads the people notes of its participants. So a fact that lands in a
people note or in `MEMORY.md` can come up in another chat. What a friend tells Dom in a DM never leaves that DM, apart
from the owner DM's facts-only "elsewhere" lines.

Until v0.8.3 the pass was told only to keep durable facts. Since v0.8.3 a group pass is also told where each kind of
fact goes, and each offered note carries its purpose:

- a person's notes hold only what that person would say in any chat;
- anything specific to the group, or personal, goes in the group's notes;
- `MEMORY.md` holds Dom's own life and general facts, never other people's private matters;
- when unsure, the group's notes.

The code enforces which files a pass can write; the model decides what goes into them, so these rules lower the
chance of a leak and do not rule it out. A later release will record where Dom learned each fact and use it only in
chats whose members were there.

## Search and export

The dashboard's search box covers chats (FTS5), memory files, briefs, reminders, desktop tasks, skills and plugins. It is
owner-only. Messages from the owner DM show in full; a message from any other chat comes back as a one-line preview, and
the API sends no more than that.

Dom's `search` tool follows the scope. In the owner DM it covers the same items as the dashboard. In a friend's DM or a
group it searches that chat's own history and nothing else: memory files, other chats and `private/` cannot surface,
whatever the model asks for. The code checks the scope on every call.

The memory export is a zip of `SOUL.md`, `memory/`, `private/`, skills and presets, built off the event loop with a size
cap. It never holds `.env`, `state.db`, transcripts or backups; use `dom backup` for a full copy. Only the owner can get it,
from the dashboard's Memory page or with `/export` in the owner DM, and each one is logged as an owner event. The zip
holds private notes: keep it as safe as the instance home.

The backup command makes an archive of the instance home and a consistent
SQLite snapshot. Archives include credentials and private data. Keep the
instance home and its backups outside the checkout and restrict access to them.
Local desktop browser state lives outside the instance home in a persistent
profile directory. It can contain signed-in sessions and needs its own file
permissions and backup policy.

## Chat scopes

| Scope | Context and access |
| --- | --- |
| Owner DM | Chat context, shared notes, owner notes and enabled owner tools. The owner can also search across chats, memory files and the rest of the dashboard's search. |
| Approved friend DM | Its own chat history, the relevant person's notes, shared memory and chat-scoped tools. |
| Approved group | That group's chat history, notes about its participants, shared memory and chat-scoped tools. |
| Pending or ignored chat | No model requests for that chat. The owner can review pending chats in the owner DM. |

The code enforces these scopes when it builds prompts and exposes tools. The
dashboard is for the owner. Protect a published dashboard with Cloudflare
Access. In that deployment, dashboard requests and responses pass through
Cloudflare. Keep local bearer-token testing on loopback.

## What leaves the host

- **Telegram** delivers messages, attachments, approvals, reactions and other
  bot actions. Telegram already handles any conversation with the bot.
- **Chat model providers**, including any configured fallback, receive the
  scope-filtered prompt and applicable tool results. Background memory and
  reflection calls also send context relevant to the notes or journal they
  update. An external provider may therefore receive owner DM content.
- **Drafting model** receives the public brief or candidate for a plugin or
  skill. Dom screens it for instance credentials and verbatim private notes
  before sending it to the drafting endpoint, or to the chat route if no
  drafting provider can answer.
- **Media provider** receives supported attachments for transcription or
  description, screenshots when the desktop worker requests a description, and
  the sticker images from the sticker sets you list, to write the short previews
  Dom reads when choosing a sticker.
- **Web search** tries the providers configured under `[models.search]` and
  `[[models.search.fallbacks]]` in order. Self-hosted **SearXNG** keeps queries
  on your own network until SearXNG forwards them to its configured upstream
  engines. **OpenRouter web** requests `provider.zdr` under your OpenRouter key.
  **DeepSeek web search** (`api.deepseek.com/v1/tools/search`) and **Tavily**
  (`api.tavily.com/search`) send queries to their cloud search APIs under each
  provider's own data policy. A query drafted in the owner DM can contain
  private context. Remove `[models.search]` (or clear the Search role in
  Settings) to disable this tool.
- **KLIPY** (GIF search, only with `KLIPY_API_KEY`) receives the short search query Dom wrote, from the Dom
  host. It sees no chat, user or owner identifier. Outside the owner DM the query goes through the outbound
  leak check when that is on. Telegram then fetches the chosen file from KLIPY. See [GIFs](gifs.md).
- **Studio**, when enabled, receives Dom's screened speech or scene and, for a
  photo or round video, an outfit ID. Profile updates request a portrait by
  outfit ID. Dom sends these to the owner's configured server with a bearer
  token. Private replies use text only. Telegram receives the generated media,
  and Dom keeps the speech or scene in the originating chat's history. After a
  photo or round video is sent, the configured media model receives those
  generated bytes once to describe what was actually delivered; the call is
  skipped at the spend cap and its description stays in the same chat.
  Generation runs on the local server. Plain HTTP exposes the request to
  anyone able to observe that connection. See [Studio](studio.md).
  Optional cloud fallbacks are off by default and receive the screened scene
  or transcript only after the local Studio cannot serve the request.
  OpenRouter speech, image and video requests carry the backup's data policy
  (below); speech and images default to zero data retention. Photos can
  include the approved outfit portrait. For a cloud video note, OpenRouter and
  the avatar provider receive Dom’s voice audio and the approved portrait under
  the separate media key’s policy. OpenRouter video keeps each finished video for download, so
  no video endpoint offers zero data retention and a video backup must be set to
  `no_training` or `any`. Direct Gemini gets the
  transcript and the prebuilt voice name only when the owner marks its key
  paid. Google's Paid Services terms exclude model improvement and allow
  limited safety and legal logging.
  [OpenRouter ZDR](https://openrouter.ai/docs/guides/features/zdr) ·
  [Video generation](https://openrouter.ai/docs/guides/overview/multimodal/video-generation) ·
  [Gemini API terms](https://ai.google.dev/gemini-api/terms).
- **Jev** can receive recent chat context for triage. The outbound leak check sends proposed text and a
  bounded selection of headings, names, numbers and other terms from owner notes
  to the configured Jev endpoint. At or above the configured threshold Dom holds
  back the text and alerts the owner. If Jev cannot score the text, Dom may send
  it and record that the check was unavailable. `leak_check = false` turns off
  this additional check. The chat-scope boundary still applies. Proposed studio
  speech and scenes go through a local credential and private-note check before
  media generation, including in the owner DM. Outside the owner DM they also go
  through the configured Jev check, so in the owner DM speech and scenes can reach
  a cloud backup after the local check alone.
  When a configured Jev check cannot score speech or a scene, Dom holds that
  media back and can still send the other bubbles.

  Jev also sees the text of a chat reply in every chat type, the owner DM included, when the chat model answers in
  plain prose instead of the JSON actions object. Dom sends that reply and the same recent messages that triage
  uses, and Jev judges whether it is ready to send and how many bubbles it should be. Dom sends it as written
  when Jev is sure, and asks the model again otherwise. This happens only on such a reply, not on every turn, and
  `leak_check = false` does not turn it off. With no Jev API key (the one `models.triage.api_key_env` names) Dom
  makes no Jev calls at all and asks the model again as before.

  Triage also gets the name and one-line description of each approved skill (public text) and picks the one that
  fits, so Dom does not need a separate model call to read it. When an online session is within ten minutes of
  ending and could still be extended, Jev is asked once more after Dom's reply, with the same recent messages
  triage uses, whether he stays longer. These come with the reply's own Jev calls and need no extra setting; with no
  Jev key, Dom picks no skill ahead and the session ends as scheduled.

- **Private group replies.** A reply Dom marks private in a group is shown only
  to the owner there. Where Telegram refuses that (Dom is not a group admin), it
  goes to the owner's DM instead, and the group's history records only that the
  owner was answered privately, never the answer.
- **Escalation model** (`[models.escalation]`, or the drafting providers when it
  is unset) receives the whole owner-DM task prompt, owner notes included, only
  after the owner taps Switch or Retry on its card. It never runs in other chats. OpenRouter calls request
  `provider.zdr`. To estimate the cost, Dom fetches OpenRouter's public model
  price list (`openrouter.ai/api/v1/models`), which carries no chat data.

## Direct DeepSeek and other cloud providers

Dom recommends local providers or a zero-data-retention route for chat content. The owner can choose a
different provider. The dashboard highlights cloud routes whose retention terms need review.

DeepSeek's direct API uses the privacy terms for the DeepSeek account. DeepSeek says it stores personal data in
the People's Republic of China, keeps inputs used to provide the service while the account exists, and may
use inputs to improve or train its models. The policy describes how to opt out of training. DeepSeek does
not offer zero data retention on this route. [DeepSeek Privacy Policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html).

## OpenRouter data policies

Routes that carry chat content through OpenRouter always ask for zero data retention (`provider.zdr = true`): the
model roles (chat, fallback, media, drafting, search, escalation), triage and the leak check (Jev), the doctor's
probes and the escalation price list. None of them can be configured otherwise.

Only the studio's cloud backups (images, speech and video) take a `data_policy`, which Dom turns into OpenRouter's
provider routing preferences ([provider selection](https://openrouter.ai/docs/guides/routing/provider-selection)):

| `data_policy` | Sent as | What it guarantees |
| --- | --- | --- |
| `zdr` (default) | `provider.zdr = true` | Only zero-data-retention endpoints. Safety Retention endpoints (which may keep prompts and responses only to prevent abuse, never for training) are also eligible unless the account or a guardrail is set to **ZDR only**. |
| `no_training` | `provider.data_collection = "deny"` | Excludes endpoints that may train on or sell the data. Endpoints that keep prompts and responses are still allowed. |
| `any` | nothing | Any endpoint, including ones that may train on what is sent. |

A speech or photo backup sends the screened line or scene. Photos can include the outfit's approved portrait.
The avatar provider receives the approved portrait and Dom's rendered speech under the media key's policy.
OpenRouter video keeps each finished video for download, so a video backup takes `no_training` or `any`.

A backup's key variable may not be one a chat route uses (a model role or a saved provider), in either direction:
config validation and the dashboard refuse it, so a key set up for media can never carry chat content.

OpenRouter's own settings apply on top of the request and can only make it stricter
([ZDR](https://openrouter.ai/docs/guides/features/zdr),
[guardrails](https://openrouter.ai/docs/guides/features/guardrails)): the account's privacy settings can enforce
ZDR per model group, a guardrail assigned to a key or a member can too, and if any of them enforces ZDR the request
gets ZDR whatever its own flag says. A backup set to `no_training` or `any` is therefore still refused while the
account's "ZDR endpoints only" setting (or a ZDR guardrail on its key) is on.

### Separate media key

To let images, speech and video use non-ZDR endpoints while chat keeps hard ZDR at every layer:

1. Keep the existing key (`OPENROUTER_API_KEY`) for the model roles, Jev and search. In OpenRouter, create a
   guardrail that enforces ZDR for every model group, and assign it to that key under **API Keys**.
2. Create a second key for media only, named for example `OPENROUTER_MEDIA_API_KEY`. Give it no ZDR guardrail. Set
   its backups to `no_training` where the model allows, so endpoints that train on the data are still excluded by the
   request itself.
3. Turn off the account-wide "ZDR endpoints only" privacy setting, because any layer that enforces ZDR wins. Keep
   member and workspace-default guardrails free of ZDR too: a member's guardrail applies to all of their keys.
4. In Settings → Models → Studio cloud backups, add each backup with the media key variable, save, then add the key's
   value to `.env` and press Test.

Chat then keeps ZDR three ways: Dom's requests, the chat key's guardrail, and the rule that the media key never serves
a chat route. A guardrail has no training switch of its own: `no_training` comes from the request.

## Between chats

These flows stay on the Dom host and Telegram. No extra provider is involved
beyond the models listed above.

- **Owner alerts.** A group message that mentions the owner, or replies to one
  of the owner's own messages, can send the owner a short alert in the owner DM.
  The alert names the sender and the group and links the message; it does
  not quote it. Alerts are rate-limited.
- **Notes to the owner.** In a group or a friend's DM, Dom can send the owner a
  note in the owner DM. Notes are rate-limited and never come from the owner's
  own messages.
- **Messages to other chats.** Text Dom drafts in the owner DM for another chat
  is shown to the owner on a card. Nothing is sent until the owner approves that
  exact text, and the card expires after two hours. For a group, Jev's leak
  check scores the text before the card is shown, against the owner's private
  terms minus the names of that group's participants. A card for a friend's DM
  is not scored and says so. A group card is not scored when Jev has no key or
  the spend cap skips the call, and then says the leak check was unavailable.
- **"Elsewhere" notes.** After a conversation in a group or friend DM, Dom can
  keep a one-line, facts-only note of it. These notes go into the owner DM's
  prompt only, and so to the chat model when the owner DM is answered. Other chats learn only that Dom "was just in another conversation",
  never where or what. Nothing from the owner DM or private notes becomes such
  a note.

## Plugins and desktop

A plugin tool receives the arguments Dom passes to it. A plugin slash command
receives only the text typed after the command and the chat's scope (owner DM,
friend DM or group), never the chat ID, the sender or chat history. A dashboard
panel receives nothing. Command replies are sent as plain text and are not added
to chat history. Panels return data that Dom renders with its own template.
Every plugin call runs in a separate Bubblewrap process with a cleared
environment, a bounded scratch filesystem and restricted system calls. Dom
probes isolation before running a plugin. Staging trials use synthetic input
and empty disposable state in an offline sandbox. Live calls require approval of
the exact code version. Offline plugins may opt in to one persistent JSON blob
shared by their owner-only tools, commands and panels.
A network-enabled plugin can send what
it receives to external services, and its commands work only in the owner DM.
Dom screens tool arguments and command text for known secrets and verbatim owner
notes, and can run the Jev leak check for network plugin tool calls. See
[Writing plugins](plugins.md) for the limits.

The desktop is owner-only. Its separate worker receives the task brief,
applicable skills, and sandbox results. The brief can contain information
passed along from the owner DM. Staged documents are uploaded to the sandbox
for their assigned task. A local Docker desktop must pass LAN isolation probes
before starting. Remote desktop isolation depends on the operator's firewall
configuration. A browser login stays in the persistent sandbox profile, and
pages and screenshots may pass through the desktop tools and configured
models while a task is running.

Self-directed work (on by default, local desktop only) lets Dom plan up to three
small research tasks of his own, each reading up to four named public sites in a
separate `self-work` container. The owner's DM gets the plan before any task
starts, and one short summary after it. Planning uses `SOUL.md` and the public
journal; a task itself gets only its plan, nothing from private notes or chats,
and it never messages anyone else. The owner's DM history keeps a note of each
plan (goals and reasons) and how each task ended, so Dom can talk about his own
work; the summary of web content stays out of that history. See
[Self-directed work](desktop.md#self-directed-work).

## Home network and T3 Code

In the owner DM, `find_devices` reads the Dom host's mDNS answers and neighbour
table to find devices on the home network. Nothing is scanned. The list it finds
reaches the chat model like any tool result. A moved LAN address becomes a proposal for the owner.

The T3 Code bridge (off by default, owner DM only) lets the owner inspect
projects and coding activity on linked machines through a T3 Code service on the
Dom host. Thread text it reads is treated as untrusted. A message goes to a
thread only after the owner taps to confirm the destination and the exact text.
See [T3 Code bridge](t3-bridge.md).
