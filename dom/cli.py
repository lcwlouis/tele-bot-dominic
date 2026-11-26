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

from dom import doctor, habits, journal_archive, reflection, release, service, setup, tracer
from dom.backup import backup, restore
from dom.claim import write_claim_code
from dom.config import Config, load_config
from dom.desktop import network as desktop_network
from dom.embedding import routed_embedding_client
from dom.home import Home, init_home
from dom.gifs import GifSearch
from dom.reachability import check_reachability
from dom.studio.client import StudioClient
from dom.studio.cloud import CloudStudio
from dom.importer import import_v2
from dom.jev import JevClient
from dom.knowledge import KnowledgeIndex
from dom.llm import FallbackClient, routed_client
from dom.media import MediaDescriber
from dom.search import SearchRouter, routed_search_client
from dom.store import Store
from dom.t3 import auth as t3_auth, service as t3_service
from dom.t3.client import T3Unavailable
from dom.telegram import executor, owner
from dom.telegram.handlers import App, build_router
from dom.usage import UsageRecorder
from dom.web.app import serving
from dom.web.auth import Auth

SHUTDOWN_DRAIN_SECONDS = 10
# `dom run`'s exit code after the dashboard's Restart (EX_TEMPFAIL). Non-zero, so systemd's Restart=on-failure and
# launchd's KeepAlive SuccessfulExit=false both start Dom again.
RESTART_EXIT = 75
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
    return routed_client(config, "chat", http, usage)


def search_client(
    config: Config, http: httpx.AsyncClient, usage: UsageRecorder | None = None,
) -> SearchRouter | None:
    return routed_search_client(config, http, usage)


def gif_search(config: Config, http: httpx.AsyncClient) -> GifSearch | None:
    key = config.secret("KLIPY_API_KEY")
    return GifSearch(key, http) if key else None


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


