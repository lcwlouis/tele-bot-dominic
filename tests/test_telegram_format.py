"""Formatting at Telegram's boundary, including delivery and proposal controls."""

import json

from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.methods import SendMessage
import pytest

from dom.actions import Actions, Bubble, utf16_len
from dom.proposals import CARDS_KEY
from dom.telegram.executor import perform
from dom.telegram.format import format_delivery, format_text
from tghelpers import GROUP, OWNER, no_sleep


SHELL = (
    "W=<window id>\n"
    "xdotool windowactivate --sync $W\n"
    "xdotool key --window $W --clearmodifiers ctrl+l; sleep 1\n"
)
SKILL = (
    "## 5. Navigate with xdotool\n\n"
    "Always pass the window id.\n\n"
    f"```bash\n{SHELL}```\n\n"
    "## 6. Verify visually"
)
LONG_SKILL = SKILL.replace(SHELL, SHELL * 64)


def entity_text(text, entity):
    assert 0 <= entity.offset < entity.offset + entity.length <= utf16_len(text)
    utf16 = text.encode("utf-16-le")
    return utf16[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


def test_utf16_and_nested_bold_link():
    (text, entities), = format_text("😀 中文 [look **这里👋**](https://example.org) now")
    assert text == "😀 中文 look 这里👋 now"
    assert [(e.type, e.offset, e.length, entity_text(text, e)) for e in entities] == [
        ("text_link", 6, 9, "look 这里👋"),
        ("bold", 11, 4, "这里👋"),
    ]
    assert entities[0].url == "https://example.org"


@pytest.mark.parametrize("plain", [
    "  hey, 中文 👋  \n\n",
    "what's up? a_b and 2 * 3\r\n  still here",
    '<script>alert("hi")</script>',
    "hey &amp; <b>literal</b>",
    "an unclosed **attempt",
    "```shell\necho oops\n",
])
def test_plain_and_malformed_text_is_unchanged(plain):
    assert format_text(plain) == [(plain, [])]


def test_unsupported_links_and_telegram_incompatible_nesting():
    (text, entities), = format_text(
        "[**label**](ftp://example.org)\n\n"
        "> A **bold `code` bit**, *italic* and ~~gone~~"
    )
    assert text == "label\n\nA bold code bit, italic and gone"
    types = [(e.type, entity_text(text, e)) for e in entities]
    assert ("code", "code") in types
    assert ("italic", "italic") in types and ("strikethrough", "gone") in types
    assert not any(e.type == "text_link" for e in entities)
    code, = (e for e in entities if e.type == "code")
    assert not any(e.offset < code.offset + code.length and code.offset < e.offset + e.length
                   for e in entities if e != code)


def test_style_spanning_link_in_quote_has_only_nested_ranges():
    (text, entities), = format_text("> **[link](https://example.org) and bold** then plain")
    assert text == "link and bold then plain"
    assert [(e.type, entity_text(text, e)) for e in entities] == [
        ("text_link", "link"), ("bold", "link"), ("blockquote", "and bold then plain"), ("bold", "and bold"),
    ]


def test_code_fence_inside_nested_list_and_indented_code():
    (text, entities), = format_text(
        "- Parent\n  1. First\n\n     ```bash\n     echo hi\n     ```\n  2. Second\n\n"
        "Outside\n\n    echo bye"
    )
    assert text == "• Parent\n  1. First\n\n    echo hi\n  2. Second\n\nOutside\n\necho bye"
    assert [(e.type, e.language, entity_text(text, e)) for e in entities] == [
        ("pre", "bash", "echo hi"), ("pre", None, "echo bye"),
    ]


def test_skill_card_renders_headings_and_shell():
    (text, entities), = format_text(SKILL)
    assert text == f"5. Navigate with xdotool\n\nAlways pass the window id.\n\n{SHELL}\n6. Verify visually"
    assert [(e.type, e.language, entity_text(text, e)) for e in entities] == [
        ("bold", None, "5. Navigate with xdotool"),
        ("pre", "bash", SHELL.rstrip("\n")),
        ("bold", None, "6. Verify visually"),
    ]


def test_long_code_splits_at_lines_with_language_on_each_piece():
    chunks = format_text(LONG_SKILL)
    assert len(chunks) >= 2
    code = []
    for text, entities in chunks:
        assert utf16_len(text) <= 4096
        assert not text.startswith("\n") and not text.endswith("\n")
        assert "```" not in text
        for entity in entities:
            part = entity_text(text, entity)
            if entity.type == "pre":
                assert entity.language == "bash"
                code.append(part)
    assert len(code) == 2
    assert "\n".join(code) == (SHELL * 64).rstrip("\n")
    assert all(part.splitlines()[-1] in SHELL.splitlines() for part in code)


def test_split_moves_an_intact_entity_to_next_chunk():
    chunks = format_text("start here\n**two full words**", limit=18)
    assert chunks[0] == ("start here", [])
    text, entities = chunks[1]
    assert text == "two full words"
    assert [(e.type, entity_text(text, e)) for e in entities] == [("bold", text)]


@pytest.mark.parametrize("markdown,kind", [("```bash\n{}\n```", "pre"), ("**{}**", "bold")])
def test_splits_oversized_entity_on_grapheme_boundaries(markdown, kind):
    content = "👩‍💻" * 1000
    chunks = format_text(markdown.format(content))
    assert len(chunks) == 2
    assert "".join(text for text, _ in chunks) == content
    for text, (entity,) in chunks:
        assert text == "👩‍💻" * (len(text) // 3)
        assert utf16_len(text) <= 4096
        assert entity.type == kind and entity_text(text, entity) == text


def test_table_becomes_aligned_code_and_splits_at_line_before_word():
    (text, entities), = format_text("## Table\n\n| A | B |\n|---|---|\n| x | y |")
    assert [e.type for e in entities] == ["bold", "pre"]
    assert entity_text(text, entities[-1]) == "A  B\n─  ─\nx  y"
    assert format_text("one\ntwo three four", limit=10)[0][0] == "one"


async def test_executor_paces_chunks_and_keeps_original_markdown(make_app):
    app = make_app()
    actions = Actions(bubbles=[Bubble(text=LONG_SKILL, reply_to=9)])
    ids = await perform(app.bot, app.store, GROUP, 9, actions, sleep=no_sleep)
    sends = app.bot.of("send", GROUP)
    assert len(ids) == len(sends) == 3
    send_indices = [i for i, call in enumerate(app.bot.calls) if call[0] == "send"]
    for previous, current in zip(send_indices, send_indices[1:]):
        assert any(call[0] == "action" for call in app.bot.calls[previous + 1:current])
    assert sends[0][3]["reply_parameters"].message_id == 9
    assert all(call[3]["reply_parameters"] is None for call in sends[1:])
    assert all("parse_mode" not in call[3] for call in sends)
    assert [call[2] for call in sends] == [text for text, _ in format_text(LONG_SKILL)]
    stored = await app.store.get_recent_chat_window(GROUP)
    assert [message.msg_id for message in stored] == ids
    assert "".join(message.text for message in stored) == LONG_SKILL
    assert stored[0].text.startswith("## 5.")
    assert stored[1].text.startswith("```bash\n")
    assert stored[-1].text.endswith("## 6. Verify visually")


async def test_a_long_skill_proposal_is_a_short_card_with_the_procedure_attached(make_app):
    app = make_app()
    row = {
        "id": 12, "kind": "skill", "title": "Desktop navigation",
        "body_json": json.dumps({"name": "desktop-navigation", "description": "Drive the browser", "old": "", "new": LONG_SKILL}),
    }
    await app.proposals.send(row)
    (_, _, card, options), = app.bot.of("send", OWNER)
    assert "Drive the browser" in card and "The full procedure is attached." in card and "xdotool" not in card
    assert options["reply_markup"].inline_keyboard[0][0].callback_data == "prop:yes:12"
    assert json.loads(await app.store.kv_get(CARDS_KEY)) == {"12": [5001]}
    (_, _, (filename, attached), _), = app.bot.of("document", OWNER)
    assert filename == "desktop-navigation.md" and attached == LONG_SKILL


async def test_entity_rejection_retries_once_as_rendered_plain_text(make_app, caplog):
    app = make_app()
    original = app.bot.send_message
    attempts = []

    async def send(chat_id, text, **options):
        attempts.append((text, options))
        if options.get("entities"):
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text), message="can't parse entities")
        return await original(chat_id, text, **options)

    app.bot.send_message = send
    ids = await perform(app.bot, app.store, GROUP, None, Actions(bubbles=["**hello**"]), sleep=no_sleep)
    assert len(ids) == 1
    assert [text for text, _ in attempts] == ["hello", "hello"]
    assert attempts[0][1]["entities"][0].type == "bold"
    assert "entities" not in attempts[1][1]
    assert "retrying as plain text" in caplog.text
    assert (await app.store.get_recent_chat_window(GROUP))[0].text == "**hello**"


