# Set up Dom

## Create a Telegram bot

Open BotFather in Telegram, send `/newbot`, and keep the token for the next
step. In BotFather's bot settings, turn off group privacy mode if Dom should
receive ordinary group messages. You can instead make the bot an administrator
in the groups where it needs to see those messages. Enable Bot-to-Bot
Communication Mode in BotFather if you want Dom to receive messages from other
bots. Dom bounds bot-to-bot exchanges and waits for a human message after it
reaches the limit. [Telegram bot settings](https://core.telegram.org/bots/features).

## Prepare the Pi and instance

Use a 64-bit Raspberry Pi OS installation, Git, `uv` and Python 3.12. Install
Bubblewrap on Linux if you want plugins and Docker if you want a local desktop.
For a Pi service, stage the app following the [Pi deployment
guide](deploy-pi.md). For a foreground trial, open a checkout of this repository
and run `uv sync --frozen`.

Run the following from the app directory. On the Pi release layout this is
`~/dom/app`. Choose a sibling instance directory so updates keep your data.

```sh
instance="$HOME/dom/instance"
mkdir -p "$(dirname "$instance")"
uv run --no-sync dom init "$instance"
cp "$instance/.env.example" "$instance/.env"
chmod 600 "$instance/.env"
```

Save the claim code printed by `init`. Edit `.env` and set
`TELEGRAM_BOT_TOKEN`. With the starter models, set `OPENROUTER_API_KEY` too.
Edit `config.toml` to set your IANA timezone and `SOUL.md` to describe your
bot's voice and character.

The starter configuration uses OpenRouter for chat, media, `[models.drafting]`,
Jev triage and web search. You can point chat at a self-hosted OpenAI-compatible
server that supports tool calling. For example, replace the `[models.chat]`
table with the following and set `MODEL_API_KEY` in `.env` if the server
requires it.

```toml
[models.chat]
base_url = "http://127.0.0.1:8000/v1"
model = "your-model-id"
api_key_env = "MODEL_API_KEY"
```

Omit `api_key_env` for a server without authentication. The media,
`[models.drafting]`, triage and search endpoints keep their own settings and
credentials. To disable web search, remove `[models.search]`. See the
[configuration reference](configuration.md) for per-role fallback chains
(`[[models.<role>.fallbacks]]` and `[models.fallback]`) and all defaults.

## Where the studio runs

`dom setup <home>` asks where the media studio (voice notes, round videos and photos)
runs and writes the matching `[studio]` settings, keeping the old file as
`config.toml.bak`. It first says once, as plain text, that zero data retention is
recommended for every cloud model that sees your chats; the dashboard afterwards shows
that as an info glyph, never as a warning.

| Choice | What it writes |
| --- | --- |
| On this machine | `[studio] enabled = true` and `base_url` (default `http://127.0.0.1:8080`), with the token in `.env` as `STUDIO_TOKEN`. |
| On a rented cloud GPU | The same, with the GPU's address and its token, both required. Reach the GPU over Tailscale, as [the studio guide](studio.md#where-the-studio-runs) says. |
| External APIs only | The studio off (`enabled = false`, no address), the voice and photo switches on, and OpenRouter voice and photo backups using `OPENROUTER_MEDIA_API_KEY`. Add that key to `.env`. Video stays off until you choose a backup: no OpenRouter video endpoint offers zero data retention. |

Skip the questions with `dom setup <home> --studio local|cloud|apis --studio-url <address>`.
The token is only ever asked for at a hidden prompt, never as an argument. Backups you
already configured are kept.

## Optional: GIFs

Dom can send reaction GIFs when `KLIPY_API_KEY` is set in `.env`. Without it the
`gif` action is not offered and `self_inspect` reports GIFs as off. See
[GIFs](gifs.md) for how to get a key.

## Check, run and claim

From the app directory, check the instance. Resolve failures before continuing.
An unclaimed owner, a missing database before the first run, and missing
backups are expected warnings. Add `--online` to check Telegram and send small
synthetic requests to the configured providers. Those requests can incur usage.

```sh
uv run --no-sync dom doctor "$instance"
uv run --no-sync dom doctor "$instance" --online
```

On the Pi service layout, use the staged virtual environment.

```sh
.venv/bin/python -m dom install-service "$instance"
.venv/bin/python -m dom install-service "$instance" --yes
```

For a foreground trial, run `uv run --no-sync dom run "$instance"`. Run a single polling
process for the bot token. Open a private Telegram chat with the bot and send
`/claim <code>` using the code from `init`. The bot records your user ID and
publishes the owner command menu. Send `/wake` and then a message to try it.

A new group or a stranger's DM starts pending. Dom sends an approval request to
the owner, with the chat's Telegram ID and the first message, and stays silent in
that chat until approved. Use the approval buttons
or send `/pending` in the owner DM to resend pending requests.

## Spend limits

`daily_spend_cap_usd` (default `1.0`) is the normal daily pace and
`monthly_spend_cap_usd` (off by default; set it, for example to `30`) a hard ceiling for the
calendar month in `timezone`. Both cover every paid call: chat, media, search, desktop tasks, embeddings,
summaries and studio cloud backups. Zero turns a cap off. When either is reached,
friends' chats and background work pause, and you get one message per day or month.
Your own DM is never paused by a cap unless `cap_owner_dm = true`; either way, what it
spends counts toward the totals.

`[area_spend_caps_usd]` sets an optional monthly limit per area: `chat` (replies, triage,
the leak check, escalation), `media` (reading attachments, studio voice, photos and
video), `search`, `desktop` and `memory` (the memory pass, reflection, summaries,
embeddings). An area at its limit pauses that work until the 1st while the others
go on. One-offs such as event days draw only on the month's unused budget and never pass the
monthly cap or their area's limit; a cost that a provider reports late counts once it
arrives. OpenRouter's own key limits remain a second safety net.

The dashboard's Spend view reads `GET /api/spend` (today, the month so far, each area,
the caps, what is left and a 30-day daily series by area), and `POST /api/spend/caps`
saves the caps like any other setting: validated, written to `config.toml`, applied
without a restart.

