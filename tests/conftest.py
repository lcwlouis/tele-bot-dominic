import httpx
import pytest
import pytest_asyncio

from dom.dispatch import Dispatcher
from dom.presence import Presence
from dom.config import Config, ModelEndpoint
from dom.home import Home
from dom.llm import ChatClient
from dom.store import Store
from dom.telegram import handlers
from tghelpers import BOT_USER, OWNER, Clock, FakeBot, FixedRandom, ModelScript, no_sleep


@pytest.fixture(autouse=True)
def lan_probes_answer(monkeypatch):
    """Tests never touch the network: every LAN probe and host address answers from the host unless a test
    says otherwise (the desktop guards count a sandbox timeout only for targets that answer from the host)."""
    from dom.desktop import network

    async def answers(target, seconds=network.PROBE_SECONDS):
        return True

    monkeypatch.setattr(network, "host_answers", answers)


@pytest.fixture
def browser(monkeypatch):
    """An authenticated client for the dashboard's ASGI app: `async with browser(app) as client`."""
    from dom.web.app import create_app
    from dom.web.auth import Auth
    from test_web_auth import ORIGIN, TOKEN

    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)

    def connect(app):
        config = app.config.model_copy(update={
            "web": app.config.web.model_copy(update={"dev_token_env": "DOM_TEST_TOKEN"}),
        })
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app, Auth(config))),
            base_url=ORIGIN, headers={"Authorization": f"Bearer {TOKEN}"},
        )

    return connect


@pytest_asyncio.fixture
async def make_app(tmp_path):
    apps = []

    def make(owner_id=OWNER, contents=()):
        endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env=None, input=("text",))  # text-only: no probe call
        config = Config(  # the leak check ships on; tests that are not about it keep their Jev calls to themselves
            home=tmp_path, owner_id=owner_id, timezone="UTC", models={"chat": endpoint}, leak_check=False,
        )
        script = ModelScript(*contents)
        client = ChatClient(endpoint, None, httpx.AsyncClient(transport=httpx.MockTransport(script)), sleep=no_sleep)
        store = Store(tmp_path, owner_id=owner_id)
        app = handlers.App(
            config=config, home=Home(tmp_path), store=store, client=client,
            bot=FakeBot(), bot_username="dombot", sleep=no_sleep, bot_id=BOT_USER.id,
        )
        clock = Clock()
        presence = Presence(store, app.dispatch.presence.tz, FixedRandom(), clock)
        app.dispatch = Dispatcher(app, presence, None, no_sleep, clock)
        apps.append(app)
        app.script = script
        return app

    yield make
    for app in apps:
        await app.self_work.close()
        if app.desktop_worker is not None:
            await app.desktop_worker.close()
        await app.dispatch.close()
        app.store.close()
