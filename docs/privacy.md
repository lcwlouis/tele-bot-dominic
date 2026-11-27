# Privacy and data storage

## On the Dom host

The instance home holds `config.toml`, the `.env` secrets, the bot's personality
in `SOUL.md`, notes, skills and plugins. `state.db` stores chat state, searchable
messages, jobs, usage, approvals, and encrypted credentials. `transcripts/` holds per-chat JSON Lines
records. Protect both the database and the transcripts as conversation data.

Saved desktop logins, owner-provided secrets (via `request_secret`), and integration
tokens are encrypted at rest with AES-256-GCM using `DOM_MASTER_KEY` in `.env`
(mode `0600`, rotatable via **System → Access** while Dom runs or `dom rotate-key <home>`
while Dom is stopped). E101 secrets and saved-login credentials are owner-only and
unreachable from groups or friends' DMs. Dom never sends a saved password or E101 secret
through Telegram chat, any model prompt or tool argument, any traced sandbox HTTP API
request (`/v1/browser/page/type`, `fill`, `evaluate`), any turn log, debug trace, usage
row, or Python log line (including `websockets` debug logging). Saved-login filling
always submits the form and, if a password is still in a field after the submit, clears
it over VNC; if anything fails or is cancelled after the password is typed and before the
submit, it makes a best-effort attempt to erase it over VNC before returning (a failed
attempt is possible, for example if the sandbox is unreachable). It aborts and alerts the
owner DM if focus leaves the password field mid-typing, and registers the username and password in a per-run redaction set for the
rest of that desktop task. Residual risk: once typed into Chromium inside the sandbox,
the password is present in the browser on the matched HTTPS origin. The page checks Dom
runs through the page's own JavaScript (focus, active field, form action) can be lied to
by a hostile page, so the browser's tab list, which a page cannot forge, is the trust anchor
for the site and is re-read right before each typing step. A hostile or
compromised page on that matched origin (or a compromised sandbox container/shell
during an owner task) could still capture the input, reveal it in an altered form or
screenshot that verbatim redaction cannot match, or steal the resulting session cookies.

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

The model decides what goes into the notes, so those rules lower the chance of a leak and do not rule it out. Since
v0.8.4 code backs them up with source tags (see `docs/memory-format.md`):

- every line in a person's or chat's notes records the chat it came from, or `any` for a general, non-sensitive fact
  the person would share anywhere. The pass picks the tag; the default is the chat it is running for;
- the prompt builder drops lines tagged to another chat and strips the tags, in code, before the model sees a note. A
  fact tagged to group A never reaches group B's prompt, even for a person who is in both. The owner's DM reads every
  public line. Untagged older lines count as `any`;
- a group's memory pass is shown only the lines it may use and cannot replace, remove or pin a line tagged to another
  chat;
