"""Provider usage and the owner's local-day spend limit."""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from math import isclose
from sys import float_info
from typing import TYPE_CHECKING, Literal

import httpx
from zoneinfo import ZoneInfo

from dom import provenance
from dom.config import SPEND_AREAS, SpendArea
from dom.secrets import active_redactions, redact_secrets

if TYPE_CHECKING:
    import httpx

    from dom.config import Config
    from dom.store import Store

Kind = Literal["chat", "triage", "media", "memory", "search", "reflection", "guard", "desktop", "escalation",
               "studio_voice", "studio_image", "studio_video", "embedding", "assistant", "news", "subscription"]
log = logging.getLogger("dom.usage")

# Which monthly area a kind of paid call is counted under. Everything counts toward the monthly cap whatever its area;
# an unlisted kind counts as chat. Embeddings and summaries belong to memory.
AREA_OF_KIND: dict[str, SpendArea] = {
    "chat": "chat", "triage": "chat", "guard": "chat", "escalation": "chat", "assistant": "chat",
    "media": "media", "studio_voice": "media", "studio_image": "media", "studio_video": "media",
    "search": "search", "news": "search", "desktop": "desktop",
    "memory": "memory", "reflection": "memory", "embedding": "memory",
}


def area_of(kind: str) -> SpendArea:
    return AREA_OF_KIND.get(kind, "chat")


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host names literally, including forms the resolver accepts but ipaddress does not
    (decimal, hex or octal IPv4 forms), so they can't pass as single-label names."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            return None
    return getattr(address, "ipv4_mapped", None) or address


def unroutable_address(host: str) -> bool:
    """An IP literal that is never a provider: link-local (169.254.x, cloud metadata), unspecified or multicast."""
    address = _ip_literal(host)
    if address is None:
        return False
    return address.is_link_local or address.is_unspecified or address.is_multicast


_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def on_own_network(host: str) -> bool:
    """Loopback, a private or Tailscale address, or a clearly local name (single-label, .local, .lan, .internal, .home.arpa, .ts.net)."""
    host = host.rstrip(".").lower()
    address = _ip_literal(host)
    if address is None:
        return "." not in host or host.endswith((".local", ".lan", ".internal", ".home.arpa", ".ts.net"))
    tailscale = (
        (address.version == 4 and address in _TAILSCALE_V4)
        or (address.version == 6 and address in _TAILSCALE_V6)
    )
    return (address.is_private or address.is_loopback or tailscale) and not unroutable_address(host)


def provider_address(url: httpx.URL) -> str:
    """Identify the provider's host and optional port without credentials."""
    return url.netloc.decode("ascii")


def local_hosts(config: Config) -> tuple[str, ...]:
    """Hosts of the configured model endpoints on the owner's own network (a home-lab vLLM, say). Their calls cost
    nothing, so a desktop task's dollar ceiling must not price them from tokens."""
    hosts = set()
    for _, endpoint in config.model_endpoints():
        url = httpx.URL(endpoint.base_url)
        if url.host and on_own_network(url.host):
            hosts.update((url.host, provider_address(url)))  # includes the host-only form in older usage rows
    return tuple(sorted(hosts))


class ModelCallSkipped(Exception):
    """The chat lost approval or reached the spend cap before a model call."""


@dataclass(frozen=True)
class _Request:
    kind: Kind
    chat_id: int | None
    allowed: Callable[[], Awaitable[bool]]
    unprompted: bool = False  # a turn Dom starts himself (an opener, a noticing), not one a message asked for


_request: ContextVar[_Request | None] = ContextVar("model_request", default=None)


@contextmanager
def model_request(
    kind: Kind, chat_id: int | None, allowed: Callable[[], Awaitable[bool]], *, unprompted: bool = False,
) -> Iterator[None]:
    """Carry request attribution and approval through tool rounds, retries and fallback."""
    token = _request.set(_Request(kind, chat_id, allowed, unprompted))
    try:
        yield
    finally:
        _request.reset(token)


def request_chat_id() -> int | None:
    """The chat the model request in progress is for, or None outside a request."""
    request = _request.get()
    return request.chat_id if request is not None else None


def request_unprompted() -> bool:
    """Whether the model request in progress is for a turn Dom started himself."""
    request = _request.get()
    return request is not None and request.unprompted


def request_kind(default: Kind) -> Kind:
    request = _request.get()
    return request.kind if request is not None else default


async def check_model_allowed() -> None:
    request = _request.get()
    if request is not None and not await request.allowed():
        raise ModelCallSkipped


