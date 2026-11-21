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
`private/USER.md`. Nothing from an import goes to `memory/`.

The backup command makes an archive of the instance home and a consistent
SQLite snapshot. Archives include credentials and private data. Keep the
instance home and its backups outside the checkout and restrict access to them.
Local desktop browser state lives outside the instance home in a persistent
profile directory. It can contain signed-in sessions and needs its own file
permissions and backup policy.

## Chat scopes

| Scope | Context and access |
| --- | --- |
| Owner DM | Chat context, shared notes, owner notes and enabled owner tools. The owner can also search across chats. |
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
- **Media provider** receives supported attachments for transcription or
  description, and screenshots when the desktop worker requests a description.
- **Web search** submits the query to `models.search.base_url` (OpenRouter by
  default). A query drafted in the owner DM can contain private context.
  Remove `[models.search]` to disable this tool.
- **KLIPY** (GIF search, only with `KLIPY_API_KEY`) receives the short search query Dom wrote, from the Dom
  host. It sees no chat, user or owner identifier. Outside the owner DM the query goes through the outbound
  leak check when that is on. Telegram then fetches the chosen file from KLIPY. See [GIFs](gifs.md).
- **Jev** can receive recent chat context for triage. The outbound leak check sends proposed text and a
  bounded selection of headings, names, numbers and other terms from owner notes
  to the configured Jev endpoint. At or above the configured threshold Dom holds
  back the text and alerts the owner. If Jev cannot score the text, Dom may send
  it and record that the check was unavailable. `leak_check = false` turns off
  this additional check. The chat-scope boundary still applies.

- **Private group replies.** A reply Dom marks private in a group is shown only
  to the owner there. Where Telegram refuses that (Dom is not a group admin), it
  goes to the owner's DM instead, and the group's history records only that the
  owner was answered privately, never the answer.

## Plugins and desktop

A plugin tool receives the arguments Dom passes to it. A plugin slash command
receives only the text typed after the command and the chat's scope (owner DM,
friend DM or group), never the chat ID, the sender or chat history. A dashboard
panel receives nothing. Command replies are sent as plain text and are not added
to chat history. Panels return data that Dom renders with its own template.
Every plugin call runs in a separate Bubblewrap process with a cleared
environment, a bounded scratch filesystem and restricted system calls. Dom
requires owner approval of the code version and probes isolation before running
a plugin. Plugins are offline by default. A network-enabled plugin can send what
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
and it never messages anyone else. See
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