- `MEMORY.md` is read by every chat and refuses chat-tagged lines;
- Dom's life sheet (`memory/WORLD.md`, see `docs/memory-format.md`) and the place notes it links are read through the
  same filter, in code, for every chat: a line tagged to another chat never reaches a group's or friend's prompt, and the
  owner's DM reads all of it. The variety hints are built only from Dom's own recent messages in the chat being
  answered. The nightly reflection that may change the sheet reads only group content and the lines no chat owns (never
  the owner's DM or `private/`), writes no tag, and cannot touch a pinned or chat-tagged line;
- a place's reference photo (`media/places/`) is the owner's own picture, saved without its camera data, and goes to
  an image model only when Dom names the place by its key on the image or video note (never guessed from the scene's
  words), the model takes a reference, and the chat has a line about that place (the owner's DM has them all); the
  turn note offers each chat only the keys it may see;
- the journal archive (`memory/journal/`, `private/journal/`) and the "recent life" summaries made from it are
  owner-DM only, because the diary mentions people from every chat. The private archive never leaves
  the owner's scope.

## Media Dom sees and sends

**What Dom sees.** Whenever the chat model accepts the type, Dom is sent the real
photo, voice note or video instead of a description of it: the newest turn's
incoming media and his own most recent generated media, at most
`media_input_images` images, `media_input_audio` recordings and
`media_input_video` clips per prompt, each up to `media_input_max_mb`. They come
only from the chat's own window, and ride after the prompt text, so the cached
prefix is unchanged. Older media stays as its `<media>` tag, with a description
only if one was already made.

What a model accepts comes from `input = ["text", "image"]` on its route in
`config.toml`, else from the provider's model list (OpenRouter lists each model's
input types), else from a one-off test of an OpenAI-compatible server with a
tiny generated image, cached per address and model. Unknown means text-only. The
test and the list fetch run in the background (never while a reply waits), and
the owner can run the test again with `POST /api/media/inputs/probe`.

**Clips: frame strips and a transcript.** A video, animation or video note that
the chat model can't take as video is, when it takes images and `ffmpeg` is
installed, cut into frame strips on arrival, in the background (never while a
reply waits). A strip is one image: evenly spaced frames, one every 2 to 5
seconds and at most 16, each with its time burnt in, made on this machine; every
frame fits a 320-pixel box and a strip never passes 2048 pixels a side, so an
odd-shaped clip can't make a huge image. A clip is downloaded up to 20 MB
(Telegram's own limit for a bot; the 8 MB limit applies only to a clip described
whole), one at a time, and at most the first 5 minutes 20 seconds are looked at,
in at most 4 strips; Dom's note on the clip says when it was cut, and which
stretches had frames that could not be read (a clip with too few readable frames
is treated as unreadable). Its sound goes to a model as a small audio file for a
transcript: **the media model when its route takes audio** (set `input = ["text",
"image", "audio"]` on the media route), else **the chat model when it takes
audio**, else nothing is transcribed and the note says so.

The chat model itself sees the whole clip: each strip is its own image on the
turn, with the transcript as text, inside `media_input_images` (the other media
in that turn count against it). With fewer free image slots than strips, strips
are combined into fewer images, their times intact; a clip with no slot left is
described by the media model instead (the turn waits for that description no longer than the catch-up wait, else it
gets the transcript and a note that the frames did not fit, and the job goes on for later turns), and the Turn log says so. A kept strip is a
JPEG in the same private `media/` folder as the photos Dom sends (`<message
id>-strip<N>.jpg`), counted in `studio.keep_cap_mb` and removed with the oldest
files; once Dom has answered, the media model describes the strips from those
files (one call per strip, each told what came before, joined with the
transcript) so a later turn remembers the clip. If the strips are gone by then,
the whole clip is described when it can still be fetched, else Dom's note says
the picture is no longer available. The strips and the sound go only to the
models above, under the usual spend caps. Where Dom's chat model takes no images
(or `media_input_images` is 0) but `ffmpeg` is there, the media model describes
the strips on arrival and the Turn log says `clip described by the media model`.
A chat model that takes video natively still gets a small clip itself; a clip too
big for it is cut into strips instead. Without `ffmpeg`, or when the clip can't be
watched, the media model describes the whole clip (up to 8 MB), as before.
Install `ffmpeg` from your system's package manager (`apt install ffmpeg` on a
Raspberry Pi, `brew install ffmpeg` on a Mac); Dom runs the `ffmpeg` found on
`PATH`, as the look before sending does.

A model that can't take a type, or a server that refuses the request, falls back
to a description from the media model; the Turn log says `media described` or
`media refused`. With triage turned on, a photo is still described once for the
triage model, which can't see.

