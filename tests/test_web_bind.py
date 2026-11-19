from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn

from dom.config import load_config
from dom.home import init_home
from dom.web.app import DashboardServer, serving
from dom.web.auth import Auth


@pytest.mark.parametrize("bind,port", [(None, 8080), ("192.0.2.1", 9090)])
async def test_dashboard_uses_configured_socket(tmp_path, monkeypatch, bind, port):
    init_home(tmp_path)
    path = tmp_path / "config.toml"
    config_text = path.read_text().replace("enabled = false", "enabled = true").replace(
        'dev_token_env = ""', 'dev_token_env = "DOM_TEST_TOKEN"',
    )
    if bind is not None:
        config_text = config_text.replace('bind = "127.0.0.1"', f'bind = "{bind}"').replace("port = 8080", f"port = {port}")
    path.write_text(config_text)
    monkeypatch.setenv("DOM_TEST_TOKEN", "test-token")
    config = load_config(tmp_path)
    dom = SimpleNamespace(config=config, owner_id=None)
    calls = []
    listener = object()

    def bind_socket(uv_config):
        calls.append((uv_config.host, uv_config.port))
        return nullcontext(listener)

    serve = AsyncMock()
    monkeypatch.setattr(uvicorn.Config, "bind_socket", bind_socket)
    monkeypatch.setattr(DashboardServer, "serve", serve)

    async with serving(dom, Auth(config)):
        assert calls == [(bind or "127.0.0.1", port)]
    serve.assert_awaited_once_with(sockets=[listener])


async def test_blank_bind_refused_before_opening_a_socket(tmp_path, monkeypatch):
    init_home(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(path.read_text().replace('bind = "127.0.0.1"', 'bind = ""').replace(
        'dev_token_env = ""', 'dev_token_env = "DOM_TEST_TOKEN"',
    ))
    monkeypatch.setenv("DOM_TEST_TOKEN", "test-token")
    config = load_config(tmp_path)
    dom = SimpleNamespace(config=config, owner_id=None)
    monkeypatch.setattr(
        uvicorn.Config, "bind_socket", lambda _: pytest.fail("a blank bind would expose all interfaces"),
    )

    with pytest.raises(ValueError, match="web.bind"):
        async with serving(dom, Auth(config)):
            pytest.fail("blank bind was accepted")
