"""Client for OpenRouter's Decisions API."""

from __future__ import annotations

import httpx

from dom import turn_log
from dom.usage import UsageRecorder, check_model_allowed, request_kind

DEFAULT_BASE_URL = "https://openrouter.ai/api/alpha"  # [models.triage].base_url in the starter config


class JevUnavailable(Exception):
    """The Decisions API did not return a usable response."""


class JevClient:
    def __init__(
        self, http: httpx.AsyncClient, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL,
        *, usage: UsageRecorder | None = None,
    ):
        self.http, self.api_key, self.model = http, api_key, model
        self.usage = usage
        self.host = httpx.URL(base_url).host
        self.url = f"{base_url.rstrip('/')}/decisions"

    async def decide(self, state: str, questions: dict) -> dict[str, dict | None]:
        await check_model_allowed()
        turn_log.attempt(self.model, request_kind("triage"))
        try:
            response = await self.http.post(
                self.url,
                json={"model": self.model, "state": state, "questions": questions},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=8,
            )
            if self.usage is not None:
                await self.usage.record_response(response, "triage", self.model, self.host)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JevUnavailable("Decisions request failed") from exc

        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            raise JevUnavailable("Decisions response is missing answers")

        result = dict.fromkeys(questions)
        for key in questions:
            answer = answers.get(key)
            if isinstance(answer, dict) and answer.get("type") in ("choice", "score"):
                result[key] = answer
        return result
