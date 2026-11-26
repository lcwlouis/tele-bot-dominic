import asyncio
import base64
from contextlib import suppress
from ipaddress import ip_network
import os
from pathlib import Path
import signal
import socket
import sys

import httpx
import pytest

from studio_service.config import Config, GIB, MIB, read_token
from studio_service.http import create_app
from studio_service.memory import Reading, parse_vm_stat
from studio_service.process import WorkerError
from studio_service.service import DEADLINES, LowMemory, Studio
from studio_service.sources import Sources

PEER = ("192.0.2.2", 1234)
TOKEN = b"k" * 48


class Memory:
    available = 64 * GIB

    async def __call__(self, groups):
        return Reading(self.available, len(groups) * GIB)


@pytest.fixture
def config(tmp_path):
    encoder = tmp_path / "encoder"
    encoder.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
                       "Path(sys.argv[-1]).write_bytes(b'fake')\n")
    encoder.chmod(0o700)
    portrait = tmp_path / "portrait.png"
    portrait.write_bytes(b"portrait")
    return Config.model_validate({
        "bind_host": "192.0.2.1", "port": 8080, "token_file": tmp_path / "token",
        "allowed_sources": ["192.0.2.0/24"],
        "ffmpeg": encoder, "default_outfit": "default",
        "outfits": {"default": {"portrait": portrait, "turnaround": tmp_path / "turnaround.png"}},
        "workers": {
            kind: {"command": [sys.executable, str(Path(__file__).with_name("fake_worker.py")), kind]}
            for kind in DEADLINES
        },
    })


@pytest.fixture
async def studio(config):
    studio = Studio(config, Memory())
    await studio.read_memory()
    yield studio
    await studio.close()


def client(studio, peer=PEER, *, authorized=True, extra_headers=None):
    app = create_app(studio, Sources(studio.config), TOKEN)
    headers = {"authorization": "Bearer " + TOKEN.decode()} if authorized else {}
    headers.update(extra_headers or {})
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://studio.example", headers=headers,
    )


def test_vm_stat():
    raw = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                            166.
