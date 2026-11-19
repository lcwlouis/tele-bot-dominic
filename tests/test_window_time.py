from zoneinfo import ZoneInfo

from dom.models import StoredMessage
from dom.store.store import format_chat_window_xml


def _msg(created_at: str, role: str = "user") -> StoredMessage:
    return StoredMessage(
        id=1, chat_id="111", msg_id=5, reply_to_msg_id=None, sender_user_id=111, sender_handle="",
        sender_name="Fran", is_bot=False, role=role, visibility_scope="owner_private", text="hi",
        media_json=None, is_edited=False, original_seen_before_edit=False, previous_seen_text=None,
        is_deleted=False, created_at=created_at, updated_at=None,
    )


def test_window_times_are_local_when_a_timezone_is_given():
    # The turn note says the local time; UTC stamps next to it made the model think a
    # one-minute-old message was eight hours old.
    xml = format_chat_window_xml("111", [_msg("2026-11-19T09:20:31+00:00")], ZoneInfo("Asia/Singapore"))
    assert 'time="2026-11-19 17:20"' in xml


def test_naive_stamps_are_treated_as_utc_and_events_converted_too():
    tz = ZoneInfo("Asia/Singapore")
    xml = format_chat_window_xml("111", [_msg("2026-11-19T09:20:31"), _msg("2026-11-19T10:00:00+00:00", "event")], tz)
    assert 'time="2026-11-19 17:20"' in xml and '<event time="2026-11-19 18:00">' in xml


def test_without_a_timezone_the_stamp_is_unchanged():
    assert 'time="2026-11-19T09:20:31+00:00"' in format_chat_window_xml("111", [_msg("2026-11-19T09:20:31+00:00")])