async def test_private_fallback_packs_entities_with_utf16_offsets(make_app):
    app = make_app()
    original = app.bot.send_message

    async def send(chat_id, text, **kwargs):
        if chat_id == GROUP and app.bot.of("send", GROUP):
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text), message="BOT_NOT_ADMIN")
        return await original(chat_id, text, **kwargs)

    app.bot.send_message = send
    ids = await perform(
        app.bot, app.store, GROUP, None, Actions(private=True, bubbles=["**first**", "😀 **second**"]),
        ephemeral_recipient=OWNER, sleep=no_sleep,
    )
    group, = app.bot.of("send", GROUP)
    assert group[3]["ephemeral_message_parameters"].receiver_user_id == OWNER
    assert entity_text(group[2], group[3]["entities"][0]) == "first"
    dm, = app.bot.of("send", OWNER)
    assert len(ids) == 2 and dm[2].endswith("\n\n😀 second")
    assert entity_text(dm[2], dm[3]["entities"][0]) == "second"


async def test_migration_retries_only_unsent_rendered_chunks(make_app):
    app = make_app()
    original = app.bot.send_message
    new_group = GROUP - 1
    remaining = []

    async def send(chat_id, text, **kwargs):
        if chat_id == GROUP and app.bot.of("send", GROUP):
            raise TelegramMigrateToChat(method=SendMessage(chat_id=chat_id, text=text), message="migrated",
                                        migrate_to_chat_id=new_group)
        return await original(chat_id, text, **kwargs)

    app.bot.send_message = send
    with pytest.raises(TelegramMigrateToChat):
        await perform(app.bot, app.store, GROUP, None, Actions(bubbles=[LONG_SKILL]),
                      sleep=no_sleep, on_migrate=remaining.append)
    await perform(app.bot, app.store, new_group, None, remaining[0], sleep=no_sleep)
    calls = app.bot.of("send")
    assert [call[1] for call in calls] == [GROUP, new_group, new_group]
    assert [(call[2], call[3]["entities"]) for call in calls] == format_text(LONG_SKILL)


