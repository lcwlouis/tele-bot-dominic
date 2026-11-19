import json

import pytest

from dom.home import Home, PrivateAccessError
from dom.models import Scope
from dom.prompt import build_messages
from dom.store import Store
from dom.tools import TurnCtx, core_tools

SECRET = "owner-only secret"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "private" / "NOTES.md").write_text(SECRET)
    (tmp_path / "SOUL.md").write_text("soul")
    (tmp_path / "memory" / "MEMORY.md").symlink_to(tmp_path / "private" / "NOTES.md")
    return tmp_path


def test_symlink_into_private_not_readable_by_public_home(root):
    with pytest.raises(PrivateAccessError):
        Home(root).read("memory/MEMORY.md")
    assert Home(root, allow_private=True).read("memory/MEMORY.md") == SECRET


def test_symlink_into_private_not_writable_by_public_home(root):
    with pytest.raises(PrivateAccessError):
        Home(root).memory_write("memory/MEMORY.md", "add", "x")
    assert (root / "private" / "NOTES.md").read_text() == SECRET


def test_symlink_outside_home_rejected(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.md"
    outside.write_text("elsewhere")
    (root / "memory" / "HABITS.md").symlink_to(outside)
    with pytest.raises(ValueError):
        Home(root, allow_private=True).read("memory/HABITS.md")
    result = Home(root).memory_write("memory/HABITS.md", "add", "x")
    assert result.ok is False
    assert outside.read_text() == "elsewhere"


def test_symlink_not_leaked_into_group_prompt(root):
    messages = build_messages(Home(root), "group", -1009, [222], "<chat_window/>", "note")
    assert SECRET not in json.dumps(messages)


async def test_symlink_not_leaked_by_memory_tool_error(root):
    store = Store(root, owner_id=111)
    try:
        ctx = TurnCtx(scope="group", chat_id=-1009, sender_id=222, home=Home(root), store=store, owner_id=111)
        memory = next(t for t in core_tools() if t.name == "memory")
        try:
            out = await memory.handler({"file": "MEMORY.md", "op": "add", "text": "x"}, ctx)
        except PrivateAccessError as exc:
            out = str(exc)
        assert SECRET not in out
    finally:
        store.close()
