import re
import stat

import pytest

from dom import cli
from dom.claim import try_claim
from dom.config import load_config
from dom.home import init_home
from dom.telegram import handlers

from tghelpers import OWNER, drain_dispatch, tg

CODE = "AB12CD34"


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    init_home(root)
    (root / ".claim").write_text(CODE)
    return root


def claim(home, sender=OWNER, chat_type="private", code=CODE):
    return try_claim(home, load_config(home), sender, chat_type, code)


def test_claim_success_sets_owner_and_removes_code(home):
    assert claim(home) is True
    assert load_config(home).owner_id == OWNER
    assert not (home / ".claim").exists()


def test_claim_rejected_when_owner_set(home):
    assert claim(home, sender=111) is True
    (home / ".claim").write_text(CODE)  # even if a code file reappears
    assert claim(home, sender=222) is False
    assert load_config(home).owner_id == 111


def test_claim_wrong_code_rejected(home):
    assert claim(home, code="WRONG000") is False
    assert claim(home, code="") is False
    assert load_config(home).owner_id is None
    assert (home / ".claim").exists()


def test_claim_in_group_rejected(home):
    assert claim(home, chat_type="group") is False
    assert claim(home, chat_type="supergroup") is False
    assert load_config(home).owner_id is None


def test_claim_without_claim_file_rejected(home):
    (home / ".claim").unlink()
    assert claim(home) is False


def test_init_writes_claim_code_0600(tmp_path, capsys):
    target = tmp_path / "newhome"
    assert cli.main(["init", str(target)]) == 0
    claim_file = target / ".claim"
    assert stat.S_IMODE(claim_file.stat().st_mode) == 0o600
    code = claim_file.read_text().strip()
    assert re.fullmatch(r"[A-Z0-9]{8}", code)
    out = capsys.readouterr().out
    assert f"/claim {code}" in out and ".env" in out and "dom run" in out
    assert (target / "SOUL.md").is_file()


def test_init_refuses_nonempty_home(tmp_path, capsys):
    (tmp_path / "x.txt").write_text("keep")
    assert cli.main(["init", str(tmp_path)]) == 1
    assert not (tmp_path / ".claim").exists()


def test_init_codes_differ(tmp_path):
    cli.main(["init", str(tmp_path / "a")])
    cli.main(["init", str(tmp_path / "b")])
    assert (tmp_path / "a" / ".claim").read_text() != (tmp_path / "b" / ".claim").read_text()


def test_build_app_reads_secrets_from_dotenv(home, monkeypatch):
    (home / ".env").write_text("OPENROUTER_API_KEY=sk-test\nTELEGRAM_BOT_TOKEN=123:abc\n")
    config = load_config(home)
    assert cli.require_secret(config, "TELEGRAM_BOT_TOKEN") == "123:abc"
    assert cli.model_api_key(config) == "sk-test"
    with pytest.raises(SystemExit):
        cli.require_secret(config, "NOT_SET_ANYWHERE")


# ---- the /claim flow through the bot ------------------------------------------------

async def test_claim_flow_makes_sender_owner_and_approves_dm(make_app, tmp_path):
    app = make_app(owner_id=None)
    (tmp_path / "config.toml").write_text('[models.chat]\nbase_url = "https://x/v1"\nmodel = "m"\n')
    (tmp_path / ".claim").write_text(CODE)
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text=f"/claim {CODE}"))
    assert app.owner_id == OWNER and app.config.owner_id == OWNER
    assert load_config(tmp_path).owner_id == OWNER
    assert len(app.bot.of("send", OWNER)) == 1
    assert (await app.store.get_chat_auth_status(OWNER))["authorization_status"] == "approved"
    assert app.script.requests == []  # the claim itself is not a chat turn
    # The newly claimed owner can wake Dom and talk to him.
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="/wake", msg_id=2))
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text="hello", msg_id=3))
    await drain_dispatch(app.dispatch)
    assert len(app.script.requests) == 1


async def test_claim_by_second_person_or_wrong_code_changes_nothing(make_app, tmp_path):
    app = make_app(owner_id=None)
    (tmp_path / "config.toml").write_text('[models.chat]\nbase_url = "https://x/v1"\nmodel = "m"\n')
    (tmp_path / ".claim").write_text(CODE)
    await handlers.handle_message(app, tg(chat_id=222, sender=222, text="/claim WRONG000"))
    await handlers.handle_message(app, tg(chat_id=-1009, sender=222, text=f"/claim {CODE}"))
    assert app.owner_id is None
    assert (tmp_path / ".claim").exists()
    await handlers.handle_message(app, tg(chat_id=OWNER, sender=OWNER, text=f"/claim {CODE}"))
    assert app.owner_id == OWNER
    await handlers.handle_message(app, tg(chat_id=222, sender=222, text=f"/claim {CODE}"))
    assert app.owner_id == OWNER and load_config(tmp_path).owner_id == OWNER


async def test_run_bounds_handler_drain_and_cancels_pending_deletions(home, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from dom.telegram import executor

    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=t\nOPENROUTER_API_KEY=k\n")
    seen = {}
    events = []

    class FakeBot:
        def __init__(self, token):
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            events.append(("session", seen["task"].done()))

        async def get_me(self):
            return SimpleNamespace(id=999, username="dombot")

    class FakeDispatcher:
        def include_router(self, router):
            pass

        async def start_polling(self, bot, **kwargs):
            seen.update(kwargs)
            forever = asyncio.Event()

            async def blocked(_):
                await forever.wait()

            task = asyncio.create_task(executor._delete_later(bot, None, 1, 2, 5, blocked))
            executor._background.add(task)
            task.add_done_callback(executor._background.discard)
            seen["task"] = task
            await asyncio.sleep(0)

    monkeypatch.setattr(cli, "Bot", FakeBot)
    monkeypatch.setattr(cli, "Dispatcher", FakeDispatcher)
    real_close = cli.Store.close
    monkeypatch.setattr(cli.Store, "close", lambda self: (events.append(("store", seen["task"].done())), real_close(self)))
    await cli.run(home)
    assert 0 < seen["tasks_shutdown_timeout"] <= 30
    assert seen["task"].cancelled()
    assert not executor._background
    assert sorted(events) == [("session", True), ("store", True)]  # deletions were cancelled before either closed
    assert seen["close_bot_session"] is False