## The leak scan

`leak_scan` sets how firmly the Jev check holds back a reply or an online plugin call:
`strict` (score 0.7), `standard` (1.2, the default and the old `leak_threshold`),
`relaxed` (2.0) or `off`. Jev scores a message from 0 (nothing private) through 1 (a vague
allusion) and 2 (reveals a private fact) to 3 (reveals several or sensitive facts), so
strict holds a mere hint, standard anything beyond an allusion, and relaxed only what
reveals a fact. `off` skips the Jev check; the local check for credentials and
verbatim private notes in public files and plugin inputs always stays on. An older
`leak_threshold` still works while `leak_scan` is unset and is shown as the nearest level, or
as custom. `GET` and `POST /api/leak-scan` read and set it.

## Services

`GET /api/services` lists what has no other home in Settings, each with a label, a state
(`ok`, `missing`, `error` or `off`), a one-line detail and its editable fields: the KLIPY
key, the local studio's address and token, the desktop hosts and T3 Code (whether it is
on, whether it is signed in, its version, what it may do, and the sign-in and audit
commands). `POST /api/services/save` changes fields and `POST /api/services/test` makes
one harmless real request. A secret is only ever written to `.env`; it is never returned.
KLIPY and the studio apply at once; desktop hosts and the T3 switch are read at startup.

## Google Calendar and Gmail

