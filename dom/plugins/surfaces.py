"""Plugin slash commands and dashboard panels.

Both run the approved plugin version in the same sandbox, with the same limits, as its tools.
A command gets only the text after `/name` and the chat's scope. A panel gets nothing.
Command replies are plain text; panel results are data Dom checks here and renders with its own template.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from aiogram.types import BotCommand, LinkPreviewOptions, Message

from dom.guard import check_local_private_text
from dom.models import IncomingMessage
from dom.plugins.manifest import PanelManifest, Plugin
from dom.plugins.runner import PluginBusy, PluginError, PluginsUnavailable
from dom.presence import read_habits
from dom.skills import _visible
from dom.telegram import gate

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

MAX_COMMAND_ARGS = 1024  # characters after /name
MAX_REPLY = 2000  # characters of a command's reply; Telegram allows 4096
COMMAND_COOLDOWN_SECONDS = 10.0  # per chat, outside the owner DM
MAX_MENU = 100  # Telegram's limit for one command list

# Panel schema limits. With the 16 KB output cap these keep a panel small enough to read at a glance.
MAX_PANEL_TITLE = 120
MAX_PARAGRAPHS, MAX_PARAGRAPH = 10, 1000
MAX_ITEMS, MAX_KEY, MAX_VALUE = 30, 80, 500
MAX_COLUMNS, MAX_ROWS, MAX_CELL = 8, 50, 200
PANEL_KEYS = {"title", "paragraphs", "items", "table"}


# ---- commands -------------------------------------------------------------------------------

def _arguments(text: str) -> str:
    """Everything after the `/name[@bot]` token, with outer whitespace removed and inner spacing kept."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _hint(error: Exception) -> str:
    """What the owner can do about the common failure: a plugin that tries to keep files."""
    text = str(error)
    if "Read-only file system" in text or "No space left" in text:
        return " (plugins cannot save files between runs; ask Dom for a version that keeps nothing)"
    return ""


async def _note_failure(app: App, what: str, error: Exception) -> None:
    """Tell Dom, through an event in the owner's DM, that a plugin he drafted failed, so he can offer a fix. Only
    the name and the kind of failure: plugin output stays out of the chat history."""
    why = ", because it tried to save a file" if _hint(error) else ""
    try:
        await app.store.record_owner_event(f"{what} failed when it ran{why}")
    except Exception:
        log.exception("could not record a plugin failure")


def _plain(text: str) -> str:
    """Drop control, bidi and invisible characters (newlines and tabs stay) and bound the length."""
    text = "".join(char for char in text if _visible(char)).strip()
    return text if len(text) <= MAX_REPLY else text[:MAX_REPLY - 1] + "…"


async def _reply(app: App, chat_id: int, text: str) -> None:
    # No parse mode and no link previews: the text is shown exactly as written and fetches nothing.
    await app.bot.send_message(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))


async def handle_command(app: App, message: Message, msg: IncomingMessage, command: str) -> bool:
    """Run an approved plugin's `/command`. False (the message is handled as usual) if no plugin offers it here."""
    if msg.is_bot or message.from_user is None or message.sender_chat is not None:
        return False  # bots and channel posts never trigger plugins
    name = command.removeprefix("/")
    scope = app.store.scope_for(msg.chat_id, msg.chat_type)
    entry = next(((p, c) for p, c in await app.plugins.command_entries(scope) if c.name == name), None)
    if entry is None or await gate.decide(app.store, msg, app.owner_id) != "process":
        return False
    plugin, declared = entry
    habits = read_habits(app.home)
    # peek, not state: this runs outside the chat lock, so it must not write the presence row
    asleep = app.dispatch.presence.peek(await app.store.get_presence(msg.chat_id), habits)[0] == "asleep"
    if asleep and scope != "owner_dm":
        return False  # asleep: an ordinary message he sees when he wakes, not a command he answers
    if scope != "owner_dm":
        last = app.plugin_command_times.get(msg.chat_id)
        if last is not None and monotonic() - last < COMMAND_COOLDOWN_SECONDS:
            return True  # silently dropped: a busy group must not turn Dom into a spam relay
        app.plugin_command_times[msg.chat_id] = monotonic()
    args = _arguments(msg.text)
    text = await _run_command(app, plugin, declared.name, args, scope)
    if text:
        await _reply(app, msg.chat_id, text)
    if asleep:  # the owner woke him for this
        await app.dispatch.grumble_after_command(msg.chat_id, declared.name, args)
    log.info("plugin command /%s (%s) ran in a %s chat", name, plugin.manifest.name, scope)
    return True


