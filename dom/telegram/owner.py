"""What only the owner sees: the "/" command menu, the /status overview with its wake/sleep/leave buttons and /chats."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)

from dom import memory_import, proposals
from dom.traits import load_preset

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.owner")

OWNER_COMMANDS = [
    BotCommand(command="status", description="Where Dom is and what he's doing"),
    BotCommand(command="wake", description="Wake Dom up"),
    BotCommand(command="sleep", description="Send Dom to sleep"),
    BotCommand(command="pending", description="Chats waiting for your approval"),
    BotCommand(command="chats", description="Every group and DM Dom knows about"),
    BotCommand(command="preset", description="Change Dom's persona in a chat"),
    BotCommand(command="proposals", description="Review pending changes"),
    BotCommand(command="reminders", description="List and cancel reminders"),
    BotCommand(command="stickers", description="Add or remove a sticker set"),
    BotCommand(command="import", description="Bring memories from another assistant"),
]

PROPOSALS_PER_PAGE = 10
MAX_MESSAGE = 4096  # Telegram's limit for a message's text
MAX_BUTTONS = 100  # Telegram's limit for one keyboard
MAX_MEMBER_LOOKUPS = 30  # get_chat_member_count calls per /chats, 0.2 s apart
_STATUS_ORDER = (("approved", "Approved"), ("pending_approval", "Pending"), ("ignored", "Ignored"), ("left", "Left"))
_CHATS_HEAD = "| Chat | Type | Members |\n|---|---|---|"
_ICONS = {"asleep": "🌙", "online": "☀️", "offline": "💤"}


@dataclass(frozen=True)
class View:
    """One owner message: rich-message markdown, the plain text sent instead if rich messages are refused, and its keyboard."""

    markdown: str
    plain: str
    markup: InlineKeyboardMarkup


def _cell(text: str) -> str:
    """A title or value safe inside a markdown table cell: nothing in it may be read as table or emphasis syntax."""
    for char in "\\|*_`":
        text = text.replace(char, "\\" + char)
    return text


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return [_row(header), "|" + "---|" * len(header), *map(_row, rows)]


async def send_view(bot, chat_id: int, view: View) -> None:
    """Send `view` as a rich message; if Telegram refuses rich messages, send its plain text with the same keyboard."""
    try:
        await bot.send_rich_message(chat_id=chat_id, rich_message=InputRichMessage(markdown=view.markdown), reply_markup=view.markup)
    except TelegramBadRequest as error:
        log.warning("rich message refused, sending plain text instead: %s", error)
        await bot.send_message(chat_id, view.plain, reply_markup=view.markup)


async def delete_message(bot, chat_id: int, message_id: int) -> None:
    """Remove an old owner message. Rich messages cannot be edited in place, so a change means delete and send anew."""
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:  # already deleted by the owner
        log.info("the message was already gone")


def _is_group(chat: dict) -> bool:
    return str(chat["chat_id"]).startswith("-")


def _short(title: str, limit: int = 20) -> str:
    return title if len(title) <= limit else title[: limit - 1] + "…"


async def publish_menu(bot, owner_id: int) -> None:
    """Show the owner the command menu and everyone else none. A failure is logged, never raised."""
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
    except Exception:
        log.exception("could not set the owner's command menu")
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception:
        log.exception("could not clear the default command menu")


def display_title(chat: dict, owner_id: int | None) -> str:
    chat_id = str(chat["chat_id"])
    if chat_id == str(owner_id):
        return "your DM"
    name = " ".join((chat["title"] or "").split())
    if chat_id.startswith("-"):
        return (name or chat_id)[:60]
    return f"DM with {(name or ' '.join((chat.get('peer_name') or '').split()) or chat_id)[:50]}"


def _one_line(parts: list[str], limit: int = 400) -> str:
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def record_event(app: App, text: str) -> None:
    """Note an owner action without interrupting its response if the event cannot be stored."""
    try:
        await app.store.record_owner_event(text)
    except Exception:
        log.exception("could not record an owner event")


@dataclass(frozen=True)
class PresenceChange:
    message: str
    spend_paused: bool = False


async def override_chat(app: App, chat_id: int, command: str, who: str) -> PresenceChange | None:
    chat = next((c for c in await app.store.list_approved_chats() if c["chat_id"] == str(chat_id)), None)
    if chat is None or not await app.dispatch.override(chat_id, command):
        return None
    title = display_title(chat, app.owner_id)
    if command == "/wake":
        await record_event(app, f"{who} woke Dom in {title}")
        if await app.dispatch.spend_paused(chat_id):
            return PresenceChange(f"Dom is up in {title}, but friends' chats are paused by today's spend cap", True)
        return PresenceChange(f"Dom is up in {title}")
    await record_event(app, f"{who} sent Dom to sleep in {title}")
    return PresenceChange(f"Dom is asleep in {title}")


LEAVABLE = ("approved", "pending_approval", "ignored")


async def leavable_chat(app: App, chat_id: int) -> dict | None:
    if chat_id >= 0:
        return None
    return next((
        c for c in await app.store.list_chats()
        if c["chat_id"] == str(chat_id) and c["authorization_status"] in LEAVABLE
    ), None)


async def leave_chat(app: App, chat_id: int, who: str) -> tuple[str, bool]:
    async with app.locks[chat_id]:
        chat = await leavable_chat(app, chat_id)
        if chat is None:
            return "unknown chat", False
        title = display_title(chat, app.owner_id)
        try:
            await app.bot.leave_chat(chat_id)
            result = f"Dom left {title}"
        except (TelegramBadRequest, TelegramForbiddenError):
            result = f"Dom wasn't in {title} anymore"
        except Exception as error:
            log.exception("could not leave chat %s", chat_id)
            await record_event(app, f"{who} tapped Leave on {title}: Dom couldn't leave")
            message = (getattr(error, "message", "") or str(error) or type(error).__name__)[:100]
            return f"Couldn't leave: {message}", False
        await app.store.set_chat_auth_status(chat_id, "left")
        await record_event(app, f"{who} tapped Leave on {title}: {result}")
        return result, True


async def set_preset(app: App, chat: dict, name: str) -> str | None:
    preset = load_preset(app.home, name) if name else None
    if (name and preset is None) or not await app.store.set_chat_preset(chat["chat_id"], name):
        return None
    title = display_title(chat, app.owner_id)
    label = preset.title if preset else "None"
    message = f"Preset for {title}: {label}"
    await record_event(app, message)
    return message


async def chats_summary(app: App) -> str:
    """What /chats listed, in one line: the groups with their titles, then each DM, the owner's last."""
    known = [c for c in await app.store.list_chats() if c["authorization_status"] in ("approved", "pending_approval")]
    groups = [display_title(c, app.owner_id) for c in known if _is_group(c)]
    dms = [c for c in known if not _is_group(c)]
    dms.sort(key=lambda c: c["chat_id"] == str(app.owner_id))
    parts = [f"{len(groups)} group{'s' if len(groups) != 1 else ''} ({', '.join(groups)})"] if groups else []
    return _one_line(parts + [display_title(c, app.owner_id) for c in dms]) or "no chats"