@pytest.mark.parametrize("casual", ["lol *sigh*", "5*6*7", "- a", "1) x", "_whispers_ fine", "2*3=6"])
def test_casual_lines_without_clear_structure_go_out_exactly_as_written(casual):
    [(text, entities)] = [(chunk[0], chunk[1]) for chunk in format_text(casual)]
    assert text == casual and not entities


def test_emphasis_renders_once_the_text_has_clear_structure():
    [(text, entities)] = [(chunk[0], chunk[1]) for chunk in format_text("**done** and *nearly*")]
    assert text == "done and nearly"
    assert [(e.type, e.offset, e.length) for e in entities] == [("bold", 0, 4), ("italic", 9, 6)]


def test_a_table_goes_out_as_aligned_columns_in_a_code_block():
    source = "| thing | status |\n|---|---|\n| bold | works |\n| 東京 | ok |\n\nafter"
    [(text, entities)] = [(chunk[0], chunk[1]) for chunk in format_text(source)]
    table = "thing  status\n─────  ──────\nbold   works\n東京   ok"
    assert text == f"{table}\n\nafter"
    assert [(e.type, e.offset, e.length) for e in entities] == [("pre", 0, utf16_len(table))]
    pieces = format_delivery(source)
    assert "".join(recorded for _, recorded in pieces) == source
