import re
from pathlib import Path

from dom.telegram.handlers import build_router

from test_owner_controls import callback
from tghelpers import OWNER

DOM = Path(__file__).resolve().parent.parent / "dom"


def callback_prefixes() -> set[str]:
    """Every `prefix:` the code puts into callback_data, so a new button can't be left unrouted."""
    found = set()
    for path in DOM.rglob("*.py"):
        text = path.read_text()
        found |= set(re.findall(r'callback_data=f?"([a-z_]+):', text))
        found |= set(re.findall(r'f"([a-z_]+):\{', text)) & {"preset", "presetset", "plugin", "prop", "rem", "desktop"}
    return found


async def test_every_button_prefix_reaches_the_callback_handler(make_app):
    # The tests call handle_callback directly; this goes through the real router and its filters,
    # which once dropped /preset and plugin buttons silently.
    app = make_app()
    handlers = build_router(app).callback_query.handlers
    prefixes = callback_prefixes()
    assert {"preset", "presetset", "plugin", "prop", "wake"} <= prefixes
    for prefix in sorted(prefixes):
        query = callback(app, OWNER, f"{prefix}:1")
        accepted = [ok for handler in handlers for ok, _ in [await handler.check(query)]]
        assert any(accepted), f"no handler accepts {prefix}: buttons"


async def test_everywhere_preset_buttons_reach_the_callback_handler(make_app):
    app = make_app()
    handlers = build_router(app).callback_query.handlers
    for data in ("preset:all", "presetset:all:", "presetset:all:classic"):
        query = callback(app, OWNER, data)
        assert any([ok for handler in handlers for ok, _ in [await handler.check(query)]]), data
