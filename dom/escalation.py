"""A one-turn model switch, offered asynchronously in the owner's Telegram DM."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dom.agent import NoFinalAnswer
from dom.acp import AcpClient, AcpError
from dom.llm import ChatClient, FallbackClient, MissingProviderKey, _retryable, openrouter_host
from dom.models import Scope, StoredMessage
from dom.telegram.executor import edit_text, send_text
from dom.usage import model_request, on_own_network

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.escalation")
OFFER_AFTER_SECONDS = 60
OFFER_AFTER_ROUNDS = 2
OFFER_AFTER_FAILURES = 2
RETRY_OFFER_SECONDS = 600
RECOVERY_TIMEOUT = 15
MODEL_FAILURES = (httpx.HTTPError, MissingProviderKey, AcpError, ValueError, KeyError, IndexError, TypeError)
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
            self.refresh_after = self.clock() + 60
        else:
            self.refresh_after = self.clock() + PRICE_TTL

    async def estimate(self, client: ChatClient, messages: list[dict], tools: list[dict]) -> str:
        if isinstance(client, AcpClient):
            return "Billed to your subscription, not per call. It keeps your prompts under its consumer terms."
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


@dataclass(frozen=True)
class RetryTask:
    provider: ChatClient
    chat_id: int
    msg_id: int
    row_id: int
    tool_results: dict[tuple[str, str, int], str]
    away_count: int
    skill: str | None


class Escalation:
    def __init__(self, app: App, spawn, clock: Callable[[], float] = monotonic):
        self.app, self.spawn, self.clock = app, spawn, clock
        self.prices = Prices(spawn, clock)
        self.pending: dict[str, EscalationTurn] = {}
        self.closed = False

    @contextmanager
    def turn(
        self, chat_id: int, scope: Scope, local: ChatClient | FallbackClient,
    ) -> Iterator[EscalationTurn | None]:
        route = self.app.escalation_client
        if scope != "owner_dm" or chat_id != self.app.owner_id or route is None or self.closed:
            yield None
            return
        # Pin the provider the owner is shown, even if Settings changes before the choice.
        provider = next((provider for provider in route.providers if provider.available), None)
        if provider is None:
            yield None
            return
        primary = local.primary if isinstance(local, FallbackClient) else local
        turn = EscalationTurn(self, chat_id, provider, primary.endpoint.model)
        self.prices.warm(provider)
        try:
            yield turn
        finally:
            turn.finish()

    def close(self) -> None:
        self.closed = True
        for turn in self.pending.values():
            if turn.expiry is not None:
                turn.expiry.cancel()
        self.pending.clear()

    async def settle(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await edit_text(self.app.bot, chat_id, message_id, text, reply_markup=None, sleep=self.app.sleep)
        except Exception as exc:
            log.info("could not settle model switch card (%s)", type(exc).__name__)

    def choose(self, token: str, decision: str, chat_id: int, message_id: int) -> str:
        turn = self.pending.get(token)
        is_retry = decision in ("retry", "dismiss")
        if turn is not None and turn.retry_until is not None and self.clock() >= turn.retry_until:
            turn.expire(message_id)
            return "That retry offer expired"
        if turn is None:
            outcome = "That retry offer expired" if is_retry else "That task already finished"
            if not self.closed:
                self.spawn(self.settle(chat_id, message_id, outcome))
            return outcome
        if (
            turn.chat_id != chat_id or is_retry != (turn.retry_until is not None)
            or (turn.message_id is not None and turn.message_id != message_id)
        ):
            return "This card has changed"
        if turn.choice is not None:
            return "Already handled"
        if turn.retry_task is not None and turn.retry_until is None:
            return "That task already finished"
        turn.message_id = message_id
        turn.choice = decision
        turn.prompt = ([], [])
        turn.update_card()
        if decision == "retry" and turn.retry_task is not None:
            self.spawn(self.app.dispatch.retry_escalation(turn.retry_task))
        turn.retry_task = None
        return OUTCOMES[decision]


OUTCOMES = {
    "switch": "Switching for this task",
    "keep": "Keeping the current model",
    "retry": "Retry requested",
    "dismiss": "Retry dismissed",
    "expired": "That retry offer expired",
}


def _can_recover(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return _retryable(exc.response.status_code)
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class EscalationTurn:
    def __init__(self, manager: Escalation, chat_id: int, provider: ChatClient, local_model: str):
        self.manager, self.chat_id, self.provider, self.local_model = manager, chat_id, provider, local_model
        self.deadline = manager.clock() + OFFER_AFTER_SECONDS
        self.rounds = self.failures = 0
        self.permanent = False  # a failure a second try on the same model cannot fix (400, 401, missing key)
        self.choice: str | None = None
        self.token = uuid4().hex
        self.timer: asyncio.Task | None = None
        self.prompt: tuple[list[dict], list[dict]] = ([], [])
        self.retry_task: RetryTask | None = None
        self.retry_until: float | None = None
        self.expiry: asyncio.TimerHandle | None = None
        self.message_id: int | None = None
        self.card_job: asyncio.Task | None = None
        self.card_version = 0

    def finish(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        if self.retry_task is None:
            self.manager.pending.pop(self.token, None)

    def failed(self) -> None:
        if self.choice != "switch":
            self.failures += 1

    def prepare_retry(
        self, newest: StoredMessage, tool_results: dict[tuple[str, str, int], str],
        away_count: int, skill: str | None,
    ) -> bool:
        if (
            (self.failures < OFFER_AFTER_FAILURES and not self.permanent) or self.choice is not None
            or newest.msg_id is None or newest.id is None
        ):
            return False
        self.retry_task = RetryTask(
            self.provider, self.chat_id, newest.msg_id, newest.id, dict(tool_results), away_count, skill,
        )
        return True

    def publish_retry(self) -> None:
        if self.manager.closed or self.manager.app.restart_requested:  # a card offered now would die with the process
            return
        self.retry_until = self.manager.clock() + RETRY_OFFER_SECONDS
        self.expiry = asyncio.get_running_loop().call_later(RETRY_OFFER_SECONDS, self.expire)
        self.manager.pending[self.token] = self
        self.update_card()

    def expire(self, message_id: int | None = None) -> None:
        self.manager.pending.pop(self.token, None)
        if self.expiry is not None:
            self.expiry.cancel()
        if self.choice is None:
            self.choice = "expired"
            if message_id is not None:
                self.message_id = message_id
            self.update_card()
        self.retry_task = None

    def abort_retry(self) -> None:
        self.manager.pending.pop(self.token, None)

    def _round_completed(self) -> None:
        self.rounds += 1
        if self.rounds >= OFFER_AFTER_ROUNDS and self.timer is None and self.token not in self.manager.pending:
            if self.manager.clock() > self.deadline:
                self._offer_switch()
            else:
                self.timer = self.manager.spawn(self._wait_for_deadline())

    async def _wait_for_deadline(self) -> None:
        while (remaining := self.deadline - self.manager.clock()) >= 0:
            await asyncio.sleep(max(0.001, remaining))
        self._offer_switch()

    def _offer_switch(self) -> None:
        if not self.manager.closed and not self.manager.app.restart_requested and self.token not in self.manager.pending:
            self.manager.pending[self.token] = self
            self.update_card()

    def update_card(self) -> None:
        self.card_version += 1
        if not self.manager.closed and (self.card_job is None or self.card_job.done()):
            self.card_job = self.manager.spawn(self._render_card())

    async def _render_card(self) -> None:
        app = self.manager.app
        estimate = None
        try:
            while app.owner_id == self.chat_id:
                version = self.card_version
                if self.choice is not None:
                    if self.message_id is not None:
                        await self.manager.settle(self.chat_id, self.message_id, OUTCOMES[self.choice])
                    return
                if self.token not in self.manager.pending:
                    return
                if estimate is None:
                    messages, tools = self.prompt
                    estimate = await self.manager.prices.estimate(self.provider, list(messages), tools)
                    continue  # state can change while pricing is fetched
                if self.retry_until is not None:
                    text = (
                        f"Dom couldn't answer with {self.local_model}. "
                        f"Retry on {self.provider.endpoint.model}?\n{estimate}"
                    )
                    choices = (("Retry", "retry"), ("Dismiss", "dismiss"))
                else:
                    text = f"Model switch available for this task\n{self.provider.endpoint.model}\n{estimate}"
                    choices = (("Switch", "switch"), ("Keep local", "keep"))
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=label, callback_data=f"escalation:{action}:{self.token}")
                    for label, action in choices
                ]])
                if self.message_id is None:
                    sent = await send_text(app.bot, self.chat_id, text, reply_markup=markup, sleep=app.sleep)
                    self.message_id = sent.message_id
                else:
                    await edit_text(app.bot, self.chat_id, self.message_id, text, reply_markup=markup, sleep=app.sleep)
                if version == self.card_version:
                    if self.retry_until is not None:
                        self.prompt = ([], [])
                    return
        except Exception as exc:
            log.warning("could not show model switch card (%s)", type(exc).__name__)

    async def complete(
        self, local: ChatClient | FallbackClient, messages: list[dict], tools: list[dict], *,
        on_fallback: bool = False,
    ) -> dict:
        self.prompt = messages, tools
        if self.rounds >= OFFER_AFTER_ROUNDS and self.manager.clock() > self.deadline:
            self._offer_switch()
        allowed = lambda: self.manager.app.dispatch.turn_allowed(self.chat_id, "owner_dm")
        for recovery in (False, True):
            try:
                if self.choice == "switch":
                    with model_request("escalation", self.chat_id, allowed):
                        reply = await self.provider.complete(messages, tools)
                elif on_fallback and isinstance(local, FallbackClient):
                    reply = await local.complete_on_fallback(messages, tools)
                elif recovery:
                    reply = await local.complete(messages, tools, once=True, timeout=RECOVERY_TIMEOUT)
                else:
                    reply = await local.complete(messages, tools)
                if not isinstance(reply, dict):
                    raise ValueError("invalid assistant message")
            except MODEL_FAILURES as exc:
                self.failed()
                self.rounds += 1
                if not _can_recover(exc):
                    self.permanent = True  # where a bigger model helps most: offer the retry after this one round
                if (
                    self.choice == "switch" or recovery or on_fallback
                    or self.failures >= OFFER_AFTER_FAILURES or not _can_recover(exc)
                ):
                    raise NoFinalAnswer(type(exc).__name__) from exc
            else:
                if self.choice != "switch":
                    self._round_completed()
                return reply
        raise AssertionError("unreachable")
