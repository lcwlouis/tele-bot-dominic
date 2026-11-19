"""The Dom command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import subprocess
import tarfile
from functools import partial
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher

from dom import habits, reflection
from dom.backup import backup, restore
from dom.claim import write_claim_code
from dom.config import Config, load_config
from dom.desktop import network as desktop_network
from dom.desktop.sandbox import configured_sandbox
from dom.home import Home, init_home
from dom.importer import import_v2
from dom.jev import JevClient
from dom.llm import ChatClient, FallbackClient
from dom.media import MediaDescriber
from dom.search import SearchClient
from dom.store import Store
from dom.telegram import executor, owner
from dom.telegram.handlers import App, build_router
from dom.usage import UsageRecorder
from dom.web.app import serving
from dom.web.auth import Auth

SHUTDOWN_DRAIN_SECONDS = 10
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member", "message_reaction"]


def require_secret(config: Config, name: str) -> str:
    value = config.secret(name)
    if not value:
        raise SystemExit(f"{name} is not set. Put it in {config.home / '.env'} or the environment.")
    return value


def model_api_key(config: Config, name: str = "chat") -> str | None:
    endpoint = config.models[name]
    return require_secret(config, endpoint.api_key_env) if endpoint.api_key_env else None


def chat_client(
    config: Config, http: httpx.AsyncClient, usage: UsageRecorder | None = None,
) -> FallbackClient:
    """The chat model, with `[models.chat_fallback]` (if configured) used when it is down."""
    primary = ChatClient(config.models["chat"], model_api_key(config), http, usage=usage)
    fallback = None
    if "chat_fallback" in config.models:
        fallback = ChatClient(config.models["chat_fallback"], model_api_key(config, "chat_fallback"), http, usage=usage)
    return FallbackClient(primary, fallback)


def search_client(
    config: Config, http: httpx.AsyncClient, usage: UsageRecorder | None = None,
) -> SearchClient | None:
    endpoint = config.models.get("search")
    if endpoint is None or not endpoint.api_key_env:
        return None
    key = config.secret(endpoint.api_key_env)
    return SearchClient(endpoint, key, http, usage=usage) if key else None


def cmd_init(home: Path, timezone: str | None = None) -> int:
    try:
        init_home(home, timezone)
    except FileExistsError as exc:
        print(f"Refusing to initialise: {exc}")
        return 1
    code = write_claim_code(home)
    print(
        f"Created {home} (timezone {load_config(home).timezone}; change it in config.toml)\n\n"
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
    try:
        auth = Auth(config) if config.web.enabled else None
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if "chat" not in config.models:
        raise SystemExit(f"{home / 'config.toml'} needs a [models.chat] section.")
    bot = Bot(token=require_secret(config, "TELEGRAM_BOT_TOKEN"))
    store = Store(home, config.owner_id)
    sandbox = configured_sandbox(config)
    try:
        me = await bot.get_me()
        async with httpx.AsyncClient() as http:
            usage = UsageRecorder(store)
            client = chat_client(config, http, usage)
            triage = config.models["triage"]
            triage_key = config.secret(triage.api_key_env) if triage.api_key_env else None
            jev = JevClient(http, triage_key, triage.model, triage.base_url, usage=usage) if triage_key else None
            media = config.models["media"]
            media_key = config.secret(media.api_key_env) if media.api_key_env else None
            media_describer = (
                MediaDescriber(ChatClient(media, media_key, http, usage=usage), bot) if media_key or not media.api_key_env else None
            )
            app = App(
                config=config, home=Home(home), store=store, client=client,
                bot=bot, bot_username=me.username or "", jev=jev, bot_id=me.id, media_describer=media_describer,
                search_client=search_client(config, http, usage), sandbox=sandbox,
            )
            await app.stickers.refresh()
            app.dispatch.jobs.register("habits", habits.due, partial(habits.run, app))
            app.dispatch.jobs.register("reflection", reflection.due, partial(reflection.run, app))
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(app))
            if config.owner_id is not None:
                await owner.publish_menu(bot, config.owner_id)
            else:
                logging.getLogger("dom").warning(
                    "No owner yet: send /claim <code> to @%s (the code is in %s).", me.username, home / ".claim"
                )
            # aiogram stops on SIGINT/SIGTERM; it waits a bounded time for running handlers.
            async with serving(app, auth):
                app.dispatch.start()
                try:
                    await dispatcher.start_polling(
                        bot, allowed_updates=ALLOWED_UPDATES,
                        tasks_shutdown_timeout=SHUTDOWN_DRAIN_SECONDS, close_bot_session=False,
                    )
                finally:
                    await app.dispatch.close()
    finally:
        if sandbox is not None:
            await sandbox.close()
        await executor.cancel_pending_deletions()
        store.close()
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dom")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("init", "create a new home folder"), ("run", "run the bot")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("home", type=Path)
        if name == "init":
            p.add_argument("--timezone", help="IANA timezone, e.g. Asia/Singapore (default: this machine's)")
    imp = sub.add_parser("import-v2", help="import a V2 workspace into a home folder")
    imp.add_argument("old_workspace", type=Path)
    imp.add_argument("home", type=Path)
    imp.add_argument("--owner-id", type=int, required=True)
    backup_cmd = sub.add_parser("backup", help="archive a home and its SQLite state")
    backup_cmd.add_argument("home", type=Path)
    backup_cmd.add_argument("--keep", type=int, default=7)
    backup_cmd.add_argument("--to", type=Path)
    restore_cmd = sub.add_parser("restore", help="restore an archive into an empty home")
    restore_cmd.add_argument("archive", type=Path)
    restore_cmd.add_argument("home", type=Path)
    setup_cmd = sub.add_parser("desktop-setup", help="create the local desktop network and firewall rules")
    setup_cmd.add_argument("home", type=Path)
    check_cmd = sub.add_parser("desktop-check", help="check connectivity from the desktop network")
    check_cmd.add_argument("home", type=Path)
    check_cmd.add_argument("lan_address", help="IPv4 address of a known reachable LAN service")
    check_cmd.add_argument("--lan-port", type=int, default=443)
    check_cmd.add_argument("--public-address", default="1.1.1.1")
    args = parser.parse_args(argv)
    if args.command in ("desktop-setup", "desktop-check"):
        try:
            config = load_config(args.home)
            if args.command == "desktop-setup":
                desktop_network.setup(config)
            else:
                desktop_network.check(config, args.lan_address, args.lan_port, args.public_address)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"{args.command} failed: {exc}")
            return 1
        return 0
    if args.command == "init":
        return cmd_init(args.home, getattr(args, "timezone", None))
    if args.command == "import-v2":
        return cmd_import(args.old_workspace, args.home, args.owner_id)
    if args.command in ("backup", "restore"):
        try:
            if args.command == "backup":
                archive = backup(args.home, keep=args.keep, destination=args.to)
                print(f"{archive} ({archive.stat().st_size} bytes)")
            else:
                restore(args.archive, args.home)
                print(f"Restored {args.home}")
        except (OSError, EOFError, ValueError, sqlite3.Error, tarfile.TarError) as exc:
            print(f"{args.command.capitalize()} failed: {exc}")
            return 1
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args.home))
    return 0
