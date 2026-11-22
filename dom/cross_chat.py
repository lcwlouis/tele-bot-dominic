"""Persist cross-chat requests and deliver them outside the source turn."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat

from dom import guard
from dom.actions import Actions, Bubble
from dom.telegram import executor
from dom.usage import ModelCallSkipped, check_model_allowed, model_request

if TYPE_CHECKING:
    from dom.dispatch import Dispatcher
    from dom.models import IncomingMessage

log = logging.getLogger(__name__)
CARD_LIFETIME = timedelta(hours=2)
MAX_TEXT = 1000
MAX_REASON = 300
CHAT_GONE = re.compile(r"chat not found|kicked|not a member|not enough rights|have no rights", re.IGNORECASE)


class DeliveryRefused(Exception):
    pass


def validate_text(text: object, reason: object) -> tuple[str, str]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError(f"Write one text message of up to {MAX_TEXT} characters.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise ValueError("Give a short reason.")
    try:
        text.encode("utf-8")
        reason.encode("utf-8")
    except UnicodeError:
        raise ValueError("The message or reason contains invalid characters.") from None
    return text, reason


class CrossChat:
    def __init__(self, dispatch: Dispatcher):
        self.dispatch = dispatch
        self.app = dispatch.app
        self.store = self.app.store
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._sending = False

    def now(self) -> datetime:
        return self.dispatch.now().astimezone(timezone.utc)

    def day_start(self, at: datetime) -> str:
        local = at.astimezone(self.dispatch.presence.tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()

    async def alert(self, msg: IncomingMessage) -> None:
        if not msg.mentions_owner or msg.is_bot or msg.chat_type == "private":
            return
        name = " ".join(msg.sender_name.split())[:128] or "Someone"
        title = " ".join(msg.chat_title.split())[:128] or "a group"
        text = f'👋 {name} is asking for you in "{title}"'
        if msg.message_url:
            text += f" · {msg.message_url}"
        if await self.store.add_owner_delivery(
            "alert", msg.chat_id, text, "", self.now(), sender=msg.sender_id, msg_id=msg.msg_id,
        ):
            self._wake.set()

    async def tell_owner(self, source: int, text: object, reason: object) -> str:
        text, reason = validate_text(text, reason)
        if not await self.store.add_owner_delivery("owner_note", source, text, reason, self.now()):
            raise ValueError("You've already let the owner know recently. Give him some time.")
        self._wake.set()
        return "Sent to the owner."

    async def _target(self, query: object) -> tuple[int, str]:
        if not isinstance(query, str) and type(query) is not int:
            raise ValueError("Give a chat title or ID.")
        chats = [
            (int(row["chat_id"]), row["title"] or row["peer_name"] or row["chat_id"])
            for row in await self.store.list_approved_chats() if int(row["chat_id"]) != self.app.owner_id
        ]
        key = str(query).strip().casefold()
        for chat, title in chats:
            if key == str(chat):
                return chat, title
        exact = [(chat, title) for chat, title in chats if key == title.casefold()]
        if len(exact) == 1:
            return exact[0]
        matches = exact or [(chat, title) for chat, title in chats if key and key in title.casefold()] or chats
        choices = ", ".join(f"{title} ({chat})" for chat, title in matches) or "none"
        raise ValueError(f"Choose a single approved chat by ID. Available matches: {choices}.")

    async def message_chat(self, source: int, chat: object, text: object, reason: object) -> str:
        if source != self.app.owner_id:
            raise ValueError("Only the owner can publish messages to other chats.")
        text, reason = validate_text(text, reason)
        target, title = await self._target(chat)
        await asyncio.to_thread(guard.check_local_private_text, self.app.config, self.app.home, [text])
        score = None
        if target < 0 and self.dispatch.jev is not None:
            try:
                with model_request("guard", source, partial(self.dispatch.model_allowed, source)):
                    await check_model_allowed()
                    score = await guard.leak_score(self.dispatch.jev, guard.private_terms(self.app.home), text)
            except ModelCallSkipped:
                pass
        at = self.now()
        await self.store.create_cross_chat_card({
            "source_chat_id": source, "target_chat_id": target, "target_title": title[:128],
            "text": text, "reason": reason, "leak_score": score,
        }, at.isoformat(), self.day_start(at))
        self._wake.set()
        return "Waiting for the owner's OK on a card; it goes out once approved."

    async def approved(self, item_id: int) -> None:
        await self.store.change_cross_chat(item_id, "awaiting_approval", "queued", self.now().isoformat())
        self._wake.set()

    async def rejected(self, item_id: int) -> None:
        await self.store.change_cross_chat(
            item_id, "awaiting_approval", "dropped", self.now().isoformat(), reason="refused",
        )
        self._wake.set()

    def start(self) -> None:
        if not self._stopping and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._sending:
            self._task.cancel()

    async def close(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            await self.reconcile()
            while not self._stopping:
                self._wake.clear()
                if not await self.process_one():
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=60)
                    except TimeoutError:
                        pass
        except Exception:
            log.exception("cross-chat worker stopped")

    def expired(self, row: dict) -> bool:
        return row["kind"] == "message" and (
            self.now() - datetime.fromisoformat(row["card_created_at"] or row["created_at"]) >= CARD_LIFETIME
        )

    async def _settle(self, row: dict) -> bool:
        status = row["status"]
        if status in ("queued", "awaiting_approval") and self.expired(row):
            await self.store.change_cross_chat(row["id"], status, "dropped", self.now().isoformat(), reason="expired")
            await self.app.proposals.settle_cards()
            return True
        if status == "awaiting_approval" and row["approval_status"] != "pending":
            if row["approval_status"] == "approved":
                await self.approved(row["id"])
            else:
                await self.rejected(row["id"])
                await self.app.proposals.settle_cards()
            return True
        return False

    async def reconcile(self) -> None:
        """Recover settled cards and conservatively dispose of sends interrupted by a process or worker crash."""
        await self.store.rekey_outstanding_cross_chat()
        for row in await self.store.cross_chat_items(work=True):
            if row["status"] == "sending":
                await self.store.change_cross_chat(
                    row["id"], "sending", "dropped", self.now().isoformat(), reason="may not have been sent",
                )
            else:
                await self._settle(row)

    async def process_one(self) -> bool:
        rows = await self.store.cross_chat_items(work=True)
        for row in rows:
            if await self._settle(row):
                return True
        for row in rows:
            if row["status"] == "queued":
                if await self._deliver(row):
                    await self.app.proposals.settle_cards()
                return True
        if self.app.owner_id is not None:
            for row in rows:
                if row["status"] == "awaiting_approval" and row["approval_status"] == "pending" and not row["card_claimed_at"]:
                    await self._send_card(row["id"])
                    return True
        return False

    async def _send_card(self, item_id: int) -> None:
        async with self.dispatch.chat_slot(self.app.owner_id):
            if self._stopping:
                return
            self._sending = True
            try:
                if card := await self.store.claim_cross_chat_card(item_id, self.now().isoformat()):
                    try:
                        await self.app.proposals.send(card)
                    except Exception:
                        # The dashboard still has the approval. A failed request may already have sent the card.
                        log.exception("could not send cross-chat card %s", card["id"])
            finally:
                self._sending = False
        if card and not self._stopping:
            await self.app.proposals.settle_cards()

    async def _deliver(self, row: dict) -> bool:
        item_id, target = row["id"], row["target_chat_id"]
        migration = None
        settle = False
        async with self.dispatch.chat_slot(target):
            if self._stopping:
                return False
            self._sending = True
            try:
                at = self.now()
                claimed = await self.store.change_cross_chat(
                    item_id, "queued", "sending", at.isoformat(), target=target, day_start=self.day_start(at),
                )
                if not claimed:
                    return row["kind"] == "message"
                async def authorize_message() -> None:
                    if self.expired(row):
                        raise DeliveryRefused("expired")
                    if not await self.store.is_chat_approved(target):
                        raise DeliveryRefused("chat gone")

                try:
                    silent = row["kind"] != "message" and self.dispatch.presence._sleep_end(at, self.dispatch._habits()) is not None
                    sent = await executor.perform(
                        self.app.bot, self.store, target, None, Actions(bubbles=[Bubble(text=row["text"])]),
                        sleep=self.app.sleep, now=self.now, record=row["kind"] != "alert",
                        disable_notification=silent, link_previews=False, retry_refused=True,
                        authorize_message=authorize_message,
                    )
                except TelegramMigrateToChat as error:
                    # An explicit migration rejection means Telegram never accepted this send.
                    migration = error.migrate_to_chat_id
                    await self.store.change_cross_chat(item_id, "sending", "queued", self.now().isoformat())
                except DeliveryRefused as error:
                    await self.store.change_cross_chat(
                        item_id, "sending", "dropped", self.now().isoformat(), reason=str(error),
                    )
                    settle = str(error) == "expired"
                except Exception as error:
                    gone = isinstance(error, TelegramForbiddenError) or (
                        isinstance(error, TelegramBadRequest) and CHAT_GONE.search(str(error)) is not None
                    )
                    await self.store.change_cross_chat(
                        item_id, "sending", "dropped", self.now().isoformat(),
                        reason="chat gone" if gone else "refused" if isinstance(error, TelegramBadRequest)
                        else "may not have been sent",
                    )
                else:
                    if sent:
                        await self.store.change_cross_chat(
                            item_id, "sending", "sent", self.now().isoformat(), sent_msg_id=sent[0],
                        )
                    else:
                        await self.store.change_cross_chat(
                            item_id, "sending", "dropped", self.now().isoformat(), reason="refused",
                        )
            finally:
                self._sending = False
        if migration is not None:
            result = await self.dispatch.migrate_chat(target, migration)
            if result not in ("migrated", "already"):
                await self.store.change_cross_chat(
                    item_id, "queued", "dropped", self.now().isoformat(), reason="chat gone",
                )
        return settle