async def _run_command(app: App, plugin: Plugin, name: str, text: str, scope: str) -> str:
    owner = scope == "owner_dm"
    if len(text) > MAX_COMMAND_ARGS:
        return f"That's too long for /{name}: at most {MAX_COMMAND_ARGS} characters."
    try:
        check_local_private_text(app.config, app.home, [text])
    except ValueError as error:
        return f"Not sent to /{name}: {error}" if owner else f"Not sent to /{name}."
    request = {"command": name, "args": text, "scope": scope}
    try:
        with app.plugin_runner.active(plugin.manifest.name):
            folder = await app.plugins.approved_folder(plugin)
            if folder is None:
                return f"/{name} is no longer approved."
            result = await asyncio.to_thread(
                app.plugin_runner.run_request, folder, request, plugin.manifest, approved_hash=plugin.hash,
            )
    except PluginBusy:
        return "Plugins are busy. Try again in a moment."
    except PluginsUnavailable:
        return "Plugins are unavailable right now." if not owner else "Plugins are unavailable: the sandbox check failed."
    except PluginError as error:
        await _note_failure(app, f"/{name} from plugin {plugin.manifest.name}", error)
        return _plain(f"/{name} failed: {error}{_hint(error)}") if owner else f"/{name} didn't work this time."
    except (OSError, ValueError, RuntimeError):
        return f"/{name} could not run."
    return _plain(result) if isinstance(result, str) else ""


async def menu_commands(app: App) -> list[BotCommand]:
    """Approved plugin commands for the owner's "/" menu. Telegram only shows Dom's menu in the owner DM."""
    try:
        entries = await app.plugins.command_entries("owner_dm")
    except Exception:
        log.exception("could not list plugin commands")
        return []
    return [BotCommand(command=c.name, description=c.description) for _, c in entries]


# ---- panels ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class PanelData:
    title: str
    paragraphs: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def _text(value: object, maximum: int, field: str, *, lines: bool = False, numbers: bool = False) -> str:
    if numbers and type(value) in (int, float) and math.isfinite(value):  # bool is not a number here
        value = str(value)
    if not isinstance(value, str) or len(value) > maximum or not _visible(value) or (not lines and "\n" in value):
        raise ValueError(f"{field} must be visible text of up to {maximum} characters.")
    return value


def _list(value: object, maximum: int, field: str) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a list of at most {maximum} entries.")
    return value


def validate_panel(data: object) -> PanelData:
    """Check a panel result against the schema. Anything else, HTML included, is refused, never cleaned."""
    if not isinstance(data, dict) or set(data) - PANEL_KEYS:
        raise ValueError(f"A panel is an object with only these keys: {', '.join(sorted(PANEL_KEYS))}.")
    title = _text(data.get("title", ""), MAX_PANEL_TITLE, "title")
    paragraphs = tuple(
        _text(text, MAX_PARAGRAPH, "paragraph", lines=True)
        for text in _list(data.get("paragraphs", []), MAX_PARAGRAPHS, "paragraphs")
    )
    pairs = []
    for item in _list(data.get("items", []), MAX_ITEMS, "items"):
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("Each item is a [key, value] pair.")
        pairs.append((_text(item[0], MAX_KEY, "item key"), _text(item[1], MAX_VALUE, "item value", numbers=True)))
    columns, rows = (), ()
    if "table" in data:
        table = data["table"]
        if not isinstance(table, dict) or set(table) != {"columns", "rows"}:
            raise ValueError('A table is {"columns": [...], "rows": [[...], ...]}.')
        columns = tuple(_text(c, MAX_CELL, "column") for c in _list(table["columns"], MAX_COLUMNS, "columns"))
        if not columns:
            raise ValueError("A table needs at least one column.")
        rows = tuple(
            tuple(_text(cell, MAX_CELL, "cell", numbers=True) for cell in _list(row, len(columns), "row"))
            for row in _list(table["rows"], MAX_ROWS, "rows")
        )
        if any(len(row) != len(columns) for row in rows):
            raise ValueError("Every table row needs one cell per column.")
    return PanelData(title, paragraphs, tuple(pairs), columns, rows)


async def find_panel(app: App, name: str, panel_id: str) -> tuple[Plugin, PanelManifest] | None:
    return next(((p, panel) for p, panel in await app.plugins.panel_entries()
                 if p.manifest.name == name and panel.id == panel_id), None)


async def run_panel(app: App, plugin: Plugin, panel: PanelManifest) -> tuple[PanelData | None, str | None]:
    """The panel's data, or a message for the owner saying why there is none."""
    try:
        if not await asyncio.to_thread(app.plugin_runner.self_test):
            return None, "Plugins are unavailable: the sandbox check failed."
        with app.plugin_runner.active(plugin.manifest.name):
            folder = await app.plugins.approved_folder(plugin)
            if folder is None:
                return None, "This plugin is no longer approved."
            result = await asyncio.to_thread(
                app.plugin_runner.run_request, folder, {"panel": panel.id}, plugin.manifest, approved_hash=plugin.hash,
            )
    except PluginBusy:
        return None, "Plugins are busy. Refresh in a moment."
    except PluginError as error:
        await _note_failure(app, f"the {panel.id} panel of plugin {plugin.manifest.name}", error)
        return None, _plain(f"The panel failed: {error}{_hint(error)}")
    except (OSError, ValueError, RuntimeError):
        return None, "The panel could not run."
    try:
        return validate_panel(result), None
    except ValueError as error:
        return None, _plain(f"The plugin returned data Dom cannot show. {error}")
