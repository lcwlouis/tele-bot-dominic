import json

from dom.prompt import build_messages
from dom.telegram import handlers
from test_owner_controls import approve, buttons, callback
from tghelpers import FRIEND, GROUP, OWNER, tg


def put(home, rel, text):
    path = home.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_normalise_aliases_and_clamps():
    from dom.traits import normalise

    assert normalise({
        "sarcasm_level": 1.8, "singlish_level": "-1", "emoji": 0.2,
        "unknown": 0.5, "humor": "nan", "empathy": True,
    }) == {"sarcasm": 0.95, "slang": 0.05, "emoji": 0.2}


async def test_effective_traits_precedence_and_stable_prefix(make_app):
    from dom.traits import effective, for_chat

    app = make_app()
    put(app.home, "SOUL.md", "---\ntraits:\n  humor: 0.5\n  emoji: 0.5\n  empathy: 0.5\n---\nShared soul.")
    put(app.home, "presets/a.md", "---\ntraits:\n  humor: 0.3\n  emoji_level: 0.2\n---\nFirst & overlay.")
    put(app.home, "presets/b.md", "Second overlay.")
    put(app.home, "private/USER.md", "OWNER-ONLY-NOTE")
    await approve(app, GROUP)
    await approve(app, OWNER)
    await app.store.kv_set("traits:drift", json.dumps({"empathy": 0.1, "humor": 0.1, "emoji": 0.1}))
    await app.store.set_chat_preset(GROUP, "a")
    await app.store.set_chat_preset(OWNER, "b")
    await app.store._run(app.store._conn.execute, "UPDATE chats SET traits_json = ? WHERE chat_id = ?",
                         (json.dumps({"emoji_level": 0.8}), str(GROUP)))
    assert await effective(app.home, app.store, GROUP) == {"humor": 0.3, "emoji": 0.8, "empathy": 0.6}

    async def prompt(scope, chat_id):
        sliders, preset = await for_chat(app.home, app.store, chat_id)
        return build_messages(app.home, scope, chat_id, [], "<chat_window/>", "note",
                              sliders=sliders, preset=preset)

    group, owner = await prompt("group", GROUP), await prompt("owner_dm", OWNER)
    assert group[0] == owner[0]
    assert group[1]["content"].index("<chat_window/>") < group[1]["content"].index("Personality sliders")
    assert '<preset name="a">First &amp; overlay.</preset>' in group[1]["content"]
    assert '<preset name="b">Second overlay.</preset>' in owner[1]["content"]
    assert "<preset " not in group[0]["content"]
    assert "OWNER-ONLY-NOTE" not in str(group)
    assert "OWNER-ONLY-NOTE" in owner[1]["content"]


async def test_unknown_preset_is_ignored_and_long_overlay_is_truncated(make_app, caplog):
    from dom.traits import for_chat

    app = make_app()
    await approve(app, GROUP)
    await app.store.set_chat_preset(GROUP, "missing")
    assert await for_chat(app.home, app.store, GROUP) == ({}, None)
    assert "unknown preset" in caplog.text
    put(app.home, "presets/long.md", "x" * 801)
    await app.store.set_chat_preset(GROUP, "long")
    _, preset = await for_chat(app.home, app.store, GROUP)
    assert preset.overlay == "x" * 800
    assert "truncated" in caplog.text


async def test_preset_buttons_work_only_in_owner_dm(make_app):
    app = make_app()
    put(app.home, "presets/classic.md", "---\ntitle: Classic\n---\n")
    await approve(app, GROUP, "Book Club")
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/preset"))
    [(_, _, _, kw)] = app.bot.of("send", OWNER)
    assert buttons(kw["reply_markup"]) == [
        [("Everywhere · None", "preset:all")], [("Book Club · uses Everywhere", f"preset:{GROUP}")],
        [("✕ Close", "card:close")],
    ]
    await handlers.handle_callback(app, callback(app, OWNER, f"preset:{GROUP}"))
    assert buttons(app.bot.of("send", OWNER)[-1][3]["reply_markup"]) == [
        [("Classic", f"presetset:{GROUP}:classic")], [("None", f"presetset:{GROUP}:")],
        [("✕ Close", "card:close")],
    ]

    data = f"presetset:{GROUP}:classic"
    await handlers.handle_callback(app, callback(app, FRIEND, data))
    await handlers.handle_callback(app, callback(app, OWNER, data, message=tg()))
    assert (await app.store.list_chats("approved"))[0]["preset"] == ""
    await handlers.handle_callback(app, callback(app, OWNER, data))
    assert (await app.store.list_chats("approved"))[0]["preset"] == "classic"
    assert "Classic" in app.bot.of("send", OWNER)[-1][2]
    events = await app.store.get_recent_chat_window(OWNER)
    assert any(m.role == "event" and "Book Club" in m.text for m in events)
    await handlers.handle_callback(app, callback(app, OWNER, f"presetset:{GROUP}:"))
    assert (await app.store.list_chats("approved"))[0]["preset"] == ""


