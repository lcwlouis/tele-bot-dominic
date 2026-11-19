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


def persona_card(home: Home) -> str:
    _, body = home.scoped(allow_private=False).soul()
    first = re.search(r"^##\s", body, re.MULTILINE)
    if first:
        following = re.search(r"^#{1,2}\s", body[first.end():], re.MULTILINE)
        if following:
            body = body[:first.end() + following.start()]
    return body.strip()[:600]


def _state(card: str, window_xml: str) -> str:
    return f"Dom's persona:\n{card}\n\nRecent messages:\n{window_xml}"


async def triage_online(jev: JevClient, card: str, window_xml: str) -> Triage:
    answers = await jev.decide(_state(card, window_xml), {
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
    })
    action_answer = answers.get("action") or {}
    emoji_answer = answers.get("emoji") or {}
    action = action_answer.get("choice")
    emoji = emoji_answer.get("choice")
    confidence = action_answer.get("confidence")
    return Triage(
        action if action in tuple(_ACTION_CRITERIA) else None,
        emoji if emoji in tuple(_EMOJI_CRITERIA) else None,
        None,
        type(confidence) in (int, float) and 0.70 <= confidence <= 1,
    )


async def triage_offline(jev: JevClient, card: str, window_xml: str) -> Triage:
    answers = await jev.decide(_state(card, window_xml), {
        "kapoh": {
            "type": "score",
            "instructions": "How much would Dom want to interrupt his offline time to join in?",
            "criteria": ["boring", "mildly interesting", "juicy", "must jump in now"],
        },
    })
    score = (answers.get("kapoh") or {}).get("score")
    return Triage(None, None, score if type(score) in (int, float) and 0 <= score <= 3 else None, False)
