"""Translate a Jev decision about the recent window into a chat action."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from dom.home import Home
from dom.jev import JevClient

Action = Literal["reply", "react", "ignore"]
_ACTION_CRITERIA = {
    "reply": "Write a reply; he is addressed or has something worth adding.",
    "react": "Just react with an emoji; acknowledgement is enough.",
    "ignore": "Stay silent; nothing for him here.",
}
_DONE_CRITERIA = {
    "yes": "The latest message completes the thought or request. Dom can answer now.",
    "no": "The sender looks likely to send a continuation, more details or an attachment shortly.",
}
_LONG_CRITERIA = {
    "yes": "The latest request needs research, a tool, a task or substantial thinking.",
    "no": "A quick conversational answer is enough, or the sender has not made a request yet.",
}
# Only choices supported by Telegram's ReactionTypeEmoji.
_EMOJI_CRITERIA = {
    "🤣": "Something is funny.",
    "❤": "Warmth, support or affection.",
    "💯": "Strong agreement; facts.",
    "🗿": "Deadpan or unimpressed.",
    "🔥": "Impressed or excited.",
    "👍": "Acknowledgement or agreement.",
    "🤡": "Playful teasing about something foolish.",
    "😭": "Overwhelmed, sad, or laughing hard.",
    "👀": "Curious, intrigued, or watching.",
    "😁": "Grinning; light amusement.",
    "🤔": "Thinking it over; not sure.",
    "🤨": "Skeptical; that sounds off.",
    "🤯": "Mind blown.",
    "😱": "Shocked.",
    "😢": "Sad or sympathetic.",
    "💔": "Heartbroken; that hurts.",
    "🎉": "Celebrating good news.",
    "👏": "Applause; well done.",
    "🙏": "Thanks, or please.",
    "🤝": "Deal; agreed.",
    "🫡": "On it; yes boss.",
    "😎": "Smug or cool.",
    "🤓": "Nerdy or pedantic.",
    "🥱": "Bored.",
    "😴": "Sleepy.",
    "😐": "Unamused; meh.",
    "🤷": "Shrug; no idea.",
    "🙈": "Can't look; cringe.",
    "😈": "Mischievous.",
}


@dataclass(frozen=True)
class Triage:
    action: Action | None
    emoji: str | None
    kapoh: float | None
    confident: bool
    confidence: float | None = None
    done: str | None = None
    done_confidence: float | None = None
    likely_long: bool = False

    @property
    def ready(self) -> bool:
        return self.done == "yes" and _confident(self.done_confidence)


def _confidence(answer: dict) -> float | None:
    value = answer.get("confidence")
    return value if type(value) in (int, float) and 0 <= value <= 1 else None


def _confident(value: float | None) -> bool:
    return value is not None and value >= 0.70


def persona_card(home: Home) -> str:
    _, body = home.scoped(allow_private=False).soul()
    first = re.search(r"^##\s", body, re.MULTILINE)
    if first:
        following = re.search(r"^#{1,2}\s", body[first.end():], re.MULTILINE)
        if following:
            body = body[:first.end() + following.start()]
    return body.strip()[:600]


def chat_state(card: str, window_xml: str) -> str:
    return f"Dom's persona:\n{card}\n\nRecent messages:\n{window_xml}"


async def triage_online(jev: JevClient, card: str, window_xml: str, *, owner_dm: bool = False) -> Triage:
    questions = {
        "action": {
            "type": "choice",
            "instructions": "What should Dom do with the newest messages?",
            "criteria": _ACTION_CRITERIA,
        },
        "emoji": {
            "type": "choice",
            "instructions": "Which emoji best fits a reaction to the newest message?",
            "criteria": _EMOJI_CRITERIA,
        },
        "done": {
            "type": "choice",
            "instructions": "Is the sender done with this thought? Judge from the messages alone.",
            "criteria": _DONE_CRITERIA,
        },
    }
    if owner_dm:
        questions["long"] = {"type": "choice", "instructions": "Will this take a while?", "criteria": _LONG_CRITERIA}
    answers = await jev.decide(chat_state(card, window_xml), questions)
    action_answer = answers.get("action") or {}
    emoji_answer = answers.get("emoji") or {}
    action = action_answer.get("choice")
    emoji = emoji_answer.get("choice")
    done_answer = answers.get("done") or {}
    long_answer = answers.get("long") or {}
    done = done_answer.get("choice")
    confidence = _confidence(action_answer)
    return Triage(
        action if action in tuple(_ACTION_CRITERIA) else None,
        emoji if emoji in tuple(_EMOJI_CRITERIA) else None,
        None,
        _confident(confidence), confidence,
        done if done in tuple(_DONE_CRITERIA) else None, _confidence(done_answer),
        long_answer.get("choice") == "yes" and _confident(_confidence(long_answer)),
    )


async def triage_offline(jev: JevClient, card: str, window_xml: str) -> Triage:
    answers = await jev.decide(chat_state(card, window_xml), {
        "kapoh": {
            "type": "score",
            "instructions": "How much would Dom want to interrupt his offline time to join in?",
            "criteria": ["boring", "mildly interesting", "juicy", "must jump in now"],
        },
    })
    score = (answers.get("kapoh") or {}).get("score")
    return Triage(None, None, score if type(score) in (int, float) and 0 <= score <= 3 else None, False)


async def triage_urgency(jev: JevClient, card: str, window_xml: str) -> float | None:
    answers = await jev.decide(chat_state(card, window_xml), {
        "urgency": {
            "type": "score",
            "instructions": "How urgently do the owner's newest messages need Dom to respond?",
            "criteria": [
                "casual chat, can wait",
                "would like an answer soon",
                "needs Dom now: a task, a deadline, a problem",
                "emergency",
            ],
        },
    })
    answer = answers.get("urgency") or {}
    score = answer.get("score")
    return score if answer.get("type") == "score" and type(score) in (int, float) and 0 <= score <= 3 else None