def _tokens(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def response_usage(payload: object) -> dict:
    data = payload.get("usage") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def token_counts(data: dict) -> tuple[int | None, int | None, int | None]:
    """Provider counts for a call, with missing or invalid values left unknown."""
    details = data.get("prompt_tokens_details") or data.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = data.get("cached_tokens", data.get("prompt_cache_hit_tokens", data.get("cache_read_input_tokens")))
    return (
        _tokens(data.get("prompt_tokens", data.get("input_tokens"))),
        _tokens(data.get("completion_tokens", data.get("output_tokens"))),
        _tokens(cached),
    )


def _cost(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= value <= float_info.max:
        return 0.0
    return float(value)


# Off-peak USD per million tokens (cache hits, cache misses, outputs).
# DeepSeek Models & Pricing, verified 2026-11-24: https://api-docs.deepseek.com/quick_start/pricing/
_DEEPSEEK_FLASH_PRICES = (0.003, 0.15, 0.60)
_DEEPSEEK_PRICES = {
    "deepseek-flash": _DEEPSEEK_FLASH_PRICES,
    "deepseek-v4-flash": _DEEPSEEK_FLASH_PRICES,
    "deepseek-v4-flash-vision-exp": _DEEPSEEK_FLASH_PRICES,
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}

# Inclusive month and day ranges from the State Council's published schedules.
# 2026: https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm
# 2027: https://www.gov.cn/zhengce/content/202611/content_7085101.htm
_CHINESE_PUBLIC_HOLIDAYS = {
    2026: ((1, 1, 3), (2, 15, 23), (4, 4, 6), (5, 1, 5), (6, 19, 21), (9, 25, 27), (10, 1, 7)),
    2027: ((1, 1, 3), (2, 5, 13), (4, 3, 5), (5, 1, 5), (6, 9, 9), (9, 15, 15), (10, 1, 7)),
}


@lru_cache(maxsize=128)
def _deepseek_prices(model: str) -> tuple[float, float, float] | None:
    prices = _DEEPSEEK_PRICES.get(model)
    if prices is None:
        log.warning("No USD price is configured for a direct DeepSeek chat model.")
    return prices


def _deepseek_cost(model: str, usage: dict, started_at: datetime) -> float:
    prices = _deepseek_prices(model)
    if prices is None:
        return 0.0
    hit = _tokens(usage.get("prompt_cache_hit_tokens"))
    miss = _tokens(usage.get("prompt_cache_miss_tokens"))
    output = _tokens(usage.get("completion_tokens"))
    utc = started_at.astimezone(timezone.utc)
    holiday = any(
        utc.month == month and first <= utc.day <= last
        for month, first, last in _CHINESE_PUBLIC_HOLIDAYS.get(utc.year, ())
    )
    peak = utc.weekday() < 5 and not holiday and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)
    return ((hit or 0) * prices[0] + (miss or 0) * prices[1] + (output or 0) * prices[2]) * (2 if peak else 1) / 1e6


class UsageRecorder:
    def __init__(self, store: Store, now: Callable[[], datetime] | None = None):
        self.store = store
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def record(
        self, kind: Kind, model: str, host: str, usage: dict | None,
        chat_id: int | None = None, fallback: bool = False,
    ) -> int | None:
        """Record one call; returns its usage row ID, or None when recording failed."""
        try:
            data = usage if isinstance(usage, dict) else {}
            input_tokens, output_tokens, _ = token_counts(data)
            if active_redactions():
                model = redact_secrets(model)
                host = redact_secrets(host)
            row = await self.store.record_usage(
                self.now().astimezone(timezone.utc).isoformat(), kind, model, host, chat_id,
                input_tokens or 0, output_tokens or 0,
                _cost(data.get("cost")), int(fallback),
            )
            if kind.startswith("studio_"):  # a media job in progress links the row, so its cost can be read back
                provenance.note(kind, model, host, row)
            from dom import turn_log  # imported here: turn_log imports this module

            turn_log.usage_recorded(row)
            return row
        except Exception:
            log.exception("could not record %s usage; the model call is unaffected", kind)
            return None

    async def record_response(
        self, response: httpx.Response | None, kind: Kind, model: str, host: str, *, fallback: bool = False,
        default_cost: float = 0.0, deepseek_started_at: datetime | None = None,
    ) -> None:
        try:
            payload = response.json() if response is not None else None
        except ValueError:
            payload = None
        request = _request.get()
        usage = response_usage(payload)
        if deepseek_started_at is not None:
            usage = {**usage, "cost": _deepseek_cost(model, usage, deepseek_started_at) if usage else 0.0}
        elif default_cost and "cost" not in usage:
            usage = {**usage, "cost": default_cost}
        if kind == "chat" and usage:
            input_tokens, output_tokens, cached_tokens = token_counts(usage)
            log.info(
                "chat call chat=%s model=%s in=%s cached=%s out=%s", request.chat_id if request else None, model,
                input_tokens, cached_tokens, output_tokens,
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

    def month_bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """The calendar month that contains now, in the owner's timezone."""
        today = self.now().astimezone(tz).date()
        first = today.replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        return datetime.combine(first, time.min, tz), datetime.combine(following, time.min, tz)

    async def month_by_kind(self, tz: ZoneInfo) -> dict[str, float]:
        """This month's spend per kind of call. Reads the live rows, so a cost reported late counts once it is set."""
        start, end = self.month_bounds(tz)
        return await self.store.usage_cost_by_kind(
            start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(),
        )

    async def month_by_area(self, tz: ZoneInfo) -> dict[SpendArea, float]:
        areas: dict[SpendArea, float] = dict.fromkeys(SPEND_AREAS, 0.0)
        for kind, cost in (await self.month_by_kind(tz)).items():
            areas[area_of(kind)] += cost
        return areas

    async def one_off_room(self, config: Config, area: SpendArea | None = None) -> float | None:
        """What a one-off (an event day) may still spend: the month's unused budget under the monthly cap and, when
        given, the area's own monthly limit, whichever is less. None means no limit applies."""
        tz = ZoneInfo(config.timezone)
        by_area = await self.month_by_area(tz)
        rooms = []
        if config.monthly_spend_cap_usd:
            rooms.append(config.monthly_spend_cap_usd - sum(by_area.values()))
        if area is not None and (area_cap := config.area_spend_caps_usd.get(area)):
            rooms.append(area_cap - by_area[area])
        return max(0.0, min(rooms)) if rooms else None

    async def may_spend_one_off(self, config: Config, amount: float, *, area: SpendArea | None = None) -> bool:
        """May a one-off spend `amount` USD? It draws only on the month's unused budget and never passes the monthly cap."""
        if not 0 <= amount < float("inf"):
            return False
        room = await self.one_off_room(config, area)
        return room is None or amount <= room or isclose(amount, room, rel_tol=1e-12)

    async def over_cap(
        self, config: Config, bot, *, sleep=None, area: SpendArea | None = None,
        spawn_notice: Callable[[Coroutine[None, None, None]], object] | None = None, include_daily: bool = True,
    ) -> bool:
        """True while the daily cap, the monthly cap or `area`'s monthly limit is reached. `area` defaults to the area
        of the model request in progress, if there is one. The first time each limit trips, the owner is told once."""
        request = _request.get()
        if area is None and request is not None:
            area = area_of(request.kind)
        area_cap = config.area_spend_caps_usd.get(area, 0) if area is not None else 0
        daily, monthly = (config.daily_spend_cap_usd if include_daily else 0), config.monthly_spend_cap_usd
        if not (daily or monthly or area_cap):
            return False
        tz = ZoneInfo(config.timezone)
        day_start, day_end = self._day_bounds(tz)
        tripped: list[tuple[str, str, str, list[str]]] = []  # (notice key, owner event, notice lead, notice lines)
        if daily:
            _, spent = await self.store.usage_totals(
                day_start.astimezone(timezone.utc).isoformat(), day_end.astimezone(timezone.utc).isoformat(),
            )
            if _reached(spent, daily):
                tripped.append((day_start.date().isoformat(), (
                    f"Daily spend cap of ${daily:.2f} reached (${spent:.2f} spent). Friends' chats are paused until "
                    "midnight. Change `daily_spend_cap_usd` in config.toml."
                ), f"Daily spend cap reached: ${spent:.2f} of ${daily:.2f}", [
                    "Friends' and groups' chats pause until midnight.",
                    "Change daily_spend_cap_usd in Settings or config.toml.",
                ]))
        if monthly or area_cap:
            by_area = await self.month_by_area(tz)
            month = day_start.strftime("%Y-%m")
            if monthly and _reached(total := sum(by_area.values()), monthly):
                tripped.append((f"month:{month}", (
                    f"Monthly spend cap of ${monthly:.2f} reached (${total:.2f} spent this month). Friends' chats are "
                    "paused until the 1st. Change `monthly_spend_cap_usd` in config.toml or in Settings."
                ), f"Monthly spend cap reached: ${total:.2f} of ${monthly:.2f}", [
                    "Friends' chats and background work pause until the 1st.",
                    "Change monthly_spend_cap_usd in Settings or config.toml.",
                ]))
            if area_cap and _reached(spent := by_area[area], area_cap):
                tripped.append((f"month:{month}:{area}", (
                    f"The monthly {area} limit of ${area_cap:.2f} is reached (${spent:.2f} spent this month). "
                    f"Work in that area is paused until the 1st. Change `area_spend_caps_usd` in config.toml or in Settings."
                ), f"Monthly {area} limit reached: ${spent:.2f} of ${area_cap:.2f}", [
                    f"{area.capitalize()} work pauses until the 1st.",
                    "Change area_spend_caps_usd in Settings or config.toml.",
                ]))
        if not tripped:
            return False

        owner_id = self.store.owner_id
        if owner_id is not None:
            for key, event, lead, lines in tripped:
                if not await self.store.claim_spend_cap_notice(key):
                    continue

                async def notify(event: str = event, lead: str = lead, lines: list[str] = lines) -> None:
                    try:
                        from dom.notice import send_notice_to

                        await self.store.record_owner_event(event)
                        kw = {"sleep": sleep} if sleep is not None else {}
                        await send_notice_to(bot, config, owner_id, lead, lines, path=SPEND_PAGE, **kw)
                    except Exception:
                        log.exception("could not send or record the owner's spend cap notice")

                if spawn_notice is None:
                    await notify()
                else:
                    spawn_notice(notify())
        return True


SPEND_PAGE = "/app/system/spend"  # where the dashboard shows the caps, for the notice's link


def _reached(spent: float, cap: float) -> bool:
    return spent >= cap or isclose(spent, cap, rel_tol=1e-12)
