"""Single render lane, memory admission and watchdog."""

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from .config import GIB
from .process import Worker, WorkerError, encode

DEADLINES = {"voice": 60, "image": 120, "video": 180, "special": 420, "stt": 60}
INCREASE = {"voice": 3, "image": 2, "video": 3, "special": 25, "stt": 2}
COLD_INCREASE = {"voice": 12, "image": 7, "video": 9, "stt": 3}
MAX_OUTPUT = 20 * 1024**2
MAX_TRANSCRIPT = 256 * 1024


class LowMemory(Exception):
    pass


@asynccontextmanager
async def workspace():
    creation = asyncio.create_task(asyncio.to_thread(tempfile.TemporaryDirectory, prefix="dom-studio-"))
    try:
        directory = await asyncio.shield(creation)
    except asyncio.CancelledError:
        directory = await creation
        await asyncio.to_thread(directory.cleanup)
        raise
    try:
        yield Path(directory.name)
    finally:
        await asyncio.to_thread(directory.cleanup)


class Studio:
    def __init__(self, config, sample):
        self.config = config
        self.sample = sample
        self.workers = {
            kind: Worker(worker_cfg)
            for kind in DEADLINES
            if (worker_cfg := getattr(config.workers, kind)) is not None
        }
        self.encoders = set()
        self.reading = None
        self.pending = set()
        self.lane = asyncio.Lock()

    async def read_memory(self):
        groups = self.encoders | {
            worker.process.pid for worker in self.workers.values() if worker.loaded
        }
        try:
            self.reading = await self.sample(groups)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.reading = None
        return self.reading

    async def admit(self, kind, required):
        def fits(reading):
            cold = sum(COLD_INCREASE[name] for name in required
                       if not self.workers[name].loaded and name != kind)
            increment = max(INCREASE[kind], COLD_INCREASE.get(kind, 0)
                            if not self.workers[kind].loaded else 0)
            return reading and reading.available >= self.config.reserve + (cold + increment) * GIB

        evictable = [self.workers[name] for name in ("image", "video", "stt")
                     if name in self.workers and (kind == "special" or name not in required) and self.workers[name].loaded]
        evictable.sort(key=lambda worker: worker.last_used)
        if kind == "special":
            for worker in evictable:
                await worker.stop()
            evictable.clear()
        reading = await self.read_memory()
        while not fits(reading) and evictable:
            await evictable.pop(0).stop()
            reading = await self.read_memory()
        if not fits(reading):
            raise LowMemory

    async def watch(self):
        while True:
            await asyncio.sleep(0.25)
            reading = await self.read_memory()
            if not reading or reading.available < self.config.reserve + 3 * GIB // 2:
                raise LowMemory

    async def guarded(self, work):
        render = asyncio.create_task(work)
        watchdog = asyncio.create_task(self.watch())
        try:
            done, _ = await asyncio.wait((render, watchdog), return_when=asyncio.FIRST_COMPLETED)
            if watchdog in done:
                watchdog.result()
            return render.result()
        finally:
            for task in (render, watchdog):
                task.cancel()
            await asyncio.gather(render, watchdog, return_exceptions=True)

    async def warm(self):
        for kind in ("voice", "image", "video"):
            async with self.lane:
                try:
                    async with asyncio.timeout(DEADLINES[kind]):
                        await self.admit(kind, {kind})
                        await self.guarded(self.workers[kind].start())
                except (LowMemory, WorkerError, TimeoutError):
                    continue

    async def render(self, kind, payload, path):
        required = {kind}
        if kind in ("video", "special"):
            required.add("voice")
        await self.admit(kind, required)
        return await self.guarded(self.generate(kind, payload, path))

    async def generate(self, kind, payload, path):
        outfit = self.config.outfits.get(payload.get("outfit") or self.config.default_outfit,
                                         self.config.outfits[self.config.default_outfit])
        seed = payload.get("seed")
        if seed is None:
            seed = secrets.randbits(32)
        worker = self.workers[kind]
        if kind == "stt":
            raw_audio = path / "input.bin"
            await asyncio.to_thread(raw_audio.write_bytes, payload["audio"])
            wav = path / "audio.wav"
            ffmpeg = [str(self.config.ffmpeg), "-nostdin", "-v", "error", "-n"]
            await encode([*ffmpeg, "-i", str(raw_audio), "-t", "600", "-ar", "16000", "-ac", "1",
                          "-c:a", "pcm_s16le", str(wav)], self.encoders)
            output = path / "transcript.txt"
            await worker.run({"audio": str(wav), "output": str(output), "language": payload.get("language") or ""})
            return await asyncio.to_thread(read_transcript, output)
        if kind == "image":
            output = path / "image.png"
            await worker.run({
                "prompt": payload["scene"] + ". Keep the same outfit as in the reference. "
                "Keep the subject fully covered by that outfit.",
                "portrait": str(outfit.portrait), "turnaround": str(outfit.turnaround),
                "size": payload["size"], "seed": seed, "output": str(output),
            })
        else:
            audio = path / "voice.wav"
            await self.workers["voice"].run({"text": payload["text"], "seed": seed, "output": str(audio)})
            ffmpeg = [str(self.config.ffmpeg), "-nostdin", "-v", "error", "-n"]
            if kind == "voice":
                output = path / "voice.ogg"
                await encode([*ffmpeg, "-i", str(audio), "-c:a", "libopus", "-ar", "48000",
                              "-ac", "1", str(output)], self.encoders)
            else:
                # Give workers at most 60 seconds of input audio as well as capping the final mux.
                clipped = path / "clipped.wav"
                await encode([*ffmpeg, "-i", str(audio), "-t", "60", str(clipped)], self.encoders)
                raw_video = path / "render.mp4"
                portrait = outfit.portrait
                if payload.get("still"):  # a fresh still for this note: animate it in place of the outfit's portrait
                    portrait = path / "still.png"
                    await asyncio.to_thread(portrait.write_bytes, payload["still"])
                await worker.run({"portrait": str(portrait), "audio": str(clipped),
                                  "seed": seed, "output": str(raw_video)})
                output = path / "note.mp4"
                await encode([
                    *ffmpeg, "-i", str(raw_video), "-i", str(clipped), "-map", "0:v:0",
                    "-map", "1:a:0", "-vf", "scale=512:512,setsar=1", "-t", "60",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1",
                    "-shortest", "-movflags", "+faststart", str(output),
                ], self.encoders)
        return await asyncio.to_thread(read_output, output)

    async def job(self, kind, payload):
        start = time.monotonic()
        outcome = "cancelled"
        try:
            async with asyncio.timeout(DEADLINES[kind]):
                try:
                    await self.lane.acquire()
                finally:
                    self.pending.discard(asyncio.current_task())
                try:
                    # A private directory per render. Cleanup happens after the worker group is reaped.
                    async with workspace() as directory:
                        result = await self.render(kind, payload, directory)
                    outcome = "ok"
                    return 200, result
                finally:
                    self.lane.release()
        except LowMemory:
            outcome = "low-memory"
            return 503, outcome
        except TimeoutError:
            outcome = "timeout"
            return 504, outcome
        except (WorkerError, OSError):
            outcome = "worker-failed"
            return 503, outcome
        finally:
            logging.getLogger("studio").info(
                "job=%s seconds=%.2f available_bytes=%s footprint_bytes=%s outcome=%s",
                kind, time.monotonic() - start, self.reading.available if self.reading else None,
                self.reading.footprint if self.reading else None, outcome,
            )

    async def close(self):
        await asyncio.gather(*(worker.stop() for worker in self.workers.values()))


def read_output(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_OUTPUT + 1)
    if not 0 < len(data) <= MAX_OUTPUT:
        raise WorkerError("invalid output length")
    return data


def read_transcript(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_TRANSCRIPT + 1)
    if len(data) > MAX_TRANSCRIPT:
        raise WorkerError("transcript too long")
    return data.decode("utf-8", errors="replace").strip()
