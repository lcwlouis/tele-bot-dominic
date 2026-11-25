"""The monthly cap, per-area limits, the owner DM's choice, one-offs, the month in the owner's timezone and late costs."""

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from dom.usage import area_of, model_request
from tghelpers import FRIEND, OWNER, no_sleep

CLOCK_START = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def configure(app, **update):
    app.config = app.config.model_copy(update=update)


async def spend(app, kind, cost, **kw):
    return await app.dispatch.usage.record(kind, "mock", "example.org", {"cost": cost}, **kw)


async def over(app, **kw):
    return await app.dispatch.usage.over_cap(app.config, app.bot, sleep=no_sleep, **kw)


def owner_notices(app):
    return [call[2] for call in app.bot.of("send", OWNER)]


async def test_monthly_cap_pauses_even_when_the_daily_cap_is_off(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=0, monthly_spend_cap_usd=5.0)
    await spend(app, "chat", 4.0)
    assert not await over(app)
    await spend(app, "search", 1.0)  # every area counts toward the month
    assert await over(app)
    assert owner_notices(app) == [
        "Monthly spend cap of $5.00 reached ($5.00 spent this month). Friends' chats are paused until the 1st. "
        "Change monthly_spend_cap_usd in config.toml or in Settings."
    ]
    assert await over(app) and len(owner_notices(app)) == 1  # one notice per month
    events = [m.text for m in await app.store.get_recent_chat_window(OWNER) if m.role == "event"]
    assert events == ["Monthly spend cap of $5.00 reached ($5.00 spent this month). Friends' chats are paused until the 1st. "
                      "Change `monthly_spend_cap_usd` in config.toml or in Settings."]


async def test_zero_monthly_cap_disables_it_and_the_daily_cap_still_applies(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=1.0, monthly_spend_cap_usd=0)
    await spend(app, "chat", 0.5)
    assert not await over(app)
    await spend(app, "chat", 0.6)
    assert await over(app)
    assert owner_notices(app)[0].startswith("Daily spend cap of $1.00 reached")
    configure(app, daily_spend_cap_usd=0)
    assert not await over(app)


async def test_monthly_total_spans_days_and_includes_the_owner_dm(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=0, monthly_spend_cap_usd=3.0)
    await spend(app, "chat", 1.0, chat_id=OWNER)
    app.dispatch.now.advance(days=3)
    await spend(app, "desktop", 1.0)
    app.dispatch.now.advance(days=3)
    assert not await over(app)
    await spend(app, "chat", 1.0, chat_id=FRIEND)
    assert await over(app)


async def test_owner_dm_is_exempt_unless_cap_owner_dm_is_on(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=1.0, monthly_spend_cap_usd=2.0)
    await spend(app, "chat", 2.5, chat_id=OWNER)  # the owner's own spend still counts toward the caps
    assert await over(app)
    assert not await app.dispatch.spend_paused(OWNER)
    assert await app.dispatch.spend_paused(FRIEND)
    configure(app, cap_owner_dm=True)
    assert await app.dispatch.spend_paused(OWNER)
    assert await app.dispatch.model_allowed(FRIEND) is False


async def test_owner_dm_obeys_the_monthly_cap_alone_when_on(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=0, monthly_spend_cap_usd=2.0, cap_owner_dm=True)
    await spend(app, "chat", 1.0)
    assert not await app.dispatch.spend_paused(OWNER)
    await spend(app, "chat", 1.0)
    assert await app.dispatch.spend_paused(OWNER)


@pytest.mark.parametrize(("kind", "area"), [
    ("chat", "chat"), ("triage", "chat"), ("guard", "chat"), ("escalation", "chat"),
    ("media", "media"), ("studio_voice", "media"), ("studio_image", "media"), ("studio_video", "media"),
    ("search", "search"), ("desktop", "desktop"),
    ("memory", "memory"), ("reflection", "memory"), ("embedding", "memory"), ("something-new", "chat"),
])
def test_every_kind_belongs_to_an_area(kind, area):
    assert area_of(kind) == area


