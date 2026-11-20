"""Owner approval for changes to Dom's soul and memory."""

from __future__ import annotations

import ipaddress
import json
import logging
from datetime import timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.config import replace_host
from dom.guard import check_local_private_text
from dom.plugins.manifest import MAX_BYTES, MAX_FILES, NETWORK_NOTICE, validate_plugin
from dom.home import MAX_SOUL, MAX_SOUL_EDIT, Home
from dom.skills import Skill, skill_diff, skill_text, write_skill

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)
MEMORY_FILES = {"USER.md": "private/USER.md", "NOTES.md": "private/NOTES.md", "MEMORY.md": "memory/MEMORY.md"}
REFLECTION_SOURCE = "reflection"
REFLECTION_INTERVAL = timedelta(days=7)


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
        files = [f"{path} ({size} bytes)" for path, size in body["files"].items()]
        network = NETWORK_NOTICE if body["network"] else "Network off"
        return "\n".join([
            heading, body["description"], f"Tools: {', '.join(tools)}", network,
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
    return heading


class Proposals:
    def __init__(self, app: App):
        self.app = app

    async def create(self, kind: str, title: str, body: dict, source: str, *, notify: bool = True) -> int:
        if kind == "plugin":
            body = await self._draft_plugin(body)
            title = f"Plugin: {body['name']}"
        elif kind == "soul_edit":
            if not (isinstance(body.get("old"), str) and body["old"] and isinstance(body.get("new"), str)):
                raise ValueError("a soul edit needs old and new text")
            if problem := soul_edit_problem(self.app.home, body["old"], body["new"]):
                raise ValueError(problem)
        elif kind == "skill":
            skill = Skill(body.get("name"), body.get("description"), body.get("body"))
            self._check_skill_secrets(skill)
            body = {
                "name": skill.name, "description": skill.description, "body": skill.body,
                "old": skill_text(self.app.home, skill.name), "new": skill.render(),
            }
        elif kind == "memory_fact":
            text, file = body.get("text"), body.get("file")
            if not isinstance(text, str) or not text.strip() or not isinstance(file, str) or file not in MEMORY_FILES:
                raise ValueError("a memory fact needs a memory filename and text")
        elif kind == "lan_address":
            try:
                body = {
                    "old": str(ipaddress.IPv4Address(body.get("old"))), "new": str(ipaddress.IPv4Address(body.get("new"))),
                    "places": [str(place) for place in body.get("places", [])], "name": str(body.get("name") or ""),
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

    async def _draft_plugin(self, body: dict) -> dict:
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
        await self.app.plugins.draft(plugin)
        return {
            "name": name, "description": description, "hash": plugin.hash, "network": plugin.manifest.network,
            "tools": [{"name": tool.name, "scopes": tool.scopes} for tool in plugin.manifest.tools],
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
        if row["kind"] == "plugin":
            body = json.loads(row["body_json"])
            review = review_button(app, body)
            if review is None:
                text += f"\nOpen {plugin_review_path(body)} in the dashboard to review."
        for start in range(0, len(text), 2000):
            markup = None
            if start + 2000 >= len(text):
                keyboard = [buttons(row["id"])]
                if review is not None:
                    keyboard.insert(0, [review])
                markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            await app.bot.send_message(
                app.owner_id, text[start:start + 2000], disable_notification=True, reply_markup=markup,
            )

    async def pending(self) -> list[dict]:
        return await self.app.store.pending_proposals()

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
        app = self.app
        status = "approved" if approve else "rejected"
        at = app.dispatch.now().astimezone(timezone.utc).isoformat()
        try:
            for row in await self.pending():
                if row["id"] == proposal_id and row["kind"] == "plugin":
                    body = json.loads(row["body_json"])
                    decision = "approve" if approve else "reject"
                    return await app.plugins.decide(body["name"], body["hash"], decision)
            changed = await app.store.decide_proposal(proposal_id, status, at, self._apply)
        except (OSError, ValueError) as error:
            await app.store.record_owner_event(f"Could not approve proposal #{proposal_id}")
            return str(error) if isinstance(error, Refused) else "Could not write the proposal. It is still pending."
        if not changed:
            return "already handled"
        await app.store.record_owner_event(f"{status.capitalize()} proposal #{proposal_id}")
        return status.capitalize()

    def _check_skill_secrets(self, skill: Skill) -> None:
        config = self.app.config
        names = {"TELEGRAM_BOT_TOKEN", config.desktop.api_key_env, config.web.dev_token_env}
        names.update(model.api_key_env for model in config.models.values())
        content = f"{skill.name}\n{skill.description}\n{skill.body}"
        for name in filter(None, names):
            if (secret := config.secret(name)) and len(secret) >= 8 and secret in content:
                raise ValueError("Public skills cannot include an instance credential.")

    def _apply(self, kind: str, body: dict) -> None:
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
        if kind == "skill":
            try:
                skill = Skill(body["name"], body["description"], body["body"])
                self._check_skill_secrets(skill)
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
