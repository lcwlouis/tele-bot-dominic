"""Owner approval for changes to Dom's soul and memory."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MessageEntity

from dom.agent import unfence
from dom.config import replace_host
from dom.cross_chat import CARD_LIFETIME
from dom.deletion import STALE_MESSAGE, prune_deletions, stage_deletion, stamp_matches
from dom.guard import check_local_private_text
from dom.memory_edit import apply_proposal as apply_memory_edit, format_summary as memory_edit_summary, prepare_proposal as prepare_memory_edit
from dom.plugins.manifest import MAX_BYTES, MAX_FILES, NETWORK_NOTICE, Plugin, validate_name, validate_plugin
from dom.plugins.trial import STORAGE_NOTICE, test_run_summary
from dom.home import MAX_SOUL, MAX_SOUL_EDIT, Home
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


def buttons(proposal_id: int, *, numbered: bool = False) -> list[InlineKeyboardButton]:
    suffix = f" #{proposal_id}" if numbered else ""
    return [
        InlineKeyboardButton(text=f"Approve{suffix}", callback_data=f"prop:yes:{proposal_id}"),
        InlineKeyboardButton(text=f"Reject{suffix}", callback_data=f"prop:no:{proposal_id}"),
    ]


def plugin_review_path(body: dict) -> str:
    return f"/plugins/{body['name']}/{body['hash']}"


def review_button(app: App, body: dict) -> InlineKeyboardButton | None:
    base = app.config.web.public_url.rstrip("/")
    url = urlsplit(base)
    if url.scheme == "https" and url.netloc and url.username is None and not (url.query or url.fragment):
        return InlineKeyboardButton(text="Review on dashboard", url=base + plugin_review_path(body))
    return None


def summary(row: dict) -> str:
    body = json.loads(row["body_json"])
    heading = f"#{row['id']} {row['title']}"
    if row["kind"] == "soul_edit":
        return f"{heading}\n\nReplace:\n{body['old']}\n\nWith:\n{body['new']}"
    if row["kind"] == "skill":
        changes = "\n".join(skill_diff(body["old"], body["new"])) if body["old"] else ""
        return "\n\n".join(filter(None, (heading, changes, body["new"])))
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
        leak = "Leak check unavailable" if score is None else f"Leak score {score:g}/3"
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
            if not (isinstance(body.get("old"), str) and body["old"] and isinstance(body.get("new"), str)):
                raise ValueError("a soul edit needs old and new text")
            if problem := soul_edit_problem(self.app.home, body["old"], body["new"]):
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
        result = await self.app.plugins.draft(plugin)
        return {
            "name": plugin.manifest.name, "description": plugin.manifest.description, "hash": plugin.hash,
            "network": plugin.manifest.network, "storage": plugin.manifest.storage, "test_run": result,
            "tools": [{"name": tool.name, "scopes": tool.scopes} for tool in plugin.manifest.tools],
            "commands": [{"name": c.name, "scopes": c.scopes} for c in plugin.manifest.commands],
            "panels": [{"id": panel.id, "title": panel.title} for panel in plugin.manifest.panels],
            "files": {path: len(data) for path, data in sorted(plugin.files.items())},
        }

    async def _notify(self, proposal_id: int, kind: str, title: str, body: dict, event: str | None = None) -> None:
        app = self.app
        if app.owner_id is None:
            return
        await app.store.record_owner_event(event or f"Proposed #{proposal_id}: {title[:250]}")
        try:
            await self.send({"id": proposal_id, "kind": kind, "title": title, "body_json": json.dumps(body)})
        except Exception:
            log.exception("could not notify the owner about proposal %s", proposal_id)

    async def send(self, row: dict) -> None:
        app = self.app
        text = summary(row)
        review = None
        options = {}
        if row["kind"] == "plugin":
            options = {"parse_mode": None, "link_preview_options": LinkPreviewOptions(is_disabled=True)}
            body = json.loads(row["body_json"])
            review = review_button(app, body)
            if review is None:
                text += f"\nOpen {plugin_review_path(body)} in the dashboard to review."
        from dom.telegram.executor import FloodBudget, retry_telegram

        budget = None
        if row["kind"] == "cross_chat":
            options = {"parse_mode": None, "link_preview_options": LinkPreviewOptions(is_disabled=True)}
            budget = FloodBudget()
        sent = None
        for start in range(0, len(text), 2000):
            markup = None
            if start + 2000 >= len(text):
                keyboard = [buttons(row["id"])]
                if review is not None:
                    keyboard.insert(0, [review])
                markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            chunk = text[start:start + 2000]
            entities = None
            if row["kind"] == "memory_edit":  # model-written diff: shown verbatim, nothing in it becomes tappable
                options = {"parse_mode": None, "link_preview_options": LinkPreviewOptions(is_disabled=True)}
                entities = [MessageEntity(type="pre", offset=0, length=len(chunk.encode("utf-16-le")) // 2)]
            sent = await retry_telegram(
                lambda c=chunk, m=markup, e=entities, o=dict(options): app.bot.send_message(
                    app.owner_id, c, disable_notification=True, reply_markup=m, entities=e, **o,
                ),
                sleep=app.sleep, budget=budget,
            )
        message_id = getattr(sent, "message_id", None)
        if isinstance(message_id, int):
            await app.store.update_proposal_cards(CARDS_KEY, {str(row["id"]): [message_id]})

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

    async def settled(self, limit: int = 50) -> list[dict]:
        return await self._current(await self.app.store.settled_proposals(limit=limit))

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
        else:
            await app.store.record_owner_event(f"{status.capitalize()} proposal #{proposal_id}")
        return status.capitalize()

    def _apply(self, kind: str, body: dict) -> None:
        if kind == "cross_chat":
            return
        if kind == "soul_edit":
            if problem := soul_edit_problem(self.app.home, body["old"], body["new"]):
                raise Refused(problem)
            self.app.home.replace_soul(body["old"], body["new"])
            return
        if kind == "memory_fact":
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
