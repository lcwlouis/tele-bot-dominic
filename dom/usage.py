"""Provider usage and the owner's local-day spend limit."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from math import isclose
from sys import float_info
from typing import TYPE_CHECKING, Literal

import httpx
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import httpx

    from dom.config import Config
    from dom.store import Store

Kind = Literal["chat", "triage", "media", "memory", "search", "reflection", "guard", "desktop"]
log = logging.getLogger("dom.usage")


def _on_own_network(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith((".local", ".lan", ".home.arpa"))
    return address.is_private or address.is_loopback


def local_hosts(config: Config) -> tuple[str, ...]:
    """Hosts of the configured model endpoints on the owner's own network (a home-lab vLLM, say). Their calls cost
    nothing, so a desktop task's dollar ceiling must not price them from tokens."""
    hosts = {httpx.URL(endpoint.base_url).host for endpoint in config.models.values()}
    return tuple(sorted(host for host in hosts if host and _on_own_network(host)))


class ModelCallSkipped(Exception):
    """The chat lost approval or reached the spend cap before a model call."""


@dataclass(frozen=True)
class _Request:
    kind: Kind
    chat_id: int | None
    allowed: Callable[[], Awaitable[bool]]


_request: ContextVar[_Request | None] = ContextVar("model_request", default=None)


@contextmanager
def model_request(kind: Kind, chat_id: int | None, allowed: Callable[[], Awaitable[bool]]) -> Iterator[None]:
    """Carry request attribution and approval through tool rounds, retries and fallback."""
    token = _request.set(_Request(kind, chat_id, allowed))
    try:
        yield
    finally:
        _request.reset(token)


def request_kind(default: Kind) -> Kind:
    request = _request.get()
    return request.kind if request is not None else default


async def check_model_allowed() -> None:
    request = _request.get()
    if request is not None and not await request.allowed():
        raise ModelCallSkipped


def _tokens(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else 0


def _cost(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= value <= float_info.max:
        return 0.0
    return float(value)


class UsageRecorder:
    def __init__(self, store: Store, now: Callable[[], datetime] | None = None):
        self.store = store
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def record(
        self, kind: Kind, model: str, host: str, usage: dict | None,
        chat_id: int | None = None, fallback: bool = False,
    ) -> None:
        try:
            data = usage if isinstance(usage, dict) else {}
            await self.store.record_usage(
                self.now().astimezone(timezone.utc).isoformat(), kind, model, host, chat_id,
                _tokens(data.get("prompt_tokens", data.get("input_tokens"))),
                _tokens(data.get("completion_tokens", data.get("output_tokens"))),
                _cost(data.get("cost")), int(fallback),
            )
        except Exception:
            log.exception("could not record %s usage; the model call is unaffected", kind)

    async def record_response(
        self, response: httpx.Response, kind: Kind, model: str, host: str, *, fallback: bool = False,
    ) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        request = _request.get()
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if kind == "chat" and isinstance(usage, dict):
            details = usage.get("prompt_tokens_details") or {}
            log.info(
                "chat call chat=%s model=%s in=%s cached=%s out=%s", request.chat_id if request else None, model,
                usage.get("prompt_tokens"), details.get("cached_tokens") if isinstance(details, dict) else None,
                usage.get("completion_tokens"),
            )
        await self.record(
            request_kind(kind), model, host,
            usage,
            chat_id=request.chat_id if request else None, fallback=fallback,
        )


class Usage(UsageRecorder):
    def _day_bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        day = self.now().astimezone(tz).date()
        return datetime.combine(day, time.min, tz), datetime.combine(day + timedelta(days=1), time.min, tz)

    async def today(self, tz: ZoneInfo) -> tuple[int, float]:
        start, end = self._day_bounds(tz)
        return await self.store.usage_totals(
            start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
        )

    async def last_30_days(self, tz: ZoneInfo) -> tuple[list[str], list[dict]]:
        today, _ = self._day_bounds(tz)
        dates = [today + timedelta(days=offset) for offset in range(-29, 2)]
        bounds = [
            (start.date().isoformat(), start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())
            for start, end in zip(dates, dates[1:])
        ]
        return [day for day, _, _ in bounds], await self.store.usage_by_day(bounds)

    async def over_cap(self, config: Config, bot) -> bool:
        cap = config.daily_spend_cap_usd
        if cap == 0:
            return False
        start, end = self._day_bounds(ZoneInfo(config.timezone))
        _, spent = await self.store.usage_totals(
            start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
        )
        if spent < cap and not isclose(spent, cap, rel_tol=1e-12):
            return False

        owner_id = self.store.owner_id
        if owner_id is not None and await self.store.claim_spend_cap_notice(start.date().isoformat()):
            text = (
                f"Daily spend cap of ${cap:.2f} reached (${spent:.2f} spent). Friends' chats are paused until midnight. "
                "Change `daily_spend_cap_usd` in config.toml."
            )
            try:
                await self.store.record_owner_event(text)
                await bot.send_message(owner_id, text)
            except Exception:
                log.exception("could not send or record the owner's daily spend cap notice")
        return True
