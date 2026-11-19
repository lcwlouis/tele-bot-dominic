"""`python -m dom` command line: init, run."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher

from dom.claim import write_claim_code
from dom.config import Config, load_config
from dom.home import Home, init_home
from dom.llm import ChatClient
from dom.store import Store
from dom.telegram.handlers import App, build_router

ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member", "message_reaction"]


def require_secret(config: Config, name: str) -> str:
    value = config.secret(name)
    if not value:
        raise SystemExit(f"{name} is not set. Put it in {config.home / '.env'} or the environment.")
    return value


def model_api_key(config: Config) -> str | None:
    endpoint = config.models["chat"]
    return require_secret(config, endpoint.api_key_env) if endpoint.api_key_env else None


def cmd_init(home: Path) -> int:
    try:
        init_home(home)
    except FileExistsError as exc:
        print(f"Refusing to initialise: {exc}")
        return 1
    code = write_claim_code(home)
    print(
        f"Created {home}\n\n"
        "Next steps:\n"
        f"  1. Copy {home / '.env.example'} to {home / '.env'} and fill in your tokens.\n"
        f"  2. Edit {home / 'SOUL.md'} to make Dom yours.\n"
        f"  3. Start the bot:  dom run {home}   (python -m dom run {home})\n"
        f"  4. In a private chat with your bot, send:  /claim {code}\n"
    )
    return 0


async def run(home: Path) -> None:
    config = load_config(home)
    if "chat" not in config.models:
        raise SystemExit(f"{home / 'config.toml'} needs a [models.chat] section.")
    bot = Bot(token=require_secret(config, "TELEGRAM_BOT_TOKEN"))
    store = Store(home, config.owner_id)
    try:
        me = await bot.get_me()
        async with httpx.AsyncClient() as http:
            client = ChatClient(config.models["chat"], model_api_key(config), http)
            app = App(config=config, home=Home(home), store=store, client=client, bot=bot, bot_username=me.username or "")
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(app))
            if config.owner_id is None:
                logging.getLogger("dom").warning(
                    "No owner yet: send /claim <code> to @%s (the code is in %s).", me.username, home / ".claim"
                )
            # aiogram stops cleanly on SIGINT/SIGTERM and closes the bot session.
            await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dom")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("init", "create a new home folder"), ("run", "run the bot")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("home", type=Path)
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.home)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args.home))
    return 0
