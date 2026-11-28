import json

import pytest
from aiogram.types import Document

from dom.telegram import handlers
from test_owner_events import events, press
from test_traits import put
from test_usage import online
from tghelpers import FRIEND, GROUP, OWNER, drain_dispatch, tg

ONE_MB = 1024 * 1024


def upload(chat_id=OWNER, sender=OWNER, size=20, filename="export.md", caption="/import claude"):
    document = Document(file_id="file-a", file_unique_id="unique-a", file_name=filename, file_size=size)
    return tg(chat_id, sender, None, document=document, caption=caption)


def mock_download(app, payload):
    calls = []

    async def download(document, *, destination):
        calls.append(document.file_id)
        destination.write(payload)
        destination.seek(0)
        return destination

    app.bot.download = download
    return calls


def test_parser_recognizes_bullets_with_or_without_categories():
    from dom.memory_import import parse_facts

    assert parse_facts(
        "# Notes\n- [food] Prefers tea (January)\n -   Reads mysteries\n- [empty]\n"
        "This is not a fact\n* Irrelevant list item\n-[bad] Invalid bullet\n- \n"
    ).facts == ["Prefers tea (January)", "Reads mysteries"]


def test_parser_caps_count_and_fact_length():
    from dom.memory_import import parse_facts

    text = "- " + "x" * 301 + "\n- " + "y" * 300 + "\n"
    text += "\n".join(f"- Fact {i}" for i in range(310))
    parsed = parse_facts(text)
    facts = parsed.facts
    assert (parsed.too_long, parsed.duplicates) == (1, 0)
    assert len(facts) == 300 and max(map(len, facts)) == 300
    assert facts[0] == "y" * 300 and facts[-1] == "Fact 298"


def test_parser_counts_what_it_skips_for_length_and_duplicates():
    from dom.memory_import import parse_facts

    parsed = parse_facts("- A\n- a\n-  A \n- B\n- " + "x" * 301 + "\n- Known\n", existing=["known"])
    assert (parsed.facts, parsed.too_long, parsed.duplicates) == (["A", "B"], 1, 3)


