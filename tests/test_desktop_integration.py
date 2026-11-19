"""Optional contract check on an existing AIO Sandbox."""

import os
from uuid import uuid4

import httpx
import pytest

from dom.desktop.client import SandboxClient


async def test_aio_sandbox_contract():
    url = os.environ.get("DOM_SANDBOX_URL")
    if not url:
        pytest.skip("DOM_SANDBOX_URL is not set")
    async with httpx.AsyncClient() as http:
        client = SandboxClient(url, None, http)
        await client.navigate("data:text/html,%3Ch1%3EDom%20desktop%3C/h1%3E")
        assert "Dom desktop" in str(await client.markdown())
        result = await client.jupyter("print(6 * 7)")
        assert "42" in str(result["outputs"])
        assert (await client.bash("printf hello"))["stdout"].strip() == "hello"
        path = f"/tmp/dom-desktop-test-{uuid4().hex}.txt"
        try:
            await client.write_file(path, "desktop test")
            assert (await client.read_file(path))["content"].strip() == "desktop test"
        finally:
            await client.bash(f"rm -f {path}")