async def status_summary(app: App) -> str:
    """What /status showed, in one line: each approved chat with its state."""
    chats = await app.store.list_approved_chats()
    states = app.dispatch.peek_presence(chats)
    return _one_line([f"{display_title(c, app.owner_id)} {state}" for c, (state, _) in zip(chats, states)])


async def pending_summary(app: App) -> str:
    pending = await app.store.list_pending_chats()
    if not pending:
        return "nothing waiting"
    return f"{len(pending)} waiting ({_one_line([display_title(c, app.owner_id) for c in pending])})"


async def render_status(app: App) -> View:
    """One table row and one Wake/Sleep(/Leave, for groups) button row per approved chat, the owner's DM first,
    then by recent activity."""
    chats = await app.store.list_approved_chats()
    states = app.dispatch.peek_presence(chats)
    tz = ZoneInfo(app.config.timezone)
    pending = len(await app.store.list_pending_chats())
    models = app.config.models
    footer = "Pending approval: {} · Chat model: {} · Triage model: {}".format(
        pending, *(models[name].model if name in models else "n/a" for name in ("chat", "triage"))
    )

    count, spent = await app.dispatch.usage.today(tz)
    model = getattr(app.client, "last_model", None)
    fallback_at = getattr(app.client, "last_fallback_at", None)
    fallback = ""
    if fallback_at is not None and fallback_at.astimezone(tz).date() == app.dispatch.now().astimezone(tz).date():
        fallback = f" (fallback at {fallback_at.astimezone(tz):%H:%M})"
    footer += f"\nToday: {count} calls · ${spent:.2f}\nLast reply: {model or '—'}{fallback}"

    table, plain, buttons, used, button_count = [], [], [], len(footer) * 2 + 160, 0  # room for the footer, heading, table head and "more" line
    for chat, (state, until) in zip(chats, states):
        unread, _ = await app.store.get_unread(chat["chat_id"])
        title = display_title(chat, app.owner_id)
        when = f" until {until.astimezone(tz):%H:%M}" if until else ""
        line = f"{_ICONS[state]} {title}: {state}{when} · {unread} unread"
        cells = [_cell(title), f"{_ICONS[state]} {state}", f"{until.astimezone(tz):%H:%M}" if until else "—", str(unread)]
        row = [
            InlineKeyboardButton(text=f"☀️ {_short(title)}", callback_data=f"wake:{chat['chat_id']}"),
            InlineKeyboardButton(text="🌙 Sleep", callback_data=f"sleep:{chat['chat_id']}"),
        ]
        if _is_group(chat):
            row.append(InlineKeyboardButton(text="🚪 Leave", callback_data=f"leave:{chat['chat_id']}"))
        cost = max(len(line), len(_row(cells))) + 1
        if used + cost > MAX_MESSAGE or button_count + len(row) > MAX_BUTTONS:
            break
        used += cost
        button_count += len(row)
        table.append(cells)
        plain.append(line)
        buttons.append(row)
    more = [f"…and {len(chats) - len(table)} more"] if len(chats) > len(table) else []
    markdown = "\n".join(["**Dom's chats**", "", *_table(["Chat", "State", "Until", "Unread"], table), "", *more, *([""] if more else []), _cell(footer)])
    return View(markdown, "\n".join(["Dom's chats", "", *plain, *more, "", footer]), InlineKeyboardMarkup(inline_keyboard=buttons))