Optional. Dom can read the owner's calendar and mail and save drafts, behind per-scope switches and approval
cards, in the owner DM only. Create a Web-application OAuth client with the redirect URI
`<web.public_url>/api/integrations/google/callback`, put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`,
then press **Connect Google** in Settings → Services. See [Google Calendar and Gmail](google.md) for the scopes, the
switches and the callback behind Cloudflare Access.

## Assistant mode

Off by default. Turn it on with `/assistant on` in the owner DM or in Settings → What Dom may do → Assistant; it
applies at once, with no restart. On, Dom keeps your day in view, in your DM only:

- A **morning plan** (`[assistant] morning`, default `08:00`) with today's calendar, the reminders due today, your
  active goals, the weather and any holiday, and an **evening wrap-up** (`evening`, default `21:30`) with how the day
  went, what was missed, what is due tomorrow and a short check-in on anything left open. Each time is in your
  `timezone`; an empty time turns that message off. The calendar is included only while Google is connected and
  Calendar read is on ([Google](google.md)).
- Each message is one chat-model call in Dom's voice, sent as a normal DM message. It goes out once a day, restarts
  included, and late only within three hours of its time. A message waits while Dom is asleep (and is sent when he wakes,
  inside that window) but not while he is merely offline: like an opener, it brings him online. It covers the day it was
  scheduled for, even when it goes out just after midnight. With nothing worth saying Dom sends nothing and no model
  is called. The call counts as background work (the monthly cap and the area limits apply,
  not the daily cap) and shows as **Assistant** in Usage. A message held back by a cap is not retried that day.
- **Follow-through:** in your DM Dom is also shown reminders due within a day or overdue and goals with no update for
  a week, and may check in on them when it fits. An item counts as raised only once a delivered reply mentions it, and
  is then left out for the rest of the day.

Settings shows both times and, for each message, when it was last sent or why not. Off, Dom is the friend he is today.
Nothing from assistant mode reaches groups or friends' DMs. Booking appointments is not part of it yet.

## Menu cards

The owner cards with a Close button (`/status`, `/chats`, `/preset`, `/plugins`,
`/proposals`, `/reminders` and the like) delete themselves `menu_card_minutes` (default
45, `0` keeps them) after they were sent. Dom records each card in its database, checks
once a minute and at startup, so a restart forgets none. Telegram will not delete a
message older than 48 hours, so a card that old only loses its buttons.

## Owner commands

Send these in the owner DM. Telegram's command menu lists them after a claim.

| Command | What it does |
| --- | --- |
| `/status` | Shows presence, unread messages and usage. |
| `/chats` | Lists groups and DMs Dom knows about. |
| `/pending` | Resends pending chat approval prompts. |
| `/wake [chat_id]`, `/sleep [chat_id]` | Changes presence in the owner DM or a specified approved chat. |
| `/preset` | Changes the persona preset for one chat, or for every chat with the "Everywhere" button, which can also be Auto so Dom picks his own daily vibe ([presets](presets.md)). A chat's own preset wins. |
| `/fresh`, `/fresh undo` | Starts this chat's window from now: earlier messages leave Dom's prompt, but nothing is deleted and search still finds them. Dom is told the chat was started fresh. `/fresh undo` puts the window back as it was. The dashboard can do the same for any chat: Fresh start on the Chats page, with Undo after it (`POST /api/chats/fresh`). |
| `/proposals` | Shows pending changes for review. Use `/proposals settled` to list settled cards or `/proposals delete <id>` to archive and delete one. |
| `/reminders` | Lists, cancels and deletes reminders. Use `/reminders all` to include finished or cancelled ones, or `/reminders delete <id>`. |
| `/skills` | Lists, retires, restores and deletes skills (`/skills retire <name>`, `/skills restore <name>`, `/skills delete <name>`). |
| `/assistant` | Turns assistant mode on or off (`/assistant on`, `/assistant off`) or shows the mode, both times and when each message was last sent (`/assistant status`). |
| `/stickers` | Adds and removes sticker sets, by short name or share link. |
| `/import` | Starts an owner-reviewed memory import. |
| `/export` | Sends a zip of everything Dom remembers: `SOUL.md`, `memory/`, `private/`, skills and presets. Never `.env`, `state.db` or backups; use `dom backup` for a full copy. |
| `/tasks` | Lists, cancels and deletes finished desktop tasks (`/tasks delete <id>`). |
| `/ownwork` | Lists, cancels and deletes finished self-directed tasks (`/ownwork delete <id>`). |
| `/studio` | Says which kinds of studio media (voice notes, round videos, photos, special videos, profile photo) are available, unavailable for now, or off. |
| `/t3` | Shows T3 Code sign-in, linked machines and audit history ([T3 Code bridge](t3-bridge.md)). |
| `/plugins` | Lists, disables (retires) and deletes plugins (`/plugins delete <name>`). |
| `/update` | Shows the newest `vX.Y.Z` release with its changelog and deploys it on your confirmation (see [Deploy on a Raspberry Pi](deploy-pi.md#update-to-a-new-release)). |

In the owner DM Dom can also look for devices on your home network (`find_devices`:
mDNS and the neighbour table, nothing is scanned; mDNS needs `avahi-utils`). If a
LAN host from `config.toml` (a model `base_url`, a desktop `url` or a desktop `lan_probes` entry)
moved to a new address, he proposes the change for you to approve in `/proposals`;
it applies after Dom restarts. A host given as a `<name>.local` name follows the
device by itself.

## Optional dashboard

The owner dashboard is off by default. To publish it, put the dashboard behind
a Cloudflare Access application with an allow policy for the owner and set the
matching Access team and audience in `[web]`. Dom validates the signed
assertion too. The [dashboard deployment guide](deploy-dashboard.md) covers
tunnel ingress and a loopback-only bearer token for local testing.

The dashboard's Settings page edits the chat, fallback, media, drafting, search
and escalation routes: a primary provider and an ordered fallback chain per role, each a local
(OpenAI-compatible) or cloud endpoint. Test sends one short real call to a saved
provider. Saving tests each changed or added provider slot first, refuses the
save if any fails, writes `config.toml` atomically, keeps `config.toml.bak` and
applies to new calls without a restart. Keys stay in `.env`; the page shows only
whether each key variable is set. The same JSON is at `GET/POST /api/models`
and `POST /api/models/test`, owner only.

The page keeps secrets from leaving: a key variable can be used only for the
host `config.toml` already pairs it with (same scheme, host and port — including
Jev/triage's key on its paired host such as `https://openrouter.ai`), or as a
new name for one host. A new name must not already hold a value and must not be
the bot token, a desktop key, `KLIPY_API_KEY`, or an unpaired `JEV_API_KEY`, so
add a new key's value to `.env` after saving. Such a slot is saved untested (it cannot be called without
its key); press Test once the key is in. Every other change is test-called before it is saved, and a failing
slot is not saved. Testing or saving a local endpoint is never held back by the daily spend cap. Pairing an existing key with a new
host is a hand edit of `config.toml`. Base URLs must be HTTPS, except for
`localhost`, private addresses and local names (single-label, `.local`, `.lan`,
`.internal`, `.home.arpa`); link-local addresses are always refused. A chain
holds at most five fallbacks. Each save keeps the previous file as
`config.toml.bak`, and the first one as `config.toml.orig`. For OpenRouter, Dom
requests zero-data-retention (ZDR) routing automatically and Settings runs a real
test call before saving (refusing models with no ZDR endpoint); for
non-OpenRouter cloud providers, checking your zero-data-retention policy remains
your responsibility. Calls to local hosts that report no cost count as free
against the desktop task cap, so don't route a paid provider through a local name.