async def test_global_preset_layers_under_the_chat_preset_and_overrides(make_app):
    from dom.traits import for_chat

    app = make_app()
    put(app.home, "SOUL.md", "---\ntraits:\n  humor: 0.5\n  emoji: 0.5\n  empathy: 0.5\n---\nShared soul.")
    put(app.home, "presets/calm.md", "---\ntraits:\n  humor: 0.2\n  empathy: 0.9\n  emoji: 0.1\n---\nCalm overlay.")
    put(app.home, "presets/loud.md", "---\ntraits:\n  humor: 0.8\n---\nLoud overlay.")
    await approve(app, GROUP)
    await approve(app, OWNER)
    await app.store.kv_set("traits:drift", json.dumps({"empathy": 0.1}))
    await app.store.kv_set("preset:global", "calm")

    # no own preset: the global one applies, on top of soul and drift
    sliders, preset = await for_chat(app.home, app.store, GROUP)
    assert sliders == {"humor": 0.2, "emoji": 0.1, "empathy": 0.9} and preset.name == "calm"

    # an own preset wins where they overlap, the global one still fills the rest, the overlay is the chat's
    await app.store.set_chat_preset(GROUP, "loud")
    sliders, preset = await for_chat(app.home, app.store, GROUP)
    assert sliders == {"humor": 0.8, "emoji": 0.1, "empathy": 0.9} and preset.name == "loud"

    # slider overrides stay on top of both
    await app.store._run(app.store._conn.execute, "UPDATE chats SET traits_json = ? WHERE chat_id = ?",
                         (json.dumps({"humor": 0.4}), str(GROUP)))
    sliders, _ = await for_chat(app.home, app.store, GROUP)
    assert sliders["humor"] == 0.4

    # an own preset that cannot be loaded falls back to the global one
    await app.store.set_chat_preset(OWNER, "missing")
    _, preset = await for_chat(app.home, app.store, OWNER)
    assert preset.name == "calm"

    async def prompt(scope, chat_id):
        sliders, preset = await for_chat(app.home, app.store, chat_id)
        return build_messages(app.home, scope, chat_id, [], "<chat_window/>", "note", sliders=sliders, preset=preset)

    group, owner = await prompt("group", GROUP), await prompt("owner_dm", OWNER)
    assert '<preset name="loud">Loud overlay.</preset>' in group[1]["content"]
    assert '<preset name="calm">Calm overlay.</preset>' in owner[1]["content"]
    assert "Calm overlay" not in group[1]["content"]
    assert group[0] == owner[0]

    await app.store.kv_set("preset:global", "")
    await app.store.set_chat_preset(GROUP, "")
    assert await for_chat(app.home, app.store, GROUP) == ({"humor": 0.4, "emoji": 0.5, "empathy": 0.6}, None)


async def test_preset_everywhere_flow(make_app):
    app = make_app()
    put(app.home, "presets/classic.md", "---\ntitle: Classic\n---\n")
    await approve(app, GROUP, "Book Club")
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/preset"))
    await handlers.handle_callback(app, callback(app, OWNER, "preset:all"))
    markup = app.bot.of("send", OWNER)[-1][3]["reply_markup"]
    assert buttons(markup) == [
        [("Classic", "presetset:all:classic")], [("Auto (Dom picks daily)", "presetset:all:auto")], [("None", "presetset:all:")],
        [("✕ Close", "card:close")],
    ]

    data = "presetset:all:classic"
    await handlers.handle_callback(app, callback(app, FRIEND, data))
    await handlers.handle_callback(app, callback(app, OWNER, data, message=tg()))
    assert await app.store.kv_get("preset:global") is None
    await handlers.handle_callback(app, callback(app, OWNER, "presetset:all:missing"))
    assert await app.store.kv_get("preset:global") is None

    await handlers.handle_callback(app, callback(app, OWNER, data))
    assert await app.store.kv_get("preset:global") == "classic"
    assert "Classic" in app.bot.of("send", OWNER)[-1][2]
    events = await app.store.get_recent_chat_window(OWNER)
    assert any(m.role == "event" and "set Dom's preset everywhere to Classic" in m.text for m in events)

    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/preset"))
    assert buttons(app.bot.of("send", OWNER)[-1][3]["reply_markup"])[0] == [("Everywhere · Classic", "preset:all")]
    await app.store.set_chat_preset(GROUP, "classic")
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/preset"))
    assert buttons(app.bot.of("send", OWNER)[-1][3]["reply_markup"])[1] == [("Book Club · Classic", f"preset:{GROUP}")]

    await handlers.handle_callback(app, callback(app, OWNER, "presetset:all:"))
    assert await app.store.kv_get("preset:global") == ""
    events = await app.store.get_recent_chat_window(OWNER)
    assert any(m.role == "event" and "everywhere to None" in m.text for m in events)
