"""Remember the last exhausted chat route and deduplicate its owner notice."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from dom.llm import BudgetRefusal
from dom.store import Store

_STATE_KEY = "chat_provider_budget"


def _line(refusal: BudgetRefusal, tz: ZoneInfo) -> str:
    amount = f" (${refusal.limit_usd:.2f})" if refusal.limit_usd is not None else ""
    if refusal.source == "deepseek_credits":
        line = "DeepSeek has insufficient credits. Check your balance in DeepSeek → Top up."
    elif refusal.scope == "workspace":
        line = f"OpenRouter's workspace budget{amount} is used up. Raise it in OpenRouter → Workspace settings."
    elif refusal.scope in ("key_limit", "api_key", "workspace_default_key"):
        line = f"OpenRouter's API key limit{amount} is used up. Check the key limit in OpenRouter."
    elif refusal.source == "openrouter_credits":
        line = "OpenRouter has insufficient credits. Check your balance in OpenRouter → Credits."
    else:
        line = f"OpenRouter's spending limit{amount} is used up. Check your budget settings in OpenRouter."
    if refusal.resets_at:
        reset = datetime.fromisoformat(refusal.resets_at).astimezone(tz)
        line += f" Resets {reset:%d %b at %H:%M} ({tz.key})."
    return line


class ProviderBudgetNotices:
    def __init__(self, store: Store):
        self.store = store
        self._lock = asyncio.Lock()
        self._state: dict | None = None

    async def _load(self) -> dict:
        if self._state is None:
            raw = await self.store.kv_get(_STATE_KEY)
            self._state = json.loads(raw) if raw is not None else {"active": [], "notified": {}, "route": ""}
        return self._state

    async def _save(self, state: dict) -> None:
        write = asyncio.create_task(self.store.kv_set(_STATE_KEY, json.dumps(state)))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            self._state = state
            raise
        self._state = state

    async def record(
        self, route: str, refusals: tuple[BudgetRefusal, ...], now: datetime, tz: ZoneInfo, *, notify: bool,
    ) -> str:
        async with self._lock:
            state = await self._load()
            notified = {
                key: until for key, until in state["notified"].items()
                if until is None or now < datetime.fromisoformat(until)
            }
            active = {}
            lines = []
            for refusal in refusals:
                expiry = refusal.resets_at
                if expiry and datetime.fromisoformat(expiry) <= now:
                    continue
                window = json.dumps([refusal.source, refusal.scope, refusal.resets_at])
                active[window] = {"refusal": asdict(refusal), "until": expiry}
                if notify and window not in notified:
                    notified[window] = expiry
                    lines.append(_line(refusal, tz))
            updated = {"route": route, "active": list(active.values()), "notified": notified}
            if updated != state:
                await self._save(updated)
            return " ".join(dict.fromkeys(lines))

    async def clear(self, route: str) -> None:
        async with self._lock:
            state = await self._load()
            if state["route"] == route and state["active"]:
                notified = {key: until for key, until in state["notified"].items() if until is not None}
                await self._save({**state, "active": [], "notified": notified})

    async def status(self, route: str, now: datetime, tz: ZoneInfo) -> str | None:
        async with self._lock:
            state = await self._load()
            if state["route"] != route:
                return None
            lines = (
                _line(BudgetRefusal(**item["refusal"]), tz)
                for item in state["active"] if item["until"] is None or now < datetime.fromisoformat(item["until"])
            )
            return " ".join(dict.fromkeys(lines)) or None