async def render_chats(app: App) -> View:
    """Every chat Dom knows, one table per status. Groups show their member count (the first MAX_MEMBER_LOOKUPS
    of them, "?" when Telegram refuses); approved and pending groups get a Leave button."""
    known = await app.store.list_chats()
    by_status = [(label, [c for c in known if c["authorization_status"] == status]) for status, label in _STATUS_ORDER]
    total = sum(len(chats) for _, chats in by_status)

    md, plain, buttons, used, lookups, shown = [], [], [], 160, 0, 0  # room for the heading and the "more" line
    full = False
    for label, chats in by_status:
        cells_list, lines = [], []
        for chat in chats:
            if full:
                break
            title = display_title(chat, app.owner_id)
            members = None
            if _is_group(chat) and label != "Left" and lookups < MAX_MEMBER_LOOKUPS:
                if lookups:
                    await app.sleep(0.2)
                lookups += 1
                try:
                    members = await app.bot.get_chat_member_count(int(chat["chat_id"]))
                except Exception:
                    log.warning("could not count the members of chat %s", chat["chat_id"])
                    members = "?"
            count = "" if members is None else f" · {members} members" if members != 1 else " · 1 member"
            cells = [_cell(title), "Group" if _is_group(chat) else "DM", "—" if members is None else str(members)]
            line = f"• {title}{count}"
            cost = max(len(line), len(_row(cells))) + 1 + (0 if cells_list else len(label) + len(_CHATS_HEAD) + 8)
            leave = label in ("Approved", "Pending") and _is_group(chat)
            if used + cost > MAX_MESSAGE or len(buttons) + leave > MAX_BUTTONS:
                full = True
                break
            used += cost
            shown += 1
            cells_list.append(cells)
            lines.append(line)
            if leave:
                buttons.append([InlineKeyboardButton(text=f"🚪 Leave {_short(title)}", callback_data=f"leave:{chat['chat_id']}")])
        if cells_list:
            md += ["", f"**{label}**", "", *_table(["Chat", "Type", "Members"], cells_list)]
            plain += ["", label, *lines]
    if total > shown:
        md += ["", f"…and {total - shown} more"]
        plain += ["", f"…and {total - shown} more"]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if not shown:
        return View("Dom doesn't know any chats yet.", "Dom doesn't know any chats yet.", markup)
    return View("\n".join(["**Dom's chats**", *md]), "\n".join(["Dom's chats", *plain]), markup)