## Optional desktop

Set `[desktop].mode` to `"local"` to use Docker on the Dom host. Read
[Desktop network](desktop.md) to choose a bridge subnet and set the LAN probes.
Preview `dom desktop-setup <home>`, then apply it with `--yes` on the Docker
host under an account permitted to manage the firewall. The sandbox and probe
images must already be present. Pull the 8.4 GB sandbox image by itself from a
detached terminal session such as `tmux`. Wait for it to finish before running
other disk- or memory-heavy work.

For a desktop on another host, use `"remote"`, configure its URL and API key,
and confirm that the remote sandbox cannot reach your LAN or the Dom host. In
either mode, use `dom doctor` to check the local configuration. In the owner DM,
ask Dom to do a desktop task, then use `/tasks` to check its progress. A login
handoff needs the authenticated dashboard URL in `[web].public_url`.

## Optional plugins

Install Bubblewrap on a Linux host that supports unprivileged user namespaces
and check `dom doctor` for a passing sandbox self-test. Ask Dom in the owner DM
to draft a plugin, or place a plugin with `plugin.toml` and `main.py` under
`<home>/plugins/<name>/`. Review the code on the dashboard's Plugins page, then
approve the exact version there or from the card in the owner DM before Dom can use its tools, slash commands or dashboard panels. Network
access is an explicit per-plugin permission. Use `/plugins` or the Plugins page
to retire (disable while keeping files) or delete installed plugins. Deleting a
plugin moves its files and database snapshot into the deletion archive first,
refuses while the plugin is mid-call, removes its database record and stored
state, and refreshes the Telegram command menu.
See [Writing plugins](plugins.md) for the format and limits.

## System notices

Messages that Dom's code writes (not his chat replies) share one short style: a bold lead
line, at most two short detail lines or three bullets, and, when `[web].public_url` is set,
a **Details on the dashboard** (or **Review on dashboard**) button that opens the item.
Approvals live on the dashboard: with `public_url` set, a new proposal arrives as a short
notice with a Review on dashboard button, and `/proposals` → Details still shows the whole
card with Approve and Reject. Without `public_url` the same short text is sent with no link,
and proposals keep their full cards and buttons.

## Skills, reminders and history cleanup

Approved skills live under `<home>/skills/<name>/SKILL.md`. On the dashboard's
Work → Own work page or with `/skills`, the owner can retire a skill (its files stay on
disk, Dom stops listing or using it, and it stays retired across approved edits
until explicitly restored) or delete it. Reminders can be cancelled or deleted
on the dashboard's Reminders page and via `/reminders`. Finished desktop tasks,
finished self-directed tasks (with their saved screenshots) and settled
approval cards can be deleted from their dashboard pages and via `/tasks`,
`/ownwork` and `/proposals`. Every delete moves the item's files and writes its
database rows into `<backups>/deleted/<UTC time>-<kind>-<name or id>/` (pruned
after 30 days or 50 entries), refuses if the item is active or changed since
confirmation, and records an owner event in the owner DM history.
