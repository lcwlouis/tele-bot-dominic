"""Owner-only tools in the owner's DM, including turns whose newest message has no sender."""

from dom.tools import TurnCtx
from tghelpers import FRIEND, GROUP, OWNER


def ctx(app, *, chat_id, sender_id, scope):
    return TurnCtx(home=app.home, store=app.store, chat_id=chat_id, sender_id=sender_id, owner_id=OWNER, scope=scope, app=app)


def test_a_turn_in_the_owners_dm_with_no_sender_is_still_the_owners(make_app):
    # Live on the real Dom (2026-11-25): a memory edit was refused in the owner's own DM because the newest message
    # was Dom's own, so the turn had no sender.
    app = make_app()
    assert ctx(app, chat_id=OWNER, sender_id=OWNER, scope="owner_dm").is_owner_dm()
    assert ctx(app, chat_id=OWNER, sender_id=None, scope="owner_dm").is_owner_dm()


def test_anyone_or_anywhere_else_is_not_the_owners_dm(make_app):
    app = make_app()
    assert not ctx(app, chat_id=OWNER, sender_id=FRIEND, scope="owner_dm").is_owner_dm()
    assert not ctx(app, chat_id=FRIEND, sender_id=None, scope="owner_dm").is_owner_dm()
    assert not ctx(app, chat_id=GROUP, sender_id=OWNER, scope="group").is_owner_dm()
    assert not ctx(app, chat_id=GROUP, sender_id=None, scope="group").is_owner_dm()
