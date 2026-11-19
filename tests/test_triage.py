import json

import httpx
import pytest

from dom.config import load_config
from dom.home import Home
from dom.jev import JevClient, JevUnavailable
from dom.triage import Triage, persona_card, triage_offline, triage_online


@pytest.fixture
def jev():
    def make(http):
        return JevClient(http, "fake-key", "~typesafe/jev-latest")
    return make


async def test_decide_parses_choice_and_score(jev):
    questions = {
        "action": {"type": "choice"},
        "kapoh": {"type": "score"},
    }
    answers = {
        "action": {
            "type": "choice", "choice": "reply",
            "probabilities": {"reply": 0.8, "react": 0.1, "ignore": 0.1}, "confidence": 0.8,
        },
        "kapoh": {
            "type": "score", "score": 2.75,
            "probabilities": {"0": 0.0, "1": 0.0, "2": 0.25, "3": 0.75}, "confidence": 0.75,
        },
    }

    def respond(request):
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer fake-key"
        assert set(request.extensions["timeout"].values()) == {8}
        assert json.loads(request.content) == {
            "model": "~typesafe/jev-latest", "state": "recent chat", "questions": questions,
        }
        return httpx.Response(200, json={"answers": answers})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await jev(http).decide("recent chat", questions) == answers


async def test_refusal_and_missing_key_are_none(jev):
    def respond(request):
        return httpx.Response(200, json={"answers": {
            "action": {"type": "refusal"},
            "kapoh": {"type": "unknown"},
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        questions = {key: {} for key in ("action", "emoji", "kapoh")}
        assert await jev(http).decide("state", questions) == dict.fromkeys(questions)


@pytest.mark.parametrize("status", [None, 503])
async def test_jev_timeout_raises_unavailable(jev, status):
    calls = []

    def respond(request):
        calls.append(request)
        if status:
            return httpx.Response(status)
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(JevUnavailable):
            await jev(http).decide("state", {})
    assert len(calls) == 1


async def test_low_confidence_not_confident(jev):
    def respond(request):
        body = json.loads(request.content)
        assert "friendly" in body["state"] and "<chat_window/>" in body["state"]
        action, emoji = (body["questions"][key] for key in ("action", "emoji"))
        assert action["instructions"] == "What should Dom do with the newest messages?"
        assert action["criteria"] == {
            "reply": "Write a reply; he is addressed or has something worth adding.",
            "react": "Just react with an emoji; acknowledgement is enough.",
            "ignore": "Stay silent; nothing for him here.",
        }
        assert list(emoji["criteria"])[:9] == ["🤣", "❤", "💯", "🗿", "🔥", "👍", "🤡", "😭", "👀"]
        assert len(emoji["criteria"]) == 29
        import inspect
        from aiogram.types import reaction_type_emoji
        allowed = inspect.getsource(reaction_type_emoji)
        assert all(f'"{e}"' in allowed for e in emoji["criteria"])
        return httpx.Response(200, json={"answers": {
            "action": {
                "type": "choice", "choice": "react",
                "probabilities": {"reply": 0.21, "react": 0.69, "ignore": 0.1}, "confidence": 0.69,
            },
            "emoji": {"type": "choice", "choice": "👍", "probabilities": {"👍": 1}, "confidence": 1},
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await triage_online(jev(http), "friendly", "<chat_window/>") == Triage("react", "👍", None, False)


async def test_offline_asks_only_for_kapoh(jev):
    def respond(request):
        questions = json.loads(request.content)["questions"]
        assert list(questions) == ["kapoh"]
        assert questions["kapoh"]["type"] == "score"
        assert questions["kapoh"]["criteria"] == ["boring", "mildly interesting", "juicy", "must jump in now"]
        return httpx.Response(200, json={"answers": {
            "kapoh": {"type": "score", "score": 2.5, "probabilities": {"2": 0.5, "3": 0.5}, "confidence": 0.5},
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert (await triage_offline(jev(http), "", "<chat_window/>")).kapoh == 2.5


def test_persona_card_capped(tmp_path):
    soul = tmp_path / "SOUL.md"
    intro = "# Dom\nFriendly.\n\n## Voice\nA bit dry."
    soul.write_text(f"---\nname: Dom\n---\n{intro}\n\n## Later\nOther detail.")
    assert persona_card(Home(tmp_path)) == intro
    soul.write_text(intro + "x" * 600)
    assert persona_card(Home(tmp_path)) == (intro + "x" * 600)[:600]


def test_existing_config_gets_phase_two_model_defaults(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "fake-chat"\n'
        '[models.media]\nbase_url = "https://llm.test/v1"\nmodel = "fake-media"\n'
    )
    config = load_config(tmp_path)
    assert config.models["triage"].model == "~typesafe/jev-latest"
    assert config.models["media"].model == "fake-media"


async def test_decisions_url_is_built_from_the_triage_base_url(tmp_path):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"answers": {}})

    (tmp_path / "config.toml").write_text('[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "m"\n')
    default = load_config(tmp_path).models["triage"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await JevClient(http, "fake-key", default.model, default.base_url).decide("s", {})
        await JevClient(http, "fake-key", "m", "https://jev.test/alpha/").decide("s", {})
        await JevClient(http, "fake-key", "m").decide("s", {})
    assert seen == [
        "https://openrouter.ai/api/alpha/decisions", "https://jev.test/alpha/decisions",
        "https://openrouter.ai/api/alpha/decisions",
    ]
