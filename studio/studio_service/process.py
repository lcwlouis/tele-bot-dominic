"""Serialized JSON-line subprocesses with process-group cleanup."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time


class WorkerError(Exception):
    pass


def offline_env():
    return {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"}


async def spawn(*command, **options):
    # Reap even if cancelled between OS process creation and receiving its handle.
    task = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("_process_guard.py")),
        str(os.getpid()), *command,
        start_new_session=True, stderr=asyncio.subprocess.DEVNULL,
        env=offline_env(), **options
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await terminate(await task)
        raise


async def terminate(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.stdout:
        while await process.stdout.read(65536):
            pass
    await process.wait()


class Worker:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.last_used = 0.0
        self.retry_at = 0.0
        self.failures = 0

    @property
    def loaded(self):
        return self.process is not None and self.process.returncode is None

    async def stop(self):
        if self.process is not None:
            await terminate(self.process)
            self.process = None

    async def expect(self, event):
        try:
            row = json.loads(await self.process.stdout.readline())
            if isinstance(row, dict) and row.get("event") == event:
                return
        except (ValueError, UnicodeError):
            pass
        raise WorkerError("unexpected worker response")

    async def start(self):
        if self.loaded:
            return
        if time.monotonic() < self.retry_at:
            raise WorkerError("worker in restart backoff")
        await self.stop()
        try:
            self.process = await spawn(
                *self.config.command, cwd=self.config.cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            )
            await self.expect("ready")
        except asyncio.CancelledError:
            await self.stop()
            raise
        except (OSError, WorkerError):
            await self.failed()
            raise WorkerError("worker startup failed") from None

    async def failed(self):
        await self.stop()
        self.failures = min(self.failures + 1, 6)
        self.retry_at = time.monotonic() + min(60, 2**self.failures)

    async def run(self, payload):
        await self.start()
        try:
            self.process.stdin.write(json.dumps(payload).encode() + b"\n")
            await self.process.stdin.drain()
            await self.expect("done")
        except asyncio.CancelledError:
            await self.stop()
            raise
        except (OSError, WorkerError):
            await self.failed()
            raise WorkerError("worker generation failed") from None
        self.failures = 0
        self.last_used = time.monotonic()


async def encode(command, groups):
    process = await spawn(*command, stdout=asyncio.subprocess.DEVNULL)
    groups.add(process.pid)
    try:
        if await process.wait():
            raise WorkerError("media encoder failed")
    finally:
        await terminate(process)
        groups.discard(process.pid)
