"""The new dashboard's Quiet Orbit tokens keep every text pair at WCAG AA in both themes."""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parents[1] / "web/src/app.css").read_text()

# (text, background) pairs the app draws. Text needs 4.5:1; the "large" set (headings, 3:1) is not used here.
TEXT_PAIRS = [
    ("text", "canvas"), ("text", "surface"), ("text", "raised"), ("text", "rail"), ("text", "tint"),
    ("text-muted", "canvas"), ("text-muted", "surface"), ("text-muted", "raised"), ("text-muted", "rail"),
    ("text-muted", "coral-tint"), ("text", "coral-tint"),
    ("signal", "canvas"), ("signal", "surface"),
    ("signal-strong", "tint"), ("signal-strong", "surface"), ("signal-strong", "rail"),
    ("on-primary", "primary-fill"), ("on-coral", "coral-fill"),
    ("coral", "coral-tint"), ("coral", "surface"), ("coral", "canvas"),
    ("text", "overlay"), ("text-muted", "overlay"),
    # Status glyphs and their words sit on every surface level, the side panel's overlay included.
    *(
        (f"status-{status}", surface)
        for status in ("done", "running", "waiting", "failed", "held", "paused")
        for surface in ("canvas", "rail", "surface", "raised", "overlay")
    ),
]
# Non-text marks that carry meaning (switch track outline, slider, focus ring) need 3:1 against their surface.
UI_PAIRS = [("focus", "canvas"), ("focus", "surface"), ("signal", "raised"), ("text-muted", "surface")]


def theme(selector: str) -> dict[str, str]:
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S)
    assert block, selector
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", block.group(1)))


def luminance(colour: str) -> float:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("selector", ['[data-theme="dark"]', '[data-theme="light"]'])
def test_quiet_orbit_text_and_marks_meet_wcag_aa(selector):
    colours = theme(selector)
    failing = [
        (fore, back, round(contrast(colours[fore], colours[back]), 2))
        for pairs, minimum in ((TEXT_PAIRS, 4.5), (UI_PAIRS, 3))
        for fore, back in pairs
        if contrast(colours[fore], colours[back]) < minimum
    ]
    assert failing == []


def test_both_themes_define_the_same_tokens():
    assert theme('[data-theme="dark"]').keys() == theme('[data-theme="light"]').keys()
