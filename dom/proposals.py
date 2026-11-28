"""Owner approval for changes to Dom's soul and memory."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from dom.agent import unfence
from dom.config import replace_host
from dom.cross_chat import CARD_LIFETIME
from dom.deletion import STALE_MESSAGE, prune_deletions, stage_deletion, stamp_matches
from dom.guard import check_local_private_text
from dom.memory_edit import apply_proposal as apply_memory_edit, format_summary as memory_edit_summary, prepare_proposal as prepare_memory_edit
from dom.plugins.manifest import MAX_BYTES, MAX_FILES, NETWORK_NOTICE, Plugin, validate_name, validate_plugin
from dom.plugins.trial import STORAGE_NOTICE, test_run_summary
from dom import notes
from dom.home import MAX_SOUL, MAX_SOUL_EDIT, WORLD, Home, _foreign, limit_for
from dom.llm import ChatBudgetExhausted
from dom.notice import REVIEW_LABEL, link_button, send_notice
from dom.skills import BODY_LIMIT, Skill, skill_diff, skill_text, write_skill

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
MEMORY_FILES = {"USER.md": "private/USER.md", "NOTES.md": "private/NOTES.md", "MEMORY.md": "memory/MEMORY.md"}
REFLECTION_SOURCE = "reflection"
REFLECTION_INTERVAL = timedelta(days=7)
CARDS_KEY = "proposal-cards"  # {proposal id: [Telegram message ids of its cards with buttons]}
SETTLED_LABELS = {"approved": "✅ Approved", "rejected": "❌ Rejected", "expired": "Expired"}
PLUGIN_DRAFT_FORMAT = (
    "Put plugin.toml in manifest_toml and main.py in files. The manifest starts with name, description and version. "
    "The description must match the JSON description. Optional network, storage, timeout_seconds and memory_mb "
    "go before [[tools]], [[commands]] and [[panels]]. Tools declare name, description, parameters (a JSON schema "
    "of type object) and scopes. Commands declare name, description and scopes. Panels declare id and title. "
    "Scopes are owner_dm, friend_dm or group, default owner_dm. Network plugins may only use owner_dm. "
    "Use the standard library and _dom. Decorate each matching function with tool, command or panel and end "
    "main.py with run(). Tools take keyword arguments and return text. Commands take text and scope and return "
    "text. Panels take no arguments and return data with only title, paragraphs (list of text), items (pairs) "
    "or table (columns and rows). An offline owner-only plugin can opt in with storage = true and use load_storage() "
    "and save_storage(value) for one 64 KB JSON blob. Include a zero-argument self_test decorator for a function "
    "that returns text or None and raises on failure. Trials use empty disposable state and no network."
)


class Refused(ValueError):
    """The proposal remains pending because its change could not be applied."""


def soul_edit_problem(home: Home, old: str, new: str) -> str | None:
    """Why replacing `old` by `new` in SOUL.md cannot be proposed or applied now, or None."""
    if len(old) > MAX_SOUL_EDIT or len(new) > MAX_SOUL_EDIT:
        return f"a soul edit's old and new text can be at most {MAX_SOUL_EDIT} characters each"
    edited = home.edited_soul(old, new)
    if edited is None:
        return "SOUL.md must contain the old text exactly once"
    if len(edited) > MAX_SOUL:
        return f"that edit would make SOUL.md longer than {MAX_SOUL} characters"
    return None


WORLD_SOURCE = "world"  # how a world edit writes: never a chat's, so it can't pin a line or touch one a chat owns


def edits_world(body: dict) -> bool:
    """A `soul_edit` proposal may name WORLD.md (E117: one conversation about both files); without `file` it is SOUL.md."""
    return body.get("file") == WORLD


def world_edit_problem(home: Home, old: str, new: str) -> str | None:
    """Why the edit to WORLD.md cannot be proposed or applied now, or None. `old` and `new` replace a line or part of
    one; an empty `old` adds `new` as a line, an empty `new` removes the lines `old` covers. A line the owner pinned
    (a core memory) or tagged to one chat is never touched; `Home.memory_write` checks the same when it applies."""
    if len(old) > MAX_SOUL_EDIT or len(new) > MAX_SOUL_EDIT:
        return f"a world edit's old and new text can be at most {MAX_SOUL_EDIT} characters each"
    if not old.strip() and not new.strip():
        return "a world edit needs the old text, the new text or both"
    if not old and "\n" in new.strip():
        return "add one line at a time to WORLD.md"
    try:
        current = home.scoped(allow_private=False).read(WORLD)
    except (OSError, ValueError):
        return "WORLD.md is unavailable"
    if old:
        if current.count(old) != 1:
            return "WORLD.md must contain the old text exactly once"
        start = current.index(old)
        end = start + len(old.rstrip("\n"))
        touched = current.split("\n")[current.count("\n", 0, start):current.count("\n", 0, end) + 1]
        if any(notes.parse_line(line).core is not None for line in touched):
            return "that line is pinned (a core memory), so it stays as it is"
        if any(_foreign(line, WORLD_SOURCE, WORLD) for line in touched):
            return "that line belongs to one chat's notes"
        edited = current.replace(old, new, 1)
    else:
        edited = f"{current.rstrip()}\n- {new.strip().removeprefix('- ')}"
    limit = limit_for(WORLD)
    if limit is not None and notes.visible_len(edited) > limit and notes.visible_len(edited) >= notes.visible_len(current):
        return f"that edit would make WORLD.md longer than {limit} characters"
    return None


def apply_world_edit(home: Home, old: str, new: str) -> None:
    """Write an approved world edit the way the reflection's world changes write: as Dom, not the owner, so pinned and
    chat-tagged lines stay protected and the new line is Dom's, not pinned. Raises ValueError with the reason when it
    can't.

    The owner approved it, so it is the owner's decision: it neither checks nor spends Dom's weekly world-change
    allowance (dom.world_change's marker), which only limits the changes the nightly reflection makes on its own."""
    if problem := world_edit_problem(home, old, new):
        raise ValueError(problem)
    scoped = home.scoped(allow_private=False)
    if not old:
        result = scoped.memory_write(WORLD, "add", new.strip().removeprefix("- "), source=WORLD_SOURCE)
    elif not new.strip():
        result = scoped.memory_write(WORLD, "remove", "", old, source=WORLD_SOURCE)
    else:
        result = scoped.memory_write(WORLD, "replace", new, old, source=WORLD_SOURCE)
    if not result.ok:
        raise ValueError(f"WORLD.md: {result.error}")


def soul_edit_lines(body: dict) -> list[str]:
    """A soul_edit proposal's change as diff lines. A world edit that adds or removes a line shows only that side."""
    if not edits_world(body):
        return [f"- {body['old']}", f"+ {body['new']}"]
    return [*([f"- {body['old']}"] if body["old"] else []), *([f"+ {body['new']}"] if body["new"].strip() else [])]


def edit_problem(home: Home, body: dict) -> str | None:
    """The problem with a soul_edit proposal's change, for whichever file it names."""
    return (world_edit_problem if edits_world(body) else soul_edit_problem)(home, body["old"], body["new"])


PROPOSAL_KINDS = {
    "soul_edit": "A change to my soul", "skill": "A skill I drafted", "plugin": "A plugin I drafted",
    "memory_fact": "Something to remember", "memory_edit": "An edit to my notes", "lan_address": "A change of LAN address",
    "cross_chat": "A message for another chat · expires in 2 h",
}


def buttons(proposal_id: int, *, numbered: bool = False) -> list[InlineKeyboardButton]:
    suffix = f" #{proposal_id}" if numbered else ""
    return [
        InlineKeyboardButton(text=f"Approve{suffix}", callback_data=f"prop:yes:{proposal_id}"),
        InlineKeyboardButton(text=f"Reject{suffix}", callback_data=f"prop:no:{proposal_id}"),
    ]


def plugin_review_path(body: dict) -> str:
    return f"/app/system/plugins/{body['name']}/{body['hash']}"


def review_button(app: App, body: dict) -> InlineKeyboardButton | None:
    return link_button(app.config, plugin_review_path(body), REVIEW_LABEL)


def summary(row: dict) -> str:
    body = json.loads(row["body_json"])
    heading = f"#{row['id']} {row['title']}"
    if row["kind"] == "soul_edit":
        if edits_world(body):
            if not body["old"]:
                return f"{heading}\n\nAdd to WORLD.md:\n{body['new']}"
            if not body["new"].strip():
                return f"{heading}\n\nRemove from WORLD.md:\n{body['old']}"
            return f"{heading}\n\nIn WORLD.md, replace:\n{body['old']}\n\nWith:\n{body['new']}"
        return f"{heading}\n\nReplace:\n{body['old']}\n\nWith:\n{body['new']}"
    if row["kind"] == "skill":
        # The card stays short: what the skill is for and how big a change it is. The full procedure follows as a file.
        if body["old"]:
            changed = sum(1 for line in skill_diff(body["old"], body["new"]) if line[:1] in "+-" and line[:3] not in ("+++", "---"))
            change = f"Updates the existing skill: {changed} changed lines."
        else:
            change = f"New skill, {len(body['new'].splitlines())} lines."
        return "\n\n".join((heading, body.get("description") or "", f"{change} The full procedure is attached."))
    if row["kind"] == "plugin":
        tools = [f"{tool['name']} ({', '.join(tool['scopes']) or 'no scopes'})" for tool in body["tools"]]
        commands = [f"/{command['name']} ({', '.join(command['scopes'])})" for command in body.get("commands", [])]
        panels = [f"{panel['id']} ({panel['title']})" for panel in body.get("panels", [])]
        files = [f"{path} ({size} bytes)" for path, size in body["files"].items()]
        network = NETWORK_NOTICE if body["network"] else "Network off"
        return "\n".join([
            heading, body["description"], STORAGE_NOTICE if body.get("storage") else "Storage off",
            test_run_summary(body["test_run"]) if body.get("test_run") else "Test run pending.",
            f"Tools: {', '.join(tools) or 'none'}",
            f"Commands: {', '.join(commands) or 'none'}", f"Panels: {', '.join(panels) or 'none'}", network,
            f"Files: {', '.join(files)}", f"Hash: {body['hash'][:12]}",
        ])
    if row["kind"] == "lan_address":
        name = f" ({body['name']})" if body.get("name") else ""
        return (
            f"{heading}\n{body['old']} → {body['new']}{name} in {', '.join(body['places'])}.\n"
            "Dom uses the new address after his next restart."
        )
    if row["kind"] == "memory_fact":
        if row["title"] == body["text"]:
            return f"#{row['id']} {body['file']}: {body['text']}"
        return f"{heading}\n{body['file']}: {body['text']}"
    if row["kind"] == "memory_edit":
        return memory_edit_summary(heading, body)
    if row["kind"] == "cross_chat":
        score = body["leak_score"]
        if score is not None:
            leak = f"Leak score {score:g}/3"
        elif body["target_chat_id"] > 0:
            leak = "No leak check for a private chat"
        else:
            leak = "Leak check unavailable"
        return f"{heading}\n\n{body['text']}\n\nReason: {body['reason']}\n{leak} · Expires in 2 h"
    return heading


class Proposals:
    def __init__(self, app: App):
        self.app = app

    async def create(
        self, kind: str, title: str, body: dict, source: str, *, notify: bool = True, draft: bool = True,
    ) -> int:
        """`draft=False` proposes a skill as written, without the drafting model's pass."""
        from_brief = kind in ("plugin", "skill") and "brief" in body
        if from_brief:
            name, brief = body.get("name"), body.get("brief")
            if kind == "plugin":
                validate_name(name)
            if not isinstance(name, str) or not isinstance(brief, str) or not brief.strip() or len(brief) > BODY_LIMIT:
                raise ValueError(f"A draft needs a name and a brief of up to {BODY_LIMIT} characters.")
            check_local_private_text(self.app.config, self.app.home, [name, brief])
            body = await self._write_draft(kind, {"name": name, "brief": brief})
        if kind == "plugin":
            body = await self._draft_plugin(body, authored=from_brief)
            title = f"Plugin: {body['name']}"
        elif kind == "soul_edit":
            world = edits_world(body)
            if not (isinstance(body.get("old"), str) and (body["old"] or world) and isinstance(body.get("new"), str)):
                raise ValueError("a soul edit needs old and new text")
            if body.get("file") not in (None, WORLD):
                raise ValueError("a soul edit changes SOUL.md or WORLD.md")
            if problem := edit_problem(self.app.home, body):
                raise ValueError(problem)
        elif kind == "skill":
            skill = Skill(body.get("name"), body.get("description"), body.get("body"))
            check_local_private_text(self.app.config, self.app.home, [skill.name, skill.description, skill.body])
            if draft and self.app.draft_client is not None and not from_brief:
                drafted = await self._write_draft("skill", {
                    "name": skill.name, "description": skill.description, "body": skill.body,
                })
                skill = Skill(drafted.get("name"), drafted.get("description"), drafted.get("body"))
                check_local_private_text(self.app.config, self.app.home, [skill.name, skill.description, skill.body])
            body = {
                "name": skill.name, "description": skill.description, "body": skill.body,
                "old": skill_text(self.app.home, skill.name), "new": skill.render(),
            }
        elif kind == "memory_fact":
            text, file = body.get("text"), body.get("file")
            if not isinstance(text, str) or not text.strip() or not isinstance(file, str) or file not in MEMORY_FILES:
                raise ValueError("a memory fact needs a memory filename and text")
        elif kind == "memory_edit":
            title, body = prepare_memory_edit(self.app.config, self.app.home, body)
        elif kind == "lan_address":
            try:
                body = {
                    "old": str(ipaddress.IPv4Address(body.get("old"))), "new": str(ipaddress.IPv4Address(body.get("new"))),
                    "places": [str(place) for place in body.get("places", [])], "name": str(body.get("name") or ""),
                    "macs": [str(mac) for mac in body.get("macs", [])],
                }
            except ValueError:
                raise ValueError("a LAN address change needs old and new IPv4 addresses") from None
        else:
            raise ValueError("unknown proposal kind")
        at = self.app.dispatch.now().astimezone(timezone.utc).isoformat()
        proposal_id = await self.app.store.create_proposal(kind, title, body, source, at)
        assert proposal_id is not None
        if notify:
            await self._notify(proposal_id, kind, title, body)
        return proposal_id

    def _plugin_from_draft(self, body: dict) -> Plugin:
        name, description = body.get("name"), body.get("description")
        files, manifest = body.get("files"), body.get("manifest_toml")
        if (
            not isinstance(manifest, str) or len(manifest) > MAX_BYTES or not isinstance(files, dict)
            or "plugin.toml" in files or len(files) >= MAX_FILES
            or any(not isinstance(path, str) or not isinstance(text, str) or len(text) > MAX_BYTES
                   for path, text in files.items())
        ):
            raise ValueError("Supply manifest_toml separately and up to nine Python source files.")
        texts = {"plugin.toml": manifest, **files}
        plugin = validate_plugin(name, {path: text.encode("utf-8") for path, text in texts.items()})
        if description != plugin.manifest.description:
            raise ValueError("The description must match plugin.toml.")
        check_local_private_text(self.app.config, self.app.home, [*texts, *texts.values()])
        return plugin

    async def _write_draft(self, kind: str, seed: dict) -> dict:
        client = self.app.draft_client or self.app.client  # the drafting chain already ends in the chat route
        fields = (
            "name, description, manifest_toml and files (a mapping from filenames to Python source)"
            if kind == "plugin" else "name, description and body (a reusable procedure in Markdown)"
        )
        instructions = (
            f"Write a {kind} for Dom's owner to review. Use the supplied brief and produce a complete "
            f"JSON object with {fields}. Keep the name and requested behaviour. Use only public information. "
            "Leave out personal data, private notes, chat history, memory and credentials. Return only the JSON object."
        )
        if kind == "plugin":
            instructions += "\n" + PLUGIN_DRAFT_FORMAT
        try:
            reply = await client.complete([
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(seed)},
            ], [], once=True)
            content = reply["content"]
            if not isinstance(content, str):
                raise ValueError("The drafting model returned no text.")
            draft = json.loads(unfence(content))
        except ChatBudgetExhausted:
            # Not an httpx error (dispatch must see it to notify the owner), so it needs its own sentence here.
            raise ValueError(f"The drafting model's provider is out of budget or credits, so no {kind} draft was written.") from None
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, RecursionError):
            raise ValueError(f"The drafting model could not return a {kind} draft.") from None
        if not isinstance(draft, dict) or draft.get("name") != seed["name"]:
            raise ValueError(f"The drafting model must return a {kind} with the requested name.")
        return draft

    async def _draft_plugin(self, body: dict, *, authored: bool) -> dict:
        plugin = self._plugin_from_draft(body)
        if self.app.draft_client is not None and not authored:
            seed = {key: body[key] for key in ("name", "description", "manifest_toml", "files")}
            plugin = self._plugin_from_draft(await self._write_draft("plugin", seed))
        return _plugin_body(plugin, await self.app.plugins.draft(plugin))

    async def review_plugin_again(self, name: str, digest: str) -> int:
        """File a rejected or retired plugin version back in Waiting for you. The registry makes it pending and
        unreviewed, so it is approved only through the usual review of this exact hash. Raises ValueError."""
        plugin, result = await self.app.plugins.review_again(name, digest)
        body = _plugin_body(plugin, result)
        at = self.app.dispatch.now().astimezone(timezone.utc).isoformat()
        proposal_id = await self.app.store.create_proposal("plugin", f"Plugin: {name}", body, "review_again", at)
        assert proposal_id is not None
        return proposal_id

    async def _notify(self, proposal_id: int, kind: str, title: str, body: dict, event: str | None = None) -> None:
        app = self.app
        if app.owner_id is None:
            return
        await app.store.record_owner_event(event or f"Proposed #{proposal_id}: {title[:250]}")
        try:
            await self.send({"id": proposal_id, "kind": kind, "title": title, "body_json": json.dumps(body)})
        except Exception:
            log.exception("could not notify the owner about proposal %s", proposal_id)

    async def send(self, row: dict, *, full: bool = False) -> None:
        """Tell the owner about a pending proposal. With a dashboard address it is a short notice with a link: the
        dashboard's Waiting page shows the whole proposal and decides it. `full` (the owner asked for the details
        in Telegram), or no dashboard address, sends the whole card with its Approve and Reject buttons."""
        app = self.app
        path = plugin_review_path(json.loads(row["body_json"])) if row["kind"] == "plugin" else f"/app/waiting?proposal={row['id']}"
        if not full and link_button(app.config, path):
            sent = await send_notice(
                app, app.owner_id, f"Proposal #{row['id']}: {row['title']}", [PROPOSAL_KINDS.get(row["kind"], row["kind"])],
                path=path, label=REVIEW_LABEL, disable_notification=True,
            )
            if isinstance(message_id := getattr(sent, "message_id", None), int):
                await app.store.update_proposal_cards(CARDS_KEY, {str(row["id"]): [message_id]})
            return
        text = summary(row)
        review = None
        options = {}
        if row["kind"] == "plugin":
            options = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
            body = json.loads(row["body_json"])
            review = review_button(app, body)
            if review is None:
                text += f"\nOpen {plugin_review_path(body)} in the dashboard to review."
        from dom.telegram.executor import FloodBudget, send_text

        kind = row["kind"]
        if kind in ("cross_chat", "memory_edit"):
            options = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        keyboard = [buttons(row["id"])]
        if review is not None:
            keyboard.insert(0, [review])
        sent = await send_text(
            app.bot, app.owner_id, text, sleep=app.sleep,
            budget=FloodBudget() if kind == "cross_chat" else None,
            # Keep the exact cross-chat text visible for approval, including any link destinations.
            literal=kind == "cross_chat", pre=kind == "memory_edit",
            disable_notification=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), **options,
        )
        message_id = getattr(sent, "message_id", None)
        if isinstance(message_id, int):
            await app.store.update_proposal_cards(CARDS_KEY, {str(row["id"]): [message_id]})
        if kind == "skill":
            body = json.loads(row["body_json"])
            from dom.telegram.executor import retry_telegram

            document = BufferedInputFile(body["new"].encode(), filename=f"{body['name']}.md")
            await retry_telegram(
                lambda: app.bot.send_document(app.owner_id, document, disable_notification=True), sleep=app.sleep,
            )

    async def _cards(self) -> dict[str, list[int]]:
        try:
            cards = json.loads(await self.app.store.kv_get(CARDS_KEY) or "{}")
        except ValueError:
            return {}
        return cards if isinstance(cards, dict) else {}

    async def settle_cards(self) -> None:
        """Turn the buttons of every Telegram card whose proposal is no longer pending into its outcome, whichever
        way it was decided (the card, the dashboard, a plugin decision or expiry), so no stale Approve is left."""
        app = self.app
        cards = await self._cards()
        if not cards or app.owner_id is None:
            return
        statuses = await app.store.proposal_statuses([int(key) for key in cards if key.isdigit()])
        settled = {key for key in cards if statuses.get(int(key) if key.isdigit() else -1) != "pending"}
        for key in settled:
            label = SETTLED_LABELS.get(statuses.get(int(key)) if key.isdigit() else "", "Handled")
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="noop")]])
            for message_id in cards[key]:
                try:
                    from dom.telegram.executor import retry_telegram

                    await retry_telegram(
                        lambda mid=message_id, m=markup: app.bot.edit_message_reply_markup(
                            chat_id=app.owner_id, message_id=mid, reply_markup=m,
                        ),
                        sleep=app.sleep,
                    )
                except TelegramAPIError:  # too old, deleted, or already showing it
                    log.info("could not settle the card of proposal %s", key)
        if settled:
            await app.store.update_proposal_cards(CARDS_KEY, {key: cards[key] for key in settled}, remove=True)

    async def _current(self, rows: list[dict]) -> list[dict]:
        """A plugin card shows the plugin row's own trial result, which a later test run updates; the copy taken when
        the draft was proposed would otherwise say "Test run pending." for good."""
        if not any(row["kind"] == "plugin" for row in rows):
            return rows
        plugins = {plugin["name"]: plugin for plugin in await self.app.store.list_plugins()}
        current = []
        for row in rows:
            body = json.loads(row["body_json"]) if row["kind"] == "plugin" else None
            if body is not None and (plugin := plugins.get(body.get("name"))) and plugin["hash"] == body.get("hash"):
                body["test_run"] = json.loads(plugin["test_run_json"] or "null")
                row = {**row, "body_json": json.dumps(body)}
            current.append(row)
        return current

    async def pending(self) -> list[dict]:
        return await self._current(await self.app.store.pending_proposals())

    async def settled(self, limit: int = 50, include: int | None = None) -> list[dict]:
        """The latest settled proposals, plus the one `include` names when it is settled but older than the latest."""
        rows = await self.app.store.settled_proposals(limit=limit)
        if include is not None and all(row["id"] != include for row in rows):
            row = await self.app.store.get_proposal(include)
            if row is not None and row["status"] != "pending":
                rows.append(row)
        return await self._current(rows)

    async def delete(self, proposal_id: int, expected_created_at: str) -> tuple[bool, str]:
        app = self.app
        if not expected_created_at:
            return False, STALE_MESSAGE
        row = await app.store.get_proposal(proposal_id)
        if row is None:
            return False, f"Proposal #{proposal_id} not found."
        if row["status"] == "pending":
            return False, f"Proposal #{proposal_id} is still pending. Approve or reject it before deleting."
        if not stamp_matches(row["created_at"], expected_created_at):
            return False, STALE_MESSAGE
        event_at = datetime.now(timezone.utc).isoformat()
        event_text = f"Deleted proposal #{proposal_id}"
        try:
            archive = await asyncio.to_thread(
                stage_deletion,
                app.home.root,
                "proposal",
                proposal_id,
                event_text=event_text,
                event_at=event_at,
                rows={"proposal": row},
            )
        except (OSError, ValueError) as error:
            log.warning("could not archive proposal %s: %s", proposal_id, type(error).__name__)
            return False, "Could not archive proposal before deleting."
        try:
            result = await app.store.delete_settled_proposal(
                proposal_id, expected_created_at, event_text, event_at, cards_key=CARDS_KEY,
            )
        except sqlite3.Error as error:
            log.warning("could not delete proposal %s: %s", proposal_id, type(error).__name__)
            await asyncio.to_thread(archive.rollback)
            return False, "Could not delete proposal."
        if result != "deleted":
            await asyncio.to_thread(archive.rollback)
            if result == "stale":
                return False, STALE_MESSAGE
            if result == "pending":
                return False, f"Proposal #{proposal_id} is still pending. Approve or reject it before deleting."
            if result == "in_flight":
                return False, "Let the delivery finish before deleting its approval."
            return False, f"Proposal #{proposal_id} not found."
        await asyncio.to_thread(archive.commit)
        await asyncio.to_thread(prune_deletions, app.home.root)
        return True, f"Deleted proposal #{proposal_id}."

    async def reflection_due(self) -> bool:
        since = self.app.dispatch.now().astimezone(timezone.utc) - REFLECTION_INTERVAL
        return not await self.app.store.has_recent_proposal("soul_edit", REFLECTION_SOURCE, since.isoformat())

    async def from_reflection(self, edit: object) -> None:
        if not isinstance(edit, dict):
            return
        old, new, why = (edit.get(key) for key in ("old", "new", "why"))
        if not all(isinstance(value, str) for value in (old, new, why)) or not old or old == new:
            return
        if problem := soul_edit_problem(self.app.home, old, new):
            log.info("dropped a reflection soul edit: %s", problem)
            return
        title = why.strip()[:250] or "Soul edit"
        body = {"old": old, "new": new}
        now = self.app.dispatch.now().astimezone(timezone.utc)
        proposal_id = await self.app.store.create_proposal(
            "soul_edit", title, body, REFLECTION_SOURCE, now.isoformat(),
            since=(now - REFLECTION_INTERVAL).isoformat(),
        )
        if proposal_id is not None:
            # The reason is model-written from chat content; the event row goes back into Dom's own prompt.
            await self._notify(proposal_id, "soul_edit", title, body, event=f"Dom proposed a soul edit (#{proposal_id})")

    async def decide(self, proposal_id: int, approve: bool) -> str:
        outcome = await self._decide(proposal_id, approve)
        await self.settle_cards()
        return outcome

    async def _decide(self, proposal_id: int, approve: bool) -> str:
        app = self.app
        status = "approved" if approve else "rejected"
        now = app.dispatch.now().astimezone(timezone.utc)
        at = now.isoformat()
        row = await app.store.get_proposal(proposal_id)
        if row is None or row["status"] != "pending":
            return "already handled"
        body = json.loads(row["body_json"])
        try:
            if row["kind"] == "plugin":
                return await app.plugins.decide(body["name"], body["hash"], "approve" if approve else "reject")
            if row["kind"] == "cross_chat" and now - datetime.fromisoformat(row["created_at"]) >= CARD_LIFETIME:
                await app.store.change_cross_chat(body["item_id"], "awaiting_approval", "dropped", at, reason="expired")
                return "Expired"
            changed = await app.store.decide_proposal(proposal_id, status, at, self._apply)
        except (OSError, ValueError) as error:
            await app.store.record_owner_event(f"Could not approve proposal #{proposal_id}")
            return str(error) if isinstance(error, Refused) else "Could not write the proposal. It is still pending."
        if not changed:
            return "already handled"
        if row["kind"] == "cross_chat":
            cross_chat = app.dispatch.cross_chat
            if approve:
                await cross_chat.approved(body["item_id"])
            else:
                await cross_chat.rejected(body["item_id"])
        elif row["kind"] == "memory_fact" and approve and body.get("import_batch"):
            await app.memory_import.stage(body["text"], source=row["source"])
            await app.store.record_owner_event(f"{status.capitalize()} proposal #{proposal_id}")
        else:
            await app.store.record_owner_event(f"{status.capitalize()} proposal #{proposal_id}")
        return status.capitalize()

    def _apply(self, kind: str, body: dict) -> None:
        if kind == "cross_chat":
            return
        if kind == "soul_edit":
            if edits_world(body):
                try:
                    apply_world_edit(self.app.home, body["old"], body["new"])
                except ValueError as error:
                    raise Refused(str(error)) from None
                return
            if problem := soul_edit_problem(self.app.home, body["old"], body["new"]):
                raise Refused(problem)
            self.app.home.replace_soul(body["old"], body["new"])
            return
        if kind == "memory_fact":
            if body.get("import_batch"):
                return  # staged for downtime organization in decide() so bulk imports never hit USER.md's cap
            rel = MEMORY_FILES[body["file"]]
            home = self.app.home.scoped(allow_private=rel.startswith("private/"))
            result = home.memory_write(rel, "add", body["text"])
            if not result.ok:
                raise Refused(f"{body['file']}: {result.error}")
            return
        if kind == "memory_edit":
            try:
                apply_memory_edit(self.app.config, self.app.home, body)
            except ValueError as error:
                raise Refused(str(error)) from None
            return
        if kind == "skill":
            try:
                skill = Skill(body["name"], body["description"], body["body"])
                check_local_private_text(self.app.config, self.app.home, [skill.name, skill.description, skill.body])
                write_skill(self.app.home, skill, body["old"])
            except ValueError as error:
                raise Refused(str(error)) from None
            return
        if kind == "lan_address":
            try:
                replace_host(self.app.home.root, body["old"], body["new"])
            except ValueError as error:
                raise Refused(str(error)) from None
            return
        raise Refused("This proposal kind is unavailable")

    async def expire(self) -> None:
        now = self.app.dispatch.now().astimezone(timezone.utc)
        await self.app.store.expire_proposals((now - timedelta(days=14)).isoformat(), now.isoformat())
        await self.settle_cards()


def _plugin_body(plugin, result: dict | None) -> dict:
    """A plugin proposal's body: what its review card and Waiting for you show."""
    return {
        "name": plugin.manifest.name, "description": plugin.manifest.description, "hash": plugin.hash,
        "network": plugin.manifest.network, "storage": plugin.manifest.storage, "test_run": result,
        "tools": [{"name": tool.name, "scopes": tool.scopes} for tool in plugin.manifest.tools],
        "commands": [{"name": c.name, "scopes": c.scopes} for c in plugin.manifest.commands],
        "panels": [{"id": panel.id, "title": panel.title} for panel in plugin.manifest.panels],
        "files": {path: len(data) for path, data in sorted(plugin.files.items())},
    }