Pages inactive:                     952632.
Pages purgeable:                    142153.
Pages speculative:                       0.
"""
    assert parse_vm_stat(raw) == (166 + 952632 + 142153) * 16384
    with pytest.raises(ValueError):
        parse_vm_stat(raw.replace("Pages inactive", "Pages active"))


def test_reserve_and_token(config):
    desktop = {"max_containers": 2, "container_memory_mb": 1600, "min_free_memory_mb": 768}
    config = Config.model_validate({**config.model_dump(), "desktop": desktop})
    assert config.reserve == 16 * GIB + 3968 * MIB
    config.token_file.write_bytes(TOKEN + b"\n")
    config.token_file.chmod(0o644)
    with pytest.raises(ValueError):
        read_token(config.token_file)
    config.token_file.chmod(0o600)
    assert read_token(config.token_file) == TOKEN


@pytest.mark.parametrize("suffix", ["local", "tailnet-example." + "ts.net"])
@pytest.mark.parametrize("lookup_error", [socket.gaierror, TimeoutError])
async def test_source_policy_and_dns_replacement(config, monkeypatch, lookup_error, suffix):
    allowed = ["0.0.0.0/0", "::/0"]
    deny = ["198.51.100.0/24"]
    source = Sources(config.model_copy(update={"allowed_sources": allowed, "denied_sources": deny}),
                     ["192.0.2.3"])
    default_vm_peer = str(ip_network("192.168.65.0/24")[5])
    bridge_peer = str(ip_network("172.16.0.0/12")[3])
    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", default_vm_peer, bridge_peer,
                 "192.0.2.1", "192.0.2.3", "198.51.100.1"):
        assert not source.permits((host, 1))
    assert source.permits(PEER)

    name = "bot." + suffix
    source = Sources(config.model_copy(update={"allowed_sources": [name]}))

    async def lookup(host, *args, **kwargs):
        assert host == name
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", PEER)]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    await source.refresh()
    assert source.permits(PEER)

    async def fail(*args, **kwargs):
        raise lookup_error()

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fail)
    await source.refresh()
    assert source.permits(PEER)

    replacement = ("192.0.2.4", 1234)

    async def new_lookup(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", replacement)]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", new_lookup)
    await source.refresh()
    assert not source.permits(PEER)
    assert source.permits(replacement)


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/v1/health", None),
    ("GET", "/v1/portrait/default", None),
    ("POST", "/v1/voice", {"text": "hello"}),
    ("POST", "/v1/video_note", {"text": "hello"}),
    ("POST", "/v1/image", {"scene": "hello"}),
])
async def test_auth_and_source(studio, method, path, payload):
    async with client(studio, authorized=False) as external:
        assert (await external.request(method, path, json=payload)).status_code == 401
    async with client(studio, peer=("127.0.0.1", 1),
                      extra_headers={"x-forwarded-for": PEER[0]}) as forbidden:
        response = await forbidden.request(method, path, json=payload)
        assert response.status_code == 403
        assert not response.content


@pytest.mark.parametrize("path,payload,mime", [
    ("voice", {"text": "x" * 600, "seed": 2**64 - 1}, "audio/ogg"),
    ("video_note", {"text": "x" * 200}, "video/mp4"),
    ("video_note", {"text": "x" * 120, "special": True}, "video/mp4"),
    ("image", {"scene": "hello", "outfit": "unknown", "size": 768}, "image/png"),
])
async def test_generate(studio, path, payload, mime):
    async with client(studio) as http:
        response = await http.post(f"/v1/{path}", json=payload)
        assert response.status_code == 200
        assert response.headers["content-type"] == mime
        assert response.headers["cache-control"] == "no-store"
        assert response.content == b"fake"
        health = (await http.get("/v1/health")).json()
        assert health["workers"]
        assert health["queue_depth"] == 0
        assert health["available_memory_bytes"] == 64 * GIB
        assert health["version"] == "0.8.5"
        assert health["video_still"] is True


async def test_portrait(studio):
    async with client(studio) as http:
        assert (await http.get("/v1/portrait/unknown")).status_code == 404
        response = await http.get("/v1/portrait/default")
        assert response.headers["content-type"] == "image/png"
        assert response.content == b"portrait"


@pytest.mark.parametrize("path,payload", [
    ("voice", {"text": "x" * 601}),
    ("voice", {"text": "hello", "seed": -1}),
    ("voice", {"text": "hello", "seed": 2**64}),
    ("voice", {"text": 12}),
    ("image", {"scene": "x" * 301}),
    ("image", {"scene": "hello", "size": 512}),
    ("image", {"scene": "hello", "size": 768.0}),
    ("video_note", {"text": "hello", "special": "true"}),
    ("video_note", {"text": "hello", "extra": "field"}),
    ("video_note", {"text": "x" * 201}),
    ("video_note", {"text": "x" * 121, "special": True}),
    ("image", {"scene": "hello", "outfit": 12}),
])
async def test_validation(studio, path, payload):
    async with client(studio) as http:
        assert (await http.post(f"/v1/{path}", json=payload)).status_code == 422


@pytest.mark.parametrize("path,payload,mime", [
    ("image", {"scene": "hello"}, "image/png"),
    ("video_note", {"text": "hello"}, "video/mp4"),
])
@pytest.mark.parametrize("outfit", ["", "x" * 129])
async def test_unusable_outfits_use_default(studio, path, payload, mime, outfit):
    # Even a configured outfit with an overlong name is ignored.
    studio.config.outfits["x" * 129] = studio.config.outfits["default"].model_copy(
        update={"portrait": Path("wrong.png")},
    )
    async with client(studio) as http:
        response = await http.post(f"/v1/{path}", json={**payload, "outfit": outfit})
        assert response.status_code == 200
        assert response.headers["content-type"] == mime


async def test_body_limits(studio):
    async with client(studio) as http:
        assert (await http.post("/v1/voice", content="x")).status_code == 415
        response = await http.post("/v1/voice", content="x" * (17 * 1024),
                                   headers={"content-type": "application/json"})
        assert response.status_code == 413


async def test_admission_evicts_lru_and_keeps_voice(studio):
    await asyncio.gather(*(studio.workers[kind].start() for kind in ("voice", "video", "image")))
    studio.workers["video"].last_used = 1
    studio.workers["image"].last_used = 2

    async def sample(groups):
        return Reading((18 if len(groups) == 3 else 23) * GIB, 0)

    studio.sample = sample
    await studio.admit("voice", {"voice"})
    assert studio.workers["image"].loaded
    assert not studio.workers["video"].loaded
    studio.sample = Memory()
    await studio.admit("special", {"special", "voice"})
    assert studio.workers["voice"].loaded
    assert not studio.workers["image"].loaded
    studio.sample.available = 40 * GIB
    with pytest.raises(LowMemory):
        await studio.admit("special", {"special", "voice"})


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_watchdog_cancels_worker(studio):
    async with client(studio) as http:
        task = asyncio.create_task(http.post("/v1/voice", json={"text": "slow"}))
        await wait_until(lambda: studio.workers["voice"].loaded)
        studio.sample.available = 17 * GIB
        response = await task
        assert (response.status_code, response.json()) == (503, {"error": "low-memory"})
        assert not studio.workers["voice"].loaded
        assert not studio.lane.locked()


async def test_queue_deadline_and_health(studio, monkeypatch):
    monkeypatch.setitem(DEADLINES, "voice", 0.5)
    async with client(studio) as http:
        tasks = [asyncio.create_task(http.post("/v1/voice", json={"text": "slow"}))]
        await wait_until(lambda: studio.lane.locked())
        tasks.extend(asyncio.create_task(http.post("/v1/voice", json={"text": "slow"})) for _ in range(3))
        await wait_until(lambda: len(studio.pending) == 3)
        assert (await http.get("/v1/health")).json()["queue_depth"] == 3
        busy = await http.post("/v1/voice", json={"text": "slow"})
        assert (busy.status_code, busy.json()) == (503, {"error": "busy"})
        for response in await asyncio.gather(*tasks):
            assert response.status_code == 504
        assert not studio.pending
        assert not studio.workers["voice"].loaded


async def test_crash_backoff(studio):
    worker = studio.workers["voice"]
    with pytest.raises(WorkerError):
        await worker.run({"text": "crash"})
    assert not worker.loaded
    with pytest.raises(WorkerError, match="backoff"):
        await worker.start()


async def test_supervisor_death_reaps_inflight_process_group():
    # A worker and its child both hold the stdout pipe. EOF proves both exited.
    worker_code = """
