from dom.stickers import resolve_sticker, sticker_fallback

TABLE = {"📚": ["a", "b"], "📚2": ["b"], "🍵": ["c"]}


def test_a_numbered_pick_sends_that_sticker_and_stores_the_bare_emoji():
    assert resolve_sticker(TABLE, "📚2") == (["b"], "📚")
    assert resolve_sticker(TABLE, " 📚 ") == (["a", "b"], "📚")


def test_an_unknown_number_falls_back_to_any_sticker_of_the_emoji():
    for raw in ("📚9", "📚0", "📚 3", "📚1"):
        assert resolve_sticker(TABLE, raw) == (["a", "b"], "📚")


def test_words_or_unknown_emoji_resolve_to_nothing():
    assert resolve_sticker(TABLE, "📚2 Owl on a book stack") is None
    assert resolve_sticker(TABLE, "🦉") is None
    assert resolve_sticker(None, "📚") is None
    assert resolve_sticker(TABLE, "") is None


def test_the_text_fallback_is_only_ever_a_bare_emoji():
    assert sticker_fallback("📚2") == "📚"
    assert sticker_fallback("🍵") == "🍵"
    assert sticker_fallback("📚2 Owl on a book stack") is None
    assert sticker_fallback("hello") is None
    assert sticker_fallback("7") is None