What changes for descriptions: a photo Dom saw himself is no longer described
when it arrives, and the media model's usage is much smaller. It is described
later only where text is needed: just before the memory pass reads its batch
(under the spend caps, never on the reply path), when catch-up reads media that
did not fit the per-prompt limits, and when the file can't be downloaded for the
turn (if that fails too, its `<media>` tag says it couldn't be loaded). Global search and the dashboard never showed
descriptions, so nothing changes there.

**What he keeps.** Each photo, voice note and video note Dom sends (and the frame
strips of the clips he is sent) is saved
under `media/<chat id>/<YYYY-MM>/<message id>.<ext>` in the home, with the scene
or speech text, kind, time and Telegram's `file_id` on the message. The folder
is owner-only (0700); files are written to a temporary name and moved into
place; a chat's files are read only through that chat's own id, so they never
reach another chat's prompt. The dashboard's owner-only API lists and serves
them (`/api/media/files`, `/api/media/usage`). Total size is capped by
`studio.keep_cap_mb` (default 2048): past it the oldest files are removed, and
their messages keep the `file_id`, so Telegram can still provide them. The
folder is left out of `dom backup`, except `media/places/`: the owner's place photos exist nowhere else, and `dom restore`
brings them back. At startup, files no message records (a
crash between saving and recording) are removed, so the cap matches the disk. Dom's other chats and friends can't see this
folder or its listing.

**Where each item came from.** The message's media record also notes how it was made: the local studio or a cloud
backup (host and model id), the outfit and preset, how long it took, the length of a voice or video note, whether
the look before sending redid it, the scene still behind a video note, and the ids of the usage rows of the paid
calls. No dollar amount is stored: the library adds up those rows when it is read, so a cost OpenRouter reports late
appears then. It is owner-only like the files, and never reaches a prompt.

**Looking before sending.** In the owner DM (and, if you turn on
`studio.look_friend_dm` or `look_group`, there too), when the chat model takes
images, Dom checks a generated photo, or a video note's first frame
(needs `ffmpeg`), against its scene in one short call, and regenerates once with
the adjusted scene if it doesn't match. The better of the two is sent; it never
loops. A cloud video note, which can't take a new scene and costs money to
repeat, is redone once with the same inputs, and only when its frame is
clearly wrong (the wrong person, a broken frame, a wildly wrong setting). The
calls, redo included, are counted under media usage and respect the spend caps.

The daily spend cap pauses friends' and groups' chats and their background work. Background work done for the owner's DM
(the look before a send, aged-media descriptions, the memory pass, reflection) is not paused by the daily cap, but it
obeys the monthly cap and the area limits. The owner's own turns in the DM, tools included, stay exempt from both caps
unless `cap_owner_dm` is on.

## Search and export

The dashboard's search box covers chats (FTS5), memory files, briefs, reminders, desktop tasks, skills and plugins. It is
owner-only. Messages from the owner DM show in full; a message from any other chat comes back as a one-line preview, and
the API sends no more than that.

Dom's `search` tool follows the scope. In the owner DM it covers the same items as the dashboard. In a friend's DM or a
group it searches that chat's own history and the note lines of the knowledge index that chat may see: `MEMORY.md`
lines, that chat's own note (whatever its lines are tagged), the notes of people present, and place, project and goal
lines tagged for that chat or `{any}` (an untagged line in those notes is for the owner DM only). `private/` and other
chats' lines cannot surface, whatever the model asks for. The code checks the scope on every call.

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

## Debug traces

The debug tracer keeps a full record of every turn in every chat (owner decision E102: "this is a debug trace for a
reason"), so the owner can see how Dom decided and how to guide him. A trace holds the exact prompt, every model request
and response, tool arguments and results, the checks and what was sent. That includes friends' and groups' messages in
full, which is the one place the dashboard shows another chat's text beyond a one-line preview.

- **Where:** `traces.db` in the instance home, a separate owner-only (0600) SQLite file with its own connection.
  `dom backup` leaves it out.
- **How long:** `tracer.retention_days` (7 by default). The store is capped at `tracer.max_mb` (256 MB by default), and
  the oldest traces are deleted first.
- **Redaction:** secrets are redacted before anything is written. They are:
  - the values of every key variable `config.toml` names, the Telegram bot token and the other fixed credentials,
    also when they come only from the environment;
  - any `.env` variable whose name says it holds a credential (KEY, TOKEN, SECRET, PASSWORD, AUTH, COOKIE, SESSION…).

  Each value is matched raw, JSON-escaped, URL-encoded and base64-encoded; a value shorter than 8 characters is
  matched only as a whole word. Any `Authorization`, `Cookie` or `Set-Cookie` value, `Basic` or `Bearer` credential,
  and API key or bot token shape is redacted whatever its value. Request headers are never recorded. Media inside
  requests is replaced by a placeholder with its type, size and hash.
- **Residual risk:** a secret split across two separate strings, such as two tool arguments, is not caught.
- **Who sees it:** only the owner, on the dashboard. The trace reads and exports are same-site only.
- **Deletion:** forgetting a chat with its history deletes its traces, under every id it had, including ones still
  waiting to be written. If that fails, the dashboard says so and Dom deletes them at the next start. A message deleted in Telegram stays in older traces until retention removes it.

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
- **Open-Meteo** (only when `[world]` is set) receives the configured latitude and longitude, in a plain HTTPS request
  about every 45 minutes, to answer with the current weather and the day's forecast for Dom's turn note. No key,
  place name, chat or personal text is sent.
- **Nager.Date** (only when `[world] country` is set) receives the configured country code and the year, in a plain
  HTTPS request about once a day, to answer with the public holidays for Dom's turn note. No key, place, chat or
  personal text is sent.
- **The web search provider and the chat model** (only when `[world]` has a place or country and a search route is set,
  unless `[world] news` is off) receive that place or country name in a few searches each evening, and the model gets
  the numbered result titles and snippets to pick from. No chat text or private note is part of either request. Only
  a result's own title and domain are kept, filtered for anything that reads as an instruction, and they may reach
  every chat as headlines marked as untrusted web text. The triage model also reads the night's few kept headlines, as
  public web text, in one call that screens them.
- **Chat model providers** also receive the real photos, voice notes and clips
  of the newest turn (and Dom's own latest generated media) when the chat model
  takes that type of input. See [Media Dom sees and sends](#media-dom-sees-and-sends).
  Once Dom has answered and that media is no longer attached, the media provider describes it once in the background
  (under the spend cap), so a later turn still knows what a photo showed.
- **Media provider** receives supported attachments for transcription or
  description (only when the chat model can't take that type, or for the
  triage model, which reads text only), screenshots when the desktop worker requests a description, and
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
  and Dom keeps the speech or scene in the originating chat's history.
  Generation runs on the local server. Plain HTTP exposes the request to
  anyone able to observe that connection. See [Studio](studio.md).
  Optional cloud fallbacks are off by default and receive the screened scene
  or transcript only after the local Studio cannot serve the request.
  OpenRouter speech, image and video requests carry the backup's data policy
  (below); speech and images default to zero data retention. Photos can
  include the approved outfit portrait. For a cloud video note, OpenRouter and
  the avatar provider receive Dom’s voice audio and the picture to animate under
  the separate media key’s policy: the approved portrait, or, with `video_scenes`
  on (the default), a fresh still made from the note's scene (the scene text goes to
  the image backup, with the approved portrait as its reference). OpenRouter video keeps each finished video for download, so
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
  it and record that the check was unavailable. `leak_scan` (`strict`, `standard`,
  `relaxed` or `off`; see [the leak scan](setup.md#the-leak-scan)) sets the threshold,
  and `off`, like `leak_check = false`, turns off this additional check. The chat-scope boundary still applies. Proposed studio
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
The avatar provider receives the picture to animate (the approved portrait, or a fresh still of Dom in the note's scene) and Dom's rendered speech under the media key's policy. A voice backup's optional `voice` is only a voice name.
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
- **Goals and noticings.** Personal goals and nightly noticings (including Own
  work ideas) are strictly owner-only: they exist only in the owner DM and the
  owner dashboard APIs (`/api/goals`, `/api/noticings`, and Home's `for_you`
  summary). Code filters the `goals` tool, turn-note context and opener
  check-ins so nothing about goals or noticings ever reaches groups or friends'
  DMs. Nightly noticings run in at most one small reflection call under the
  spend caps, reading only owner-DM messages, approved-group context,
  goals, Own work and R13-filtered notes (never friends' DMs or unapproved
  chats). Any group reference is restricted in code to a one-line paraphrase
  with no verbatim group text, handles or raw R13 tags. Tapping **Let's do it**
  acts through existing tools, reminders or Own work, and always routes
  anything targeting another chat through an approval card; **Not now**
  dismisses the item and feeds Dom's preference history for future nights.

## Google Calendar and Gmail

Optional, owner-only, and off until the owner connects Google and turns a switch on (see
[Google Calendar and Gmail](google.md)).

- **Where tokens live.** The OAuth refresh and access tokens are encrypted in `state.db` with the same AES-256-GCM key
  as the other secrets (`DOM_MASTER_KEY` in `.env`, namespace `google`). The OAuth client id and secret are in `.env`.
  No token, code or client secret is written to any log, turn log, usage row, prompt or tool result, and none reaches the
  debug tracer: Dom's tool results and its own log lines never carry one, and the tracer also redacts the client secret
  by its instance list. Calendar and mail text that Dom reads in the owner DM is turn content like any other, so the
  tracer's full trace of that turn holds it (owner-only, on the dashboard). A database copy
  without `.env` is unreadable. A full backup holds both, like it does for `.env` today. Disconnect revokes the token
  at Google and deletes it here. The ids of the calendars Dom made (and which of their events carry a nudge) are
  encrypted in the same store, per Google account (a hash of its `sub`, from an `openid` ID token that is not kept); a copy
  of the database alone shows neither. Only the connected account's list is used, and another account connecting sets the
  earlier one's aside and cancels its nudges.
- **Dom writes only to his own calendars.** With `calendar.app.created`, Dom makes calendars of his own and changes events
  only on those; code refuses any other target, your primary calendar included, and Google enforces the same. Making a
  calendar is asked every time. He still reads your calendars as before.
- **A nudge reaches only you.** An event on his calendar can carry a reminder; it is an ordinary reminder in the owner DM
  and is never sent to a group or a friend. Removing a calendar, or moving or deleting an event, cancels it.
- **Who can reach it.** The `calendar`, `gmail`, `drive` and `contacts` tools are offered only in the owner's DM, only for the switches
  that are on and the scopes Google granted, and each handler checks the owner DM and the switch again in code. The
  "today and tomorrow" block in the turn note is added only for the owner DM. A group or a friend's DM never gets the
  tools, the block or any result.
- **What goes to which model.** Event titles, times and attendees, and the mail Dom reads (headers, snippets, a
  capped text body, attachment names), are tool results in the owner DM, so they go to the chat model route that
  serves that turn, and to its fallbacks, under the same `zdr` routing and provider rules as the rest of the owner
  DM (see [OpenRouter data policies](#openrouter-data-policies)). Dom's tool results never include attachments' contents, tokens or
  raw headers beyond From, To, Cc, Date and Subject. Nothing from Google is sent to the media, search, drafting or
  embedding models, to Telegram except what Dom writes in the owner DM, or to any other service. Mail and event
  text is written by other people and is fenced as untrusted data in every result; it is never an instruction.
- **Memory.** The owner DM's memory pass writes only the owner-private notes (`private/USER.md` and
  `private/NOTES.md`) and the nightly reflection keeps the owner DM in its private journal, so what Dom says about mail
  or events in the owner DM cannot reach notes that groups or friends read. Dom's tool descriptions also tell him not to copy mail into notes or other chats.
- **Drafts and events.** Adding, moving or deleting an event and saving a draft each need an approval card unless the
  owner chose Allow always for that kind. Dom has no way to send mail.
- **Drive sees only what Dom made or was handed.** Dom's connection holds `drive.file` and nothing wider (never `drive`,
  `documents` or `spreadsheets`), so Google shows him only the files he made, in his own folder "Dom", and the files the
  owner picked for him with Google Picker. Creating a file, adding to a Doc, adding rows to a Sheet and replacing cells
  each need an approval card that shows the file's name and the exact change, unless the owner chose Allow always for that
  kind. The `drive` tool is offered only in the owner DM, when the Files switch is on and Google granted the scope; the
  handler checks both again in code.
- **Contacts are read only.** The Contacts switch (`contacts.readonly`) lets Dom look up a name for an email address or
  phone number in the owner DM. Nothing can be changed, and the lookup is also used by Dom's own booking code only to find
  one unambiguous address.
- **What goes to which model.** File text Dom reads and the contacts he looks up are tool results in the owner DM, so they
  go to the chat model route that serves that turn under the same `zdr` routing and provider rules as event and mail text,
  and to no other model or service. Read results are size-bounded and fenced as untrusted data, never instructions, and
  Dom is told not to copy them into notes or other chats.
- **The file picker page.** Handing Dom a file opens a dashboard page behind the same login as the rest of the dashboard.
  It is the only page that loads Google's scripts and frames, and has its own content security policy for that. No token
  of Dom's reaches the browser: the browser asks Google for its own `drive.file`-only token, and Dom's server asks Google
  whether the pick worked. The page holds the OAuth client id, the browser API key and the Cloud project number
  (`GOOGLE_PICKER_API_KEY`, `GOOGLE_PROJECT_NUMBER` in `.env`); they are never logged or shown anywhere else. The Picker
  and Google's sign-in are Google's services running in your browser, under Google's terms.

## Assistant mode

Assistant mode is off until the owner turns it on, and it only ever speaks in the owner's DM.

- **Who can reach it.** The morning plan, the evening wrap-up and the follow-through block in the turn note are built
  only for the owner DM, and code checks that the chat is the owner's DM and the owner's chat id every time. A group
  or a friend's DM never gets the block, a message or any of its content; tests cover both.
- **What goes to which model.** Each plan is one call to the chat model route that serves the owner DM (and its
  fallbacks), under the same `zdr` routing and provider rules. It is one completion with no tool offered, so nothing
  can run while the model holds the day's facts. It carries the day's facts: reminders and goal text, the
  weather and holiday line, and, only while Google is connected and Calendar read is on, today's and tomorrow's event
  titles, times, locations and attendees, fenced as untrusted data. Nothing goes to the media, search, drafting,
  embedding or any other service. The call is attributed to `assistant` in usage and obeys the monthly cap and the
  area limits.
- **What is kept.** The plan is a normal DM message in chat history, so the next turn and the memory pass see it as
  any owner DM message; the owner DM's notes stay owner-private. The dashboard shows only when each message was last
  sent and a one-line reason, never its text or the facts behind it.

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
