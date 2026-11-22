"""A one-turn model switch, offered asynchronously in the owner's Telegram DM."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.llm import ChatClient, FallbackClient, MissingProviderKey, openrouter_host
from dom.models import Scope
from dom.usage import model_request, on_own_network

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.escalation")
OFFER_AFTER_SECONDS = 60
OFFER_AFTER_ROUNDS = 2
OFFER_AFTER_FAILURES = 2
PRICE_TTL = 3600
PRICE_TIMEOUT = 4
PRICE_LIST = "https://openrouter.ai/api/v1/models"
ESTIMATED_OUTPUT_TOKENS = 1000


class Prices:
    """Cache the public list prices, including a short cooldown after a failed refresh."""

    def __init__(self, spawn, clock: Callable[[], float]):
        self.spawn, self.clock = spawn, clock
        self.rows: dict[str, dict] = {}
        self.refresh_after = 0.0
        self.pending: asyncio.Task | None = None

    def warm(self, client: ChatClient) -> None:
        if (
            openrouter_host(client.host) and self.clock() >= self.refresh_after
            and (self.pending is None or self.pending.done())
        ):
            self.pending = self.spawn(self._refresh(client.http))

    async def _refresh(self, http: httpx.AsyncClient) -> None:
        try:
            async with asyncio.timeout(PRICE_TIMEOUT):
                response = await http.get(
                    PRICE_LIST, params={"zdr": "true"}, follow_redirects=False, timeout=PRICE_TIMEOUT,
                )
                response.raise_for_status()
                self.rows = {
                    row["id"]: row["pricing"] for row in response.json()["data"]
                    if isinstance(row.get("pricing"), dict)
                }
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            self.rows = {}
            self.refresh_after = self.clock() + 60
        else:
            self.refresh_after = self.clock() + PRICE_TTL

    async def estimate(self, client: ChatClient, messages: list[dict], tools: list[dict]) -> str:
        if on_own_network(client.host):
            return "Estimated model cost $0 on the local endpoint."
        if openrouter_host(client.host):
            self.warm(client)
            if self.pending is not None and not self.pending.done():
                await asyncio.shield(self.pending)
            prices = self.rows.get(client.endpoint.model, {})
            try:
                prompt, completion, request = (
                    Decimal(str(prices["prompt"])), Decimal(str(prices["completion"])),
                    Decimal(str(prices.get("request") or 0)),
                )
                if all(rate.is_finite() and rate >= 0 for rate in (prompt, completion, request)):
                    size = len(json.dumps((messages, tools), ensure_ascii=False).encode())
                    input_tokens = (size + 3) // 4
                    cost = prompt * input_tokens + completion * ESTIMATED_OUTPUT_TOKENS + request
                    amount = "<$0.0001" if 0 < cost < Decimal("0.0001") else f"${cost:.4f}"
                    return (
                        f"Estimated {amount} for the next round "
                        f"(~{input_tokens:,} input and {ESTIMATED_OUTPUT_TOKENS:,} output tokens)."
                    )
            except (KeyError, InvalidOperation):
                pass
        return "Estimated cost unavailable from the provider's price list."


class Escalation:
    def __init__(self, app: App, spawn, clock: Callable[[], float] = monotonic):
        self.app, self.spawn, self.clock = app, spawn, clock
        self.prices = Prices(spawn, clock)
        self.pending: dict[str, EscalationTurn] = {}

    @contextmanager
    def turn(self, chat_id: int, scope: Scope) -> Iterator[EscalationTurn | None]:
        route = self.app.escalation_client
        if scope != "owner_dm" or chat_id != self.app.owner_id or route is None:
            yield None
            return
        # A card consents to this exact model and provider. A later settings edit or provider error
        # cannot send the task to an alternate endpoint under that approval.
        provider = next((provider for provider in route.providers if provider.available), None)
        if provider is None:
            yield None
            return
        turn = EscalationTurn(self, chat_id, provider)
        self.prices.warm(provider)
        try:
            yield turn
        finally:
            turn.finish()

    def choose(self, token: str, *, switch: bool) -> str:
        turn = self.pending.get(token)
        if turn is None:
            return "That task already finished"
        if turn.choice is not None:
            return "Already handled"
        turn.choice = switch
        return "Switching for this task" if switch else "Keeping the current model"


class EscalationTurn:
    def __init__(self, manager: Escalation, chat_id: int, provider: ChatClient):
        self.manager, self.chat_id, self.provider = manager, chat_id, provider
        self.deadline = manager.clock() + OFFER_AFTER_SECONDS
        self.rounds = self.failures = 0
        self.choice: bool | None = None
        self.token: str | None = None
        self.timer: asyncio.Task | None = None
        self.prompt: tuple[list[dict], list[dict]] = ([], [])

    @property
    def offered(self) -> bool:
        return self.token is not None

    def finish(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        if self.token is not None:
            self.manager.pending.pop(self.token, None)

    def failed(self) -> None:
        if self.choice is not True:
            self.failures += 1
            if self.failures >= OFFER_AFTER_FAILURES:
                self._offer()

    def _round_completed(self) -> None:
        self.rounds += 1
        if self.rounds >= OFFER_AFTER_ROUNDS and not self.offered:
            if self.manager.clock() > self.deadline:
                self._offer()
            elif self.timer is None:
                self.timer = self.manager.spawn(self._wait_for_deadline())

    async def _wait_for_deadline(self) -> None:
        while (remaining := self.deadline - self.manager.clock()) >= 0:
            await asyncio.sleep(max(0.001, remaining))
        self._offer()

    def _offer(self) -> None:
        if self.offered:
            return
        self.token = uuid4().hex
        self.manager.pending[self.token] = self
        messages, tools = self.prompt
        self.manager.spawn(self._send_card(self.token, list(messages), tools))

    async def _send_card(self, token: str, messages: list[dict], tools: list[dict]) -> None:
        try:
            estimate = await self.manager.prices.estimate(self.provider, messages, tools)
            app = self.manager.app
            if token not in self.manager.pending or app.owner_id != self.chat_id:
                return
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Switch", callback_data=f"escalation:switch:{token}"),
                InlineKeyboardButton(text="Keep local", callback_data=f"escalation:keep:{token}"),
            ]])
            await app.bot.send_message(
                self.chat_id,
                f"Model switch available for this task\n{self.provider.endpoint.model}\n{estimate}",
                reply_markup=markup, parse_mode=None,
            )
        except Exception as exc:
            log.warning("could not send model switch card (%s)", type(exc).__name__)

    async def complete(
        self, local: ChatClient | FallbackClient, messages: list[dict], tools: list[dict], *,
        on_fallback: bool = False,
    ) -> dict:
        self.prompt = messages, tools
        if self.rounds >= OFFER_AFTER_ROUNDS and self.manager.clock() > self.deadline:
            self._offer()
        allowed = lambda: self.manager.app.dispatch.turn_allowed(self.chat_id, "owner_dm")
        # A dead route can otherwise end a turn on its first connection error. Give it a bounded
        # recovery path while the owner can act on the card. No wait for a decision is added.
        for attempt in range(3):
            if self.choice is True:
                with model_request("escalation", self.chat_id, allowed):
                    return await self.provider.complete(messages, tools)
            failures_before = self.failures
            with model_request("chat", self.chat_id, allowed, on_failure=self.failed):
                try:
                    if on_fallback and isinstance(local, FallbackClient) and local.fallback is not None:
                        reply = await local.complete_on_fallback(messages, tools, once=attempt > 0)
                    else:
                        reply = await local.complete(messages, tools, once=attempt > 0)
                    if not isinstance(reply, dict):
                        raise ValueError("invalid assistant message")
                except (httpx.HTTPError, MissingProviderKey, ValueError, KeyError, IndexError, TypeError):
                    if self.failures == failures_before:
                        self.failed()
                    self._round_completed()
                    if attempt == 2 or failures_before >= OFFER_AFTER_FAILURES:
                        raise
                else:
                    self._round_completed()
                    return reply
        raise AssertionError("unreachable")