async def _preset_buttons(app: App, text: str, choices: list[tuple[str, str]]) -> None:
    for start in range(0, len(choices), MAX_BUTTONS):
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data)]
            for label, data in choices[start:start + MAX_BUTTONS]
        ])
        await app.bot.send_message(app.owner_id, text, reply_markup=markup)


async def show_presets(app: App) -> None:
    choices = []
    for chat in await app.store.list_chats("approved"):
        preset = load_preset(app.home, chat["preset"]) if chat["preset"] else None
        title = display_title(chat, app.owner_id)
        choices.append((f"{_short(title, 30)} · {_short(preset.title, 30) if preset else 'None'}", f"preset:{chat['chat_id']}"))
    if choices:
        await _preset_buttons(app, "Choose a chat.", choices)
    else:
        await app.bot.send_message(app.owner_id, "No approved chats yet.")


async def choose_preset(app: App, chat: dict) -> None:
    prefix = f"presetset:{chat['chat_id']}:"
    choices = []
    for name in app.home.preset_names():
        data = prefix + name
        if len(data.encode("utf-8")) <= 64 and (preset := load_preset(app.home, name)) is not None:
            choices.append((_short(preset.title, 64), data))
    choices.append(("None", prefix))
    await _preset_buttons(app, f"Choose a preset for {display_title(chat, app.owner_id)}.", choices)


