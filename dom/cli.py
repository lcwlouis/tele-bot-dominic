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
from dom.importer import import_v2
from dom.jev import JevClient
from dom.llm import ChatClient
from dom.media import MediaDescriber
from dom.store import Store
from dom.telegram import executor
from dom.telegram.handlers import App, build_router

SHUTDOWN_DRAIN_SECONDS = 10
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


def cmd_import(old_workspace: Path, home: Path, owner_id: int) -> int:
    try:
        report = import_v2(old_workspace, home, owner_id)
    except (FileExistsError, ValueError) as exc:
        print(f"Import refused: {exc}")
        return 1
    print(f"Imported {len(report.imported)} files into {home}.")
    sections = (
        ("Handle-only profiles (numeric id unknown, they bind on the person's first message)", report.unresolved),
        ("Over their memory limit (copied as is; Dom will prune them)", report.over_limit),
        ("Skipped (unrecognised)", report.skipped),
        ("Kept for reference, not imported", report.legacy),
    )
    for title, items in sections:
        if items:
            print(f"\n{title}:")
            for item in items:
                print(f"  {item}")
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
            triage = config.models["triage"]
            triage_key = config.secret(triage.api_key_env) if triage.api_key_env else None
            jev = JevClient(http, triage_key, triage.model, triage.base_url) if triage_key else None
            media = config.models["media"]
            media_key = config.secret(media.api_key_env) if media.api_key_env else None
            media_describer = (
                MediaDescriber(ChatClient(media, media_key, http), bot) if media_key or not media.api_key_env else None
            )
            app = App(
                config=config, home=Home(home), store=store, client=client,
                bot=bot, bot_username=me.username or "", jev=jev, bot_id=me.id, media_describer=media_describer,
            )
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(app))
            if config.owner_id is None:
                logging.getLogger("dom").warning(
                    "No owner yet: send /claim <code> to @%s (the code is in %s).", me.username, home / ".claim"
                )
            # aiogram stops on SIGINT/SIGTERM; it waits a bounded time for running handlers.
            app.dispatch.start()
            try:
                await dispatcher.start_polling(
                    bot, allowed_updates=ALLOWED_UPDATES,
                    tasks_shutdown_timeout=SHUTDOWN_DRAIN_SECONDS, close_bot_session=False,
                )
            finally:
                await app.dispatch.close()
    finally:
        await executor.cancel_pending_deletions()
        store.close()
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dom")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("init", "create a new home folder"), ("run", "run the bot")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("home", type=Path)
    imp = sub.add_parser("import-v2", help="import a V2 workspace into a home folder")
    imp.add_argument("old_workspace", type=Path)
    imp.add_argument("home", type=Path)
    imp.add_argument("--owner-id", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.home)
    if args.command == "import-v2":
        return cmd_import(args.old_workspace, args.home, args.owner_id)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args.home))
    return 0