async def test_an_area_limit_pauses_that_area_only(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=0, monthly_spend_cap_usd=100, area_spend_caps_usd={"media": 1.0, "chat": 0})
    await spend(app, "studio_voice", 0.6)
    await spend(app, "media", 0.5)  # both kinds are the media area
    assert await over(app, area="media")
    assert not await over(app, area="chat")  # zero means no limit of its own
    assert not await over(app)
    with model_request("media", None, lambda: None):
        assert await over(app)  # a media request in progress is held
    with model_request("chat", FRIEND, lambda: None):
        assert not await over(app)
    assert owner_notices(app) == [
        "The monthly media limit of $1.00 is reached ($1.10 spent this month). Work in that area is paused until the 1st. "
        "Change area_spend_caps_usd in config.toml or in Settings."
    ]
    await over(app, area="media")
    assert len(owner_notices(app)) == 1


async def test_one_offs_draw_only_on_the_months_unused_budget(make_app):
    app = make_app()
    usage = app.dispatch.usage
    app.dispatch.now.at = CLOCK_START
    configure(app, monthly_spend_cap_usd=10.0, area_spend_caps_usd={"media": 2.0})
    await spend(app, "chat", 6.0)
    await spend(app, "media", 1.5)
    assert await usage.one_off_room(app.config) == pytest.approx(2.5)
    assert await usage.may_spend_one_off(app.config, 2.5)
    assert not await usage.may_spend_one_off(app.config, 2.51)
    assert await usage.may_spend_one_off(app.config, 0)
    assert await usage.one_off_room(app.config, "media") == pytest.approx(0.5)  # the area's own limit is lower
    assert not await usage.may_spend_one_off(app.config, 1.0, area="media")
    assert await usage.may_spend_one_off(app.config, 1.0, area="chat")
    await spend(app, "chat", 3.0)  # past the ceiling: nothing is left, and the room never goes negative
    assert await usage.one_off_room(app.config) == 0
    assert not await usage.may_spend_one_off(app.config, 0.01)
    for amount in (-1, math.inf, math.nan):
        assert not await usage.may_spend_one_off(app.config, amount)


async def test_one_offs_have_no_limit_when_no_cap_applies(make_app):
    app = make_app()
    configure(app, monthly_spend_cap_usd=0)
    assert await app.dispatch.usage.one_off_room(app.config) is None
    assert await app.dispatch.usage.may_spend_one_off(app.config, 1000)


async def test_the_month_turns_over_at_midnight_in_the_owners_timezone(make_app):
    app = make_app()
    configure(app, timezone="Asia/Singapore", daily_spend_cap_usd=0, monthly_spend_cap_usd=1.0)
    clock = app.dispatch.now
    clock.at = datetime(2026, 1, 31, 15, 59, tzinfo=timezone.utc)  # 23:59 on January 31 in Singapore
    await spend(app, "chat", 1.0)
    assert await over(app)
    assert len(owner_notices(app)) == 1
    clock.advance(minutes=1)  # February 1 in Singapore, still January 31 in UTC
    assert not await over(app)
    assert await app.dispatch.usage.one_off_room(app.config) == pytest.approx(1.0)
    await spend(app, "chat", 1.0)
    assert await over(app)
    assert len(owner_notices(app)) == 2  # the new month gets its own notice


async def test_a_cost_reported_late_counts_once_it_arrives(make_app):
    app = make_app()
    app.dispatch.now.at = CLOCK_START
    configure(app, daily_spend_cap_usd=0, monthly_spend_cap_usd=1.0)
    row = await spend(app, "studio_voice", 0.0)  # the provider reports the price later
    assert not await over(app)
    await app.store.set_usage_cost(row, 1.2)
    assert await over(app)
    by_area = await app.dispatch.usage.month_by_area(ZoneInfo("UTC"))
    assert by_area["media"] == pytest.approx(1.2)