async def show_proposals(app: App, page: int = 1) -> None:
    rows = await app.proposals.pending()
    if not rows:
        await app.bot.send_message(app.owner_id, "No pending proposals.")
        return
    pages = (len(rows) + PROPOSALS_PER_PAGE - 1) // PROPOSALS_PER_PAGE
    page = min(max(1, page), pages)
    start = (page - 1) * PROPOSALS_PER_PAGE
    shown = rows[start:start + PROPOSALS_PER_PAGE]
    lines = [f"Pending proposals ({page}/{pages})"]
    keyboard = []
    for row in shown:
        text = " ".join(proposals.summary(row).split())
        preview = text.encode("utf-16-le")[:720].decode("utf-16-le", errors="ignore")
        controls = proposals.buttons(row["id"], numbered=len(shown) > 1)
        if preview != text:
            preview += "…"
            controls.append(InlineKeyboardButton(text=f"Details #{row['id']}", callback_data=f"prop:show:{row['id']}"))
        lines.append(preview)
        keyboard.append(controls)
    # IDs are ascending and never reused. The range refers to this page even if other proposals are decided.
    keyboard.append([InlineKeyboardButton(
        text="Approve all on this page", callback_data=f"prop:all:{shown[0]['id']}:{shown[-1]['id']}",
    )])
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton(text="Previous", callback_data=f"prop:page:{page - 1}"))
    if page < pages:
        navigation.append(InlineKeyboardButton(text="Next", callback_data=f"prop:page:{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    await app.bot.send_message(
        app.owner_id, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        disable_notification=True,
    )


async def approve_proposal_page(app: App, first: int, last: int) -> str:
    """Approve the memory facts on one page. A soul edit changes who Dom is, so it keeps its own Approve button."""
    rows = [row for row in await app.proposals.pending() if first <= row["id"] <= last]
    if not rows or len(rows) > PROPOSALS_PER_PAGE:
        return "already handled"
    facts = [row for row in rows if row["kind"] == "memory_fact"]
    souls = len(rows) - len(facts)
    held = (" 1 soul edit needs its own Approve." if souls == 1 else f" {souls} soul edits need their own Approve.") if souls else ""
    results = [await app.proposals.decide(row["id"], True) for row in facts]
    approved = results.count("Approved")
    errors = [result for result in results if result not in ("Approved", "already handled")]
    if not approved and not errors:
        return held.strip() or "already handled"
    if errors:
        return f"Approved {approved}. {len(errors)} still pending. {errors[0]}"[:180]
    return f"Approved {approved}.{held}"


async def show_reminders(app: App) -> None:
    reminders = await app.dispatch.reminders.active()
    if not reminders:
        await app.bot.send_message(app.owner_id, "No active reminders.")
        return
    tz = ZoneInfo(app.config.timezone)
    for start in range(0, len(reminders), 10):
        lines, buttons = [], []
        for row in reminders[start:start + 10]:
            due = datetime.fromisoformat(row["due_at"]).astimezone(tz)
            repeat = f", {row['repeat']}" if row["repeat"] else ""
            preview = " ".join(row["text"].split())[:130]
            lines.append(f"#{row['id']} {due:%Y-%m-%d %H:%M} ({row['importance']}{repeat}) {preview}")
            buttons.append([InlineKeyboardButton(text=f"Cancel #{row['id']}", callback_data=f"rem:cancel:{row['id']}")])
        await app.bot.send_message(
            app.owner_id, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


async def edit_stickers(app: App, args: list[str]) -> None:
    if len(args) != 2 or args[0] not in ("add", "remove"):
        await app.bot.send_message(app.owner_id, "Use /stickers add <set_name> or /stickers remove <set_name>.")
        return
    result = await change_sticker_set(app, *args)
    await app.bot.send_message(app.owner_id, result)


async def change_sticker_set(app: App, action: str, name: str) -> str:
    if action not in ("add", "remove"):
        raise ValueError("Choose Add or Remove.")
    try:
        result = await app.stickers.change(action, name)
    except (OSError, ValueError) as error:
        result = str(error) if isinstance(error, ValueError) else "Could not update stickers.toml."
    await app.store.record_owner_event(result)
    return result


async def import_memory(app: App, message: Message, text: str) -> tuple[str, str]:
    """Prepare the DM response and owner event for an import command."""
    line, _, inline = text.partition("\n")
    args = line.split()[1:]
    event = "Ran /import"
    if not args and message.document is None:
        response = (
            "Paste this prompt into ChatGPT, Claude or Muse. Reply here with /import <service> on the first "
            "line and the export below. You can also reply to the export with /import <service>, or attach "
            "a .txt or .md document with that caption.\n\n" + memory_import.export_prompt()
        )
    elif len(args) == 1 and args[0].lower() == "siri":
        response = "Siri has no exportable memory."
        event = "Ran /import siri"
    elif len(args) != 1 or args[0].lower() not in memory_import.SERVICES:
        response = "Use /import chatgpt, /import claude or /import muse."
    else:
        service = args[0].lower()
        try:
            if message.document is not None:
                source = await memory_import.download_text(app.bot, message.document)
            elif inline.strip():
                source = inline
            elif (
                (reply := message.reply_to_message) is not None
                and reply.chat.id == app.owner_id and reply.chat.type == "private" and reply.text
            ):
                source = reply.text
            else:
                raise ValueError("Reply to the export or attach a .txt or .md document with /import <service>.")
            parsed = await app.memory_import.run(service, source)
        except (OSError, ValueError) as error:
            response = str(error) if isinstance(error, ValueError) else "Could not read the import or existing notes."
            event = f"Could not import memories from {service}"
        else:
            response = memory_import.summary(service, parsed)
            event = response
    return response, event
