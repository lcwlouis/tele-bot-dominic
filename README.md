# Dom

Dom is a self-hosted Telegram bot that chats with friends and acts as a personal
assistant in the owner's DM. It has per-chat online and sleep hours, queues
messages while away, and keeps notes across conversations. The owner approves
new chats before Dom responds.

Dom is early. Phases 1–2 work. See the [roadmap](docs/superpowers/plans/2026-11-19-roadmap-phases-3-7.md) for planned features.

## Quick start

1. Create a bot in BotFather and get its token.
2. From the checkout, create a home for your instance.

   ```sh
   uv sync
   uv run dom init ./dom-home
   cp ./dom-home/.env.example ./dom-home/.env
   ```

3. Fill in `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY` in `dom-home/.env`.
   Set your timezone and model endpoints in `dom-home/config.toml` and edit
   `dom-home/SOUL.md` to give Dom a personality.
4. Run `uv run dom run ./dom-home`.
5. In a private chat with the bot, send `/claim <code>` using the code printed by
   `dom init`.

### BotFather settings

To receive ordinary group messages, disable privacy mode in BotFather or make
Dom a group administrator. Enable **Bot-to-Bot Communication Mode** in
BotFather if Dom should talk to other bots. The owner must approve each group.

## Owner commands

Run these from the owner's DM.

- `/status` shows presence, unread messages, and model usage.
- `/chats` lists known groups and DMs.
- `/pending` resends approval prompts.
- `/wake [chat_id]` wakes Dom in the owner's DM or an approved chat.
- `/sleep [chat_id]` sends Dom to sleep in the owner's DM or an approved chat.

See the [design spec](docs/superpowers/specs/2026-11-19-dom-core-design.md) for
chat scopes, presence, and what lives in the instance home. Implementation
plans are in [docs/superpowers/plans](docs/superpowers/plans).

## Licence

[MIT](LICENSE)
