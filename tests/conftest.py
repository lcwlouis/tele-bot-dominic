import httpx
import pytest

from dom.config import Config, ModelEndpoint
from dom.home import Home
from dom.llm import ChatClient
from dom.store import Store
from dom.telegram import handlers
from tghelpers import OWNER, FakeBot, ModelScript, no_sleep


@pytest.fixture
def make_app(tmp_path):
    stores = []

    def make(owner_id=OWNER, contents=()):
        endpoint = ModelEndpoint(base_url="https://llm.test/v1", model="m", api_key_env=None)
        config = Config(home=tmp_path, owner_id=owner_id, timezone="UTC", models={"chat": endpoint})
        script = ModelScript(*contents)
        client = ChatClient(endpoint, None, httpx.AsyncClient(transport=httpx.MockTransport(script)), sleep=no_sleep)
        store = Store(tmp_path, owner_id=owner_id)
        stores.append(store)
        app = handlers.App(
            config=config, home=Home(tmp_path), store=store, client=client,
            bot=FakeBot(), bot_username="dombot", sleep=no_sleep,
        )
        app.script = script
        return app

    yield make
    for s in stores:
        s.close()