async def test_import_prompts_and_reply_creates_private_proposals_with_one_summary(make_app):
    from importlib import resources

    app = make_app()
    prompt = resources.files("dom").joinpath("starter_home", "EXPORT_PROMPT.md").read_text()
    await handlers.handle_message(app, tg(OWNER, OWNER, "/import"))
    instructions, code = app.bot.of("send", OWNER)
    assert "/import <service>" in instructions[2] and prompt.strip() == code[2]
    (entity,) = code[3]["entities"]  # one code block over the whole prompt, so one tap copies it
    assert (entity.type, entity.offset, entity.length) == ("pre", 0, len(code[2].encode("utf-16-le")) // 2)
    assert all(word in prompt for word in ("[category]", "when learned"))
    app.bot.calls.clear()
    put(app.home, "private/USER.md", "- Prefers tea\n")
    put(app.home, "private/NOTES.md", "- Reads mysteries\n")
    export = tg(OWNER, OWNER, "- [food] Prefers  tea\n- Reads mysteries\n- Owns a kettle\n- [home] Owns a kettle\n- Likes book clubs")
    await handlers.handle_message(app, tg(OWNER, OWNER, "/import ChatGPT", msg_id=2, reply_to=export))
    expected = (
        "Imported 2 facts from chatgpt. Skipped 3 duplicates. Review them in the dashboard or with /proposals. "
        "Approved facts are queued and organized into your notes and reminders during Dom's downtime."
    )
    assert [call[2] for call in app.bot.of("send")] == [expected]
    assert app.bot.of("send")[0][1] == OWNER
    assert len(await events(app)) == 2
    assert (await events(app))[-1]["text"] == expected
    pending = await app.proposals.pending()
    bodies = [json.loads(row["body_json"]) for row in pending]
    batches = {body.pop("import_batch") for body in bodies}
    assert len(batches) == 1 and len(next(iter(batches))) == 32
    assert [(row["source"], body) for row, body in zip(pending, bodies)] == [
        ("import:chatgpt", {"file": "USER.md", "text": "Owns a kettle"}),
        ("import:chatgpt", {"file": "USER.md", "text": "Likes book clubs"}),
    ]
    assert app.home.scoped(allow_private=True).read("private/USER.md") == "- Prefers tea\n"
    assert not app.script.requests

    await handlers.handle_message(app, tg(OWNER, OWNER, "/import ChatGPT", msg_id=3, reply_to=export))
    assert "Imported 0 facts" in app.bot.of("send", OWNER)[-1][2]


async def test_summary_reports_skipped_facts_and_the_user_md_budget(make_app):
    app = make_app()
    text = "/import claude\n- Likes tea\n- likes  TEA\n- " + "x" * 301 + "\n- " + "y" * 301 + "\n- Reads"
    await handlers.handle_message(app, tg(OWNER, OWNER, text))
    (summary,) = [call[2] for call in app.bot.of("send", OWNER)]
    assert summary.startswith("Imported 2 facts from claude. Skipped 1 duplicate and 2 over 300 characters. ")
    assert "queued and organized" in summary and "downtime" in summary


async def test_inline_import_and_utf8_document_use_the_same_parser_without_a_model(make_app):
    app = make_app()
    await handlers.handle_message(app, tg(OWNER, OWNER, "/import muse\n- Owns a small kettle"))
    calls = mock_download(app, b"\xef\xbb\xbf- [food] Likes tea\n")
    await handlers.handle_message(app, upload())
    assert calls == ["file-a"]
    assert {row["source"] for row in await app.proposals.pending()} == {"import:muse", "import:claude"}
    assert [call[2].split(".")[0] for call in app.bot.of("send", OWNER)] == [
        "Imported 1 facts from muse", "Imported 1 facts from claude",
    ]
    assert not app.script.requests


@pytest.mark.parametrize("chat_id,sender", [(FRIEND, FRIEND), (GROUP, OWNER)])
async def test_non_owner_dm_import_is_ignored_without_downloading_or_dispatching(make_app, chat_id, sender):
    app = make_app()
    await online(app, chat_id)
    calls = mock_download(app, b"- A fact")
    await handlers.handle_message(app, tg(chat_id, sender, "/import"))
    await handlers.handle_message(app, upload(chat_id, sender))
    await drain_dispatch(app.dispatch)
    assert not calls and not app.bot.calls and not app.script.requests
    assert await app.proposals.pending() == []


async def test_siri_has_no_exportable_memory(make_app):
    app = make_app()
    await handlers.handle_message(app, tg(OWNER, OWNER, "/import siri"))
    assert "Siri has no exportable memory" in app.bot.of("send", OWNER)[0][2]
    assert not await app.proposals.pending()


@pytest.mark.parametrize("size,downloads", [(ONE_MB + 1, []), (None, ["file-a"])])
async def test_oversized_document_refused_before_or_during_download(make_app, size, downloads):
    app = make_app()
    calls = mock_download(app, b"x" * (ONE_MB + 1))
    await handlers.handle_message(app, upload(size=size))
    assert calls == downloads
    assert "1 MB" in app.bot.of("send", OWNER)[0][2]
    assert not await app.proposals.pending()
    assert not app.script.requests


def markup_callbacks(call):
    return {button.text: button.callback_data for row in call[3]["reply_markup"].inline_keyboard for button in row}


async def test_proposals_pages_ten_and_bulk_approval_targets_the_original_page(make_app):
    app = make_app()
    text = "/import muse\n" + "\n".join(f"- Fact {i:02d}" for i in range(12))
    await handlers.handle_message(app, tg(OWNER, OWNER, text))
    await handlers.handle_message(app, tg(OWNER, OWNER, "/proposals", msg_id=2))
    page_one = app.bot.of("send", OWNER)[-1]
    callbacks = markup_callbacks(page_one)
    assert sum(value.startswith("prop:yes:") for value in callbacks.values()) == 10
    assert "Fact 09" in page_one[2] and "Fact 10" not in page_one[2]
    await press(app, callbacks["Next"])
    page_two = app.bot.of("send", OWNER)[-1]
    assert "Fact 10" in page_two[2] and "Fact 09" not in page_two[2]

    bulk = callbacks["Approve all on this page"]
    await press(app, bulk, FRIEND)
    assert len(await app.proposals.pending()) == 12
    await press(app, callbacks["Reject #1"])
    await press(app, bulk)
    assert app.bot.of("answer")[-1][2] == "Approved 9."
    assert [row["id"] for row in await app.proposals.pending()] == [11, 12]
    assert [item["text"] for item in await app.memory_import.staged()] == [
        f"Fact {i:02d}" for i in range(1, 10)
    ]
    await press(app, bulk)
    assert app.bot.of("answer")[-1][2] == "already handled"


async def test_approved_imports_stage_when_user_md_is_full_and_organize_only_in_downtime(make_app):
    from datetime import datetime, timedelta, timezone

    app = make_app()
    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    app.dispatch.now.at = now
    # Fill private/USER.md to its exact 1,375-character cap.
    put(app.home, "private/USER.md", "- " + "a" * 1373)
    text = (
        "/import other\n"
        "- Works on GardenShed project\n"
        "- Dentist appointment on 2026-12-05 at 10:00\n"
        "- Prefers black coffee"
    )
    await handlers.handle_message(app, tg(OWNER, OWNER, text))
    pending = await app.proposals.pending()
    assert len(pending) == 3
    # Approving all three succeeds without hitting USER.md's 1,375-char limit.
    for row in pending:
        assert await app.proposals.decide(row["id"], True) == "Approved"
    staged = await app.memory_import.staged()
    assert len(staged) == 3

    # While Dom is online in the owner DM, downtime organizer must NOT run.
    await online(app, OWNER)
    assert await app.memory_import.is_downtime() is False
    await app.memory_import.tick()
    assert len(await app.memory_import.staged()) == 3
    assert not app.script.requests

    # Move owner DM offline (downtime) and provide a structured organizer response.
    await app.store.set_presence(
        OWNER, is_sleeping=False, sleep_until=None, is_offline=True,
        offline_until=(now + timedelta(hours=1)).isoformat(), online_until=None,
    )
    assert await app.memory_import.is_downtime() is True
    plan = {
        "user_md": "- Prefers black coffee",
        "notes_add": ["Drinks black coffee every morning"],
        "entities": [
            {"kind": "projects", "slug": "GardenShed!", "lines": ["Main project: GardenShed"]},
        ],
        "reminders": [
            {"when": "2026-12-05T10:00", "text": "Dentist appointment", "repeat": "", "importance": "normal"},
        ],
    }
    app.script.contents = [json.dumps(plan)]
    app.bot.calls.clear()
    await app.memory_import.tick()

    assert await app.memory_import.staged() == []
    private_home = app.home.scoped(allow_private=True)
    assert private_home.read("private/USER.md").strip() == "- Prefers black coffee"
    assert "Drinks black coffee every morning" in private_home.read("private/NOTES.md")
    project_note = private_home.read("memory/projects/gardenshed.md")
    assert "Main project: GardenShed" in project_note and "{" not in project_note  # untagged = owner-only
    active_reminders = await app.dispatch.reminders.active()
    assert len(active_reminders) == 1 and active_reminders[0]["text"] == "Dentist appointment"
    assert any("Imported memories organized" in call[2] for call in app.bot.of("send", OWNER))