async def run(home: Path) -> bool:
    """Run Dom until it stops; True when the owner asked for a restart from the dashboard."""
    config = load_config(home)
    try:
        auth = Auth(config) if config.web.enabled else None
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if "chat" not in config.models:
        raise SystemExit(f"{home / 'config.toml'} needs a [models.chat] section.")
    bot = Bot(token=require_secret(config, "TELEGRAM_BOT_TOKEN"))
    store = Store(home, config.owner_id)
    app = None
    try:
        me = await bot.get_me()
        async with httpx.AsyncClient() as http, httpx.AsyncClient(trust_env=False) as studio_http:
            usage = UsageRecorder(store)
            client = chat_client(config, http, usage)
            triage = config.models["triage"]
            triage_key = config.secret(triage.api_key_env) if triage.api_key_env else None
            jev = JevClient(http, triage_key, triage.model, triage.base_url, usage=usage) if triage_key else None
            media = routed_client(config, "media", http, usage)
            studio = config.studio
            studio_token = config.secret(studio.token_env) if studio.enabled and studio.base_url else None
            app = App(
                config=config, home=Home(home), store=store, client=client,
                bot=bot, bot_username=me.username or "", jev=jev, bot_id=me.id,
                media_describer=MediaDescriber(media, bot) if media.available else None,
                search_client=search_client(config, http, usage), gifs=gif_search(config, http),
                studio=StudioClient(studio, studio_token, studio_http) if studio_token else None,
                studio_http=studio_http,
                cloud_studio=CloudStudio(studio_http, usage),
                draft_client=routed_client(config, "drafting", http, usage),
                escalation_client=routed_client(config, "escalation", http, usage),
                embedding_client=routed_embedding_client(config, http, usage),
            )
            await app.stickers.refresh()
            from dom.deletion import recover_deletions
            await recover_deletions(app.home.root, app.store)
            await app.plugins.cleanup_drafts()
            if app.desktop_worker is not None:
                await app.desktop_worker.recover()
            app.dispatch.jobs.register("habits", habits.due, partial(habits.run, app))
            app.dispatch.jobs.register("reflection", reflection.due, partial(reflection.run, app))
            # Self-directed work is planned in the same nightly window, after the reflection (the daydream).
            app.dispatch.jobs.register("self-work", reflection.due, app.self_work.plan)
            # Noticings and goal progress run once nightly in the same reflection window (at most one small call).
            app.dispatch.jobs.register("noticings", reflection.due, app.noticings.reflect)
            app.dispatch.jobs.register("memory-upkeep", journal_archive.due, partial(journal_archive.run, app))
            app.dispatch._spawn(app.knowledge.run_background())
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(app))
            if config.owner_id is not None:
                await owner.refresh_menu(app)
            else:
                logging.getLogger("dom").warning(
                    "No owner yet: send /claim <code> to @%s (the code is in %s).", me.username, home / ".claim"
                )
            # aiogram stops on SIGINT/SIGTERM; it waits a bounded time for running handlers.
            async with serving(app, auth):
                try:
                    await app.media_files.reconcile()  # before anything new is saved
                except Exception:
                    logging.getLogger("dom").warning("could not reconcile the kept media", exc_info=True)
                try:
                    app.traces = tracer.open_writer(home, lambda: app.config)
                    app.traces.start()
                    await app.traces.prune()
                except Exception:  # the tracer is a debugging aid: Dom runs without it rather than not at all
                    logging.getLogger("dom").warning("could not open the trace store; turns are not traced", exc_info=True)
                    app.traces = None
                app.dispatch.start()
                app.dispatch._spawn(check_reachability(app))
                if app.cloud_studio is not None:
                    await app.cloud_studio.reconcile_pending(config)
                try:
                    release.mark_ready(home)  # a deploy in progress waits for this before it keeps the release
                except OSError:
                    logging.getLogger("dom").warning("could not write the ready marker for deploys")
                owner.watch_update(app)
                try:
                    await dispatcher.start_polling(
                        bot, allowed_updates=ALLOWED_UPDATES,
                        tasks_shutdown_timeout=SHUTDOWN_DRAIN_SECONDS, close_bot_session=False,
                    )
                finally:
                    if app.cloud_studio is not None:
                        await app.cloud_studio.aclose()
                    await app.self_work.close()
                    if app.desktop_worker is not None:
                        await app.desktop_worker.close()
                    await app.dispatch.close()
                    if app.traces is not None:
                        await app.traces.close()
                        app.traces.store.close()
    finally:
        if app is not None and app.desktop_worker is not None:
            await app.desktop_worker.close()
        await executor.cancel_pending_deletions()
        store.close()
        await bot.session.close()
    return app is not None and app.restart_requested


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dom")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("init", "create a new home folder"), ("run", "run the bot")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("home", type=Path)
        if name == "init":
            p.add_argument("--timezone", help="IANA timezone, e.g. Asia/Singapore (default: this machine's)")
    setup_wizard = sub.add_parser("setup", help="choose where the media studio runs and write its settings")
    setup_wizard.add_argument("home", type=Path)
    setup_wizard.add_argument("--studio", choices=setup.STUDIO_PATHS, help="skip the question: local, cloud or apis")
    setup_wizard.add_argument("--studio-url", default="", help="the studio's address, for local or cloud")
    imp = sub.add_parser("import-v2", help="import a V2 workspace into a home folder")
    imp.add_argument("old_workspace", type=Path)
    imp.add_argument("home", type=Path)
    imp.add_argument("--owner-id", type=int, required=True)
    backup_cmd = sub.add_parser("backup", help="archive a home and its SQLite state")
    backup_cmd.add_argument("home", type=Path)
    backup_cmd.add_argument("--keep", type=int, default=7)
    backup_cmd.add_argument("--to", type=Path)
    doctor_cmd = sub.add_parser("doctor", help="check instance configuration and health")
    doctor_cmd.add_argument("home", type=Path)
    doctor_cmd.add_argument("--online", action="store_true", help="also contact Telegram and model providers")
    service_cmd = sub.add_parser("install-service", help="install systemd user units for Dom and its backups")
    service_cmd.add_argument("home", type=Path)
    service_cmd.add_argument("--yes", action="store_true", help="write and enable the units")
    service_cmd.add_argument("--force", action="store_true", help="replace existing units with differing contents")
    connect_cmd = sub.add_parser("t3-connect", help="sign in to T3 Code with Supervised access")
    connect_cmd.add_argument("home", type=Path)
    connect_cmd.add_argument("--yes", action="store_true")
    t3_cmd = sub.add_parser("t3-service", help="install the pinned T3 Code systemd user service")
    t3_cmd.add_argument("home", type=Path)
    t3_cmd.add_argument("--yes", action="store_true", help="write and enable the service")
    deploy_cmd = sub.add_parser(
        "deploy", help="deploy a vX.Y.Z release tag: stage, back up, switch, check health, roll back on failure",
    )
    deploy_cmd.add_argument("home", type=Path)
    deploy_cmd.add_argument("tag", help="release tag, exactly vX.Y.Z")
    deploy_cmd.add_argument("--root", type=Path, help="folder holding app and releases (default: this release's)")
    deploy_cmd.add_argument("--expect", help="refuse unless the tag is still this commit")
    deploy_cmd.add_argument("--health-seconds", type=float, default=release.HEALTH_SECONDS)
    restore_cmd = sub.add_parser("restore", help="restore an archive into an empty home")
    restore_cmd.add_argument("archive", type=Path)
    restore_cmd.add_argument("home", type=Path)
    setup_cmd = sub.add_parser("desktop-setup", help="create the local desktop network and firewall rules")
    setup_cmd.add_argument("home", type=Path)
    setup_cmd.add_argument("--yes", action="store_true", help="apply the network and firewall changes")
    check_cmd = sub.add_parser("desktop-check", help="check connectivity from the desktop network")
    check_cmd.add_argument("home", type=Path)
    check_cmd.add_argument("lan_address", help="IPv4 address or <name>.local of a known reachable LAN service")
    check_cmd.add_argument("--lan-port", type=int, default=443)
    check_cmd.add_argument("--public-address", default=None, help="override [desktop] probe_control (port 443)")
    index_cmd = sub.add_parser("rebuild-index", help="rebuild the graph index from notes and message history")
    index_cmd.add_argument("home", type=Path)
    args = parser.parse_args(argv)
    if args.command in ("desktop-setup", "desktop-check"):
        try:
            config = load_config(args.home)
            if args.command == "desktop-setup":
                print(
                    f"Ensure the Docker bridge {config.desktop.network!r} uses {config.desktop.subnet} "
                    f"and install {desktop_network.CHAIN} firewall rules in DOCKER-USER and INPUT.",
                    flush=True,
                )
                if not args.yes:
                    print("Dry run. Add --yes to apply the network and firewall changes.")
                    return 0
                desktop_network.setup(config)
            else:
                desktop_network.check(config, args.lan_address, args.lan_port, args.public_address)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"{args.command} failed: {exc}")
            return 1
        return 0
    if args.command == "setup":
        return setup.run_setup(args.home, path=args.studio, base_url=args.studio_url)
    if args.command == "doctor":
        return doctor.run(args.home, online=args.online)
    if args.command == "rebuild-index":
        store = Store(args.home, load_config(args.home).owner_id)
        try:
            result = asyncio.run(KnowledgeIndex(store, Home(args.home)).rebuild())
        finally:
            store.close()
        print(f"Indexed {result['facts']} facts and {result['entities']} entities from {result['files']} files.")
        return 0
    if args.command == "deploy":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            release.release_tag(args.tag)
            layout = release.Layout(args.root.resolve()) if args.root else release.Layout.installed()
            repo_url = load_config(args.home).update.repo_url
        except (release.DeployError, OSError, ValueError) as exc:
            print(f"deploy refused: {exc}")
            return 1
        result = release.deploy(
            layout, args.home.resolve(), repo_url, args.tag, expect=args.expect, health_seconds=args.health_seconds,
        )
        print(result.message)
        return 0 if result.ok else 1
    if args.command == "install-service":
        try:
            return service.install(args.home, yes=args.yes, force=args.force)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"install-service failed: {exc}")
            return 1
    if args.command == "t3-connect":
        if not args.yes:
            print("Dry run. Add --yes to sign in with Supervised access and save t3-token.json.")
            return 0
        try:
            t3_auth.connect_home(args.home, load_config(args.home).t3)
        except (T3Unavailable, ValueError) as exc:
            print(f"t3-connect failed: {exc}")
            return 1
        print("Signed in to T3 Code with Supervised access.")
        return 0
    if args.command == "t3-service":
        try:
            return t3_service.install(load_config(args.home).t3, yes=args.yes)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"t3-service failed: {exc}")
            return 1
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
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would put the KLIPY key in the log
    return RESTART_EXIT if asyncio.run(run(args.home)) else 0