import os
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(os.getpgrp(), flush=True)
time.sleep(60)
"""
    supervisor_code = """
import asyncio
import sys

from studio_service.process import spawn

async def main():
    worker = await spawn(sys.executable, "-c", sys.argv[1])
    await worker.wait()

asyncio.run(main())
"""
    supervisor = await asyncio.create_subprocess_exec(
        sys.executable, "-c", supervisor_code, worker_code, stdout=asyncio.subprocess.PIPE,
    )
    group = None
    try:
        async with asyncio.timeout(5):
            group = int(await supervisor.stdout.readline())
            supervisor.kill()
            assert await supervisor.stdout.read() == b""
            group = None
    finally:
        if supervisor.returncode is None:
            with suppress(ProcessLookupError):
                supervisor.kill()
        if group is not None:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        await supervisor.wait()


async def test_disconnect_reaps_render(studio):
    app = create_app(studio, Sources(studio.config), TOKEN)
    events = asyncio.Queue()
    events.put_nowait({"type": "http.request", "body": b'{"text":"slow"}', "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(app({
        "type": "http", "method": "POST", "path": "/v1/voice",
        "client": PEER, "headers": [(b"authorization", b"Bearer " + TOKEN),
                                     (b"content-type", b"application/json")],
    }, events.get, send))
    await wait_until(lambda: studio.workers["voice"].loaded)
    events.put_nowait({"type": "http.disconnect"})
    await task
    assert sent[0]["status"] == 499
    assert not studio.pending
    assert not studio.workers["voice"].loaded
    assert not studio.lane.locked()


STILL = b"\x89PNG\r\n\x1a\n" + b"still" * 100


@pytest.mark.parametrize("special", [False, True])
async def test_video_note_from_a_fresh_still(studio, special):
    payload = {"text": "hello", "special": special, "still": base64.b64encode(STILL).decode()}
    async with client(studio) as http:
        response = await http.post("/v1/video_note", json=payload)
        assert response.status_code == 200 and response.content == b"fake"


@pytest.mark.parametrize("still", [
    "not base64!", base64.b64encode(b"GIF89a").decode(), base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(3 * 1024**2)).decode(),
])
async def test_a_bad_still_is_refused(studio, still):
    async with client(studio) as http:
        assert (await http.post("/v1/video_note", json={"text": "hello", "still": still})).status_code == 422


async def test_only_a_video_note_takes_a_big_body(studio):
    big = {"content": "x" * (64 * 1024), "headers": {"content-type": "application/json"}}
    async with client(studio) as http:
        assert (await http.post("/v1/image", **big)).status_code == 413
        assert (await http.post("/v1/video_note", **big)).status_code == 422  # read in full, then not valid JSON
