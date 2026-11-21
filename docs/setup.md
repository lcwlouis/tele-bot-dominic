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

The starter configuration uses OpenRouter for chat, media, Jev triage and web
search. You can point chat at a self-hosted OpenAI-compatible server that
supports tool calling. For example, replace the `[models.chat]` table with
the following and set `MODEL_API_KEY` in `.env` if the server requires it.

```toml
[models.chat]
base_url = "http://127.0.0.1:8000/v1"
model = "your-model-id"
api_key_env = "MODEL_API_KEY"
```

Omit `api_key_env` for a server without authentication. The media, triage and
search endpoints keep their own settings and credentials. To disable web search,
remove `[models.search]`. See the [configuration reference](configuration.md)
for the optional chat fallback and all defaults.

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
the owner and stays silent in that chat until approved. Use the approval buttons
or send `/pending` in the owner DM to resend pending requests.

## Owner commands

Send these in the owner DM. Telegram's command menu lists them after a claim.

| Command | What it does |
| --- | --- |
| `/status` | Shows presence, unread messages and usage. |
| `/chats` | Lists groups and DMs Dom knows about. |
| `/pending` | Resends pending chat approval prompts. |
| `/wake [chat_id]`, `/sleep [chat_id]` | Changes presence in the owner DM or a specified approved chat. |
| `/preset` | Changes the persona preset for one chat, or for every chat with the "Everywhere" button, which can also be Auto so Dom picks his own daily vibe ([presets](presets.md)). A chat's own preset wins. |
| `/proposals` | Shows pending changes for review. |
| `/reminders` | Lists and cancels reminders. |
| `/stickers` | Adds and removes sticker sets. |
| `/import` | Starts an owner-reviewed memory import. |
| `/tasks` | Lists and cancels desktop tasks. |
| `/t3` | Shows T3 Code sign-in, linked machines and audit history ([T3 Code bridge](t3-bridge.md)). |
| `/plugins` | Lists and disables plugins. |
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
access is an explicit per-plugin permission. Use `/plugins` to inspect or disable
installed plugins. See [Writing plugins](plugins.md) for the format and limits.
