"""The house style for Dom's system notices, and the notices that use it."""

import pytest

from dom import notice
from dom.config import WebConfig
from dom.telegram import handlers
from dom.actions import utf16_len
from test_proposals import soul_edit
from dom.selfwork import parse_task
from test_selfwork import NOTES, TASK, plan_reply, self_app
from tghelpers import OWNER, tg


def with_url(app, url="https://example.org"):
    app.config = app.config.model_copy(update={"web": WebConfig(public_url=url)})


# ---- the formatter ---------------------------------------------------------------------------------------

def test_shorten_cuts_at_a_word_boundary_and_never_mid_word():
    assert notice.shorten("short enough", 20) == "short enough"
    assert notice.shorten("  many   spaces\n here ", 40) == "many spaces here"
    text = "Open the Wikipedia page for Last mile and skim delivery cost"
    for limit in range(8, len(text)):
        cut = notice.shorten(text, limit)
        assert len(cut) <= limit
        if cut != text:
            assert cut.endswith("…") and cut[:-1] in text and text[len(cut) - 1] in " "  # ended on a whole word
    assert notice.shorten("a" * 100, 10) == "a" * 9 + "…"  # one unbroken word has no boundary to use
    assert notice.shorten("exactly ten", 11) == "exactly ten" and notice.shorten("word word", 5) == "word…"


def test_first_sentence_drops_a_leading_label_and_stops_at_the_sentence():
    assert notice.first_sentence("Done. Opened the page. Then read it.") == "Opened the page."
    assert notice.first_sentence("Summary: tides rise twice a day! More follows.") == "tides rise twice a day!"
    assert notice.first_sentence("") == ""
    assert len(notice.first_sentence("word " * 50)) <= notice.DETAIL_LIMIT


def test_compose_limits_lead_details_and_bullets():
    text, entities = notice.compose("L" * 30 + " " + "m" * 80, ["one " * 40, "two", "three"])
    lines = text.splitlines()
    assert len(lines) == 3 and len(lines[0]) <= notice.LEAD_LIMIT and all(len(line) <= notice.DETAIL_LIMIT for line in lines)
    assert [(e.type, e.offset, e.length) for e in entities] == [("bold", 0, len(lines[0]))]
    text, _ = notice.compose("Lead", ["a", "b", "c", "d"], bullets=True)
    assert text == "Lead\n• a\n• b\n• c"
    text, entities = notice.compose("Lead 😀", ["detail"], tag="web summary, unchecked")
    assert text == "Lead 😀\ndetail\nweb summary, unchecked"
    assert [(e.type, e.offset, e.length) for e in entities] == [
        ("bold", 0, utf16_len("Lead 😀")), ("italic", utf16_len("Lead 😀\ndetail\n"), len("web summary, unchecked")),
    ]


def test_compose_never_turns_text_into_markup():
    text, entities = notice.compose("**Done** <b>x</b>", ["[a](https://evil.example) `code` _it_"])
    assert "**Done** <b>x</b>" in text and "[a](https://evil.example)" in text
    assert [e.type for e in entities] == ["bold"]  # only the lead is formatted, as one span


@pytest.mark.parametrize("base", ["", "http://example.org", "https://user@example.org", "https://example.org/?x=1", "https://example.org#f"])
def test_dashboard_url_needs_a_plain_https_address(base):
    assert notice.dashboard_url(base, "/app/work") is None


def test_dashboard_url_joins_the_path():
    assert notice.dashboard_url("https://example.org/", "/app/work?task=7") == "https://example.org/app/work?task=7"


async def test_send_notice_has_a_link_button_only_with_a_public_url(make_app):
    app = make_app()
    await notice.send_notice(app, OWNER, "Done: it", ["detail"], path="/app/work?task=7")
    [(_, _, text, kwargs)] = app.bot.of("send", OWNER)
    assert text == "Done: it\ndetail" and kwargs["reply_markup"] is None
    assert kwargs["link_preview_options"].is_disabled is True
    app.bot.calls.clear()
    with_url(app)
    await notice.send_notice(app, OWNER, "Done: it", ["detail"], path="/app/work?task=7")
    [button] = app.bot.of("send", OWNER)[0][3]["reply_markup"].inline_keyboard[0]
    assert (button.text, button.url) == ("Details on the dashboard", "https://example.org/app/work?task=7")


# ---- the converted notices -------------------------------------------------------------------------------

async def test_the_plan_announcement_links_to_the_dashboard_and_has_no_per_task_buttons(make_app):
    app = self_app(make_app, plan_reply(TASK, NOTES), delay_hours=3)
    with_url(app)
    await app.self_work.plan()
    [(_, _, text, kwargs)] = app.bot.of("send", OWNER)
    markup = kwargs["reply_markup"]
    [[button]] = markup.inline_keyboard
    assert button.text == "Review on dashboard" and button.url == "https://example.org/app/work/own-work"
    assert not any(b.callback_data for row in markup.inline_keyboard for b in row)
    assert "cancel with /ownwork" in text


async def test_a_finished_task_links_to_its_own_dashboard_item(make_app):
    app = self_app(make_app, plan_reply(NOTES))
    with_url(app)
    [row] = await app.store.add_self_tasks([parse_task(NOTES, 20, 25)], app.dispatch.now(), app.dispatch.now())
    row = await app.store.update_self_task(row["id"], ("planned",), "done", result="Done. Tide pools hold anemones. More.")
    await app.self_work._report(row)
    [(_, _, text, kwargs)] = app.bot.of("send", OWNER)
    assert text.splitlines()[1:] == ["Tide pools hold anemones.", "web summary, unchecked"]
    assert [e.type for e in kwargs["entities"]] == ["bold", "italic"]
    assert kwargs["reply_markup"].inline_keyboard[0][0].url == f"https://example.org/app/work/own-work?own={row['id']}"


async def test_a_proposal_is_a_short_notice_with_a_dashboard_link_when_one_is_set(make_app):
    app = make_app()
    with_url(app)
    proposal_id = await soul_edit(app)
    [(_, _, text, kwargs)] = app.bot.of("send", OWNER)
    assert text == f"Proposal #{proposal_id}: A gentler voice\nA change to my soul"
    [button] = kwargs["reply_markup"].inline_keyboard[0]
    assert (button.text, button.url) == ("Review on dashboard", f"https://example.org/app/waiting?proposal={proposal_id}")
    assert "curious" not in text and kwargs["disable_notification"] is True
    # Asking for the details in Telegram still gives the whole card with its buttons.
    app.bot.calls.clear()
    await app.proposals.send((await app.proposals.pending())[0], full=True)
    [(_, _, full, kwargs)] = app.bot.of("send", OWNER)
    assert "curious" in full and [b.callback_data for b in kwargs["reply_markup"].inline_keyboard[0]] == [
        f"prop:yes:{proposal_id}", f"prop:no:{proposal_id}",
    ]


async def test_without_a_public_url_a_proposal_keeps_its_full_card(make_app):
    app = make_app()
    proposal_id = await soul_edit(app)
    [(_, _, text, kwargs)] = app.bot.of("send", OWNER)
    assert "curious" in text and kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"prop:yes:{proposal_id}"
