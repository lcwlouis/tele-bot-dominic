"""Frame strips: how Dom watches a clip his chat model can't take as video.

A video, animation or video note becomes a few strips, each one image: a grid of evenly spaced stills with the time
burnt onto every one, made with ffmpeg and Pillow. The sound is cut out as a small audio file for a transcript. A short
clip makes one strip (a frame every 2 to 5 seconds, 16 at most); a long one is split into equal parts of one strip each,
up to MAX_STRIPS and MAX_SECONDS, and the rest is cut off (the caller says so). Everything here is blocking-free: the
ffmpeg processes run as subprocesses with timeouts, the grid is drawn in a thread.

ffmpeg is the system binary found on PATH, as the studio's own ffmpeg calls find it (`apt install ffmpeg` on the Pi,
`brew install ffmpeg` on a Mac, and in a container image the same package). Nothing else is needed: the duration and
the streams come from `ffmpeg -i`, so ffprobe is not required, and Pillow is already a dependency. Without ffmpeg
there are no strips and the media model describes the whole clip, as before.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("dom.video")

FRAMES_PER_STRIP = 16
MIN_STEP, MAX_STEP = 2.0, 5.0  # seconds between sampled frames
MAX_STRIPS = 4
STRIP_SECONDS = FRAMES_PER_STRIP * MAX_STEP  # what one strip covers at its widest spacing
MAX_SECONDS = MAX_STRIPS * STRIP_SECONDS  # the longest stretch of a clip that is looked at
DOWNLOAD_BYTES = 20_000_000  # what Telegram lets a bot download
COLUMNS = 4
CELL_WIDTH = 320  # pixels; a full strip is 1280 wide
FRAME_SECONDS = 30  # for one ffmpeg call
WATCH_SECONDS = 240  # for a whole clip
_PARALLEL = 3  # frames cut at once

Route = Literal["native", "strips", "clip"]
Listener = Literal["media", "chat"]


class Unwatchable(Exception):
    """The clip could not be turned into strips (no ffmpeg, unreadable, no frames)."""


@dataclass(frozen=True)
class Span:
    """The part of a clip one strip covers, and the moments its frames are taken at (seconds)."""

    start: float
    end: float
    times: tuple[float, ...]


@dataclass(frozen=True)
class Strip:
    start: float
    end: float
    image: bytes  # JPEG


@dataclass(frozen=True)
class Watched:
    strips: list[Strip]
    seconds: float  # how much of the clip the strips cover
    total: float  # the clip's length
    audio: bytes | None  # m4a of the covered stretch, None when the clip has no sound

    @property
    def cut(self) -> bool:
        return self.total > self.seconds + 0.5


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def route(*, takes_video: bool, takes_images: bool, has_ffmpeg: bool) -> Route:
    """How a clip reaches Dom: as the clip itself when his chat model takes video, as frame strips when it takes images
    and ffmpeg is there, else the media model describes the whole clip."""
    if takes_video:
        return "native"
    return "strips" if takes_images and has_ffmpeg else "clip"


def listener(media_hears: bool, chat_hears: bool) -> Listener | None:
    """Which model writes the transcript of a clip's sound: the media route when it takes audio (it is the cheaper
    one), else the chat model when it does, else nobody."""
    return "media" if media_hears else "chat" if chat_hears else None


def plan(duration: float) -> tuple[list[Span], bool]:
    """The strips for a clip of `duration` seconds, and whether the clip is longer than what is looked at. Each strip
    covers an equal part and takes frames at the middle of equal slices of it, so the frames are evenly spaced."""
    duration = max(duration, 0.0)
    covered = min(duration, MAX_SECONDS)
    count = max(1, ceil(covered / STRIP_SECONDS - 1e-9))
    length = covered / count
    step = min(MAX_STEP, max(MIN_STEP, length / FRAMES_PER_STRIP))
    frames = max(1, min(FRAMES_PER_STRIP, round(length / step)))
    spans = []
    for index in range(count):
        start = index * length
        spans.append(Span(start, start + length, tuple(start + (slot + 0.5) * length / frames for slot in range(frames))))
    return spans, duration > MAX_SECONDS


def clock(seconds: float) -> str:
    """Seconds as m:ss (h:mm:ss from an hour)."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_AUDIO = re.compile(r"Stream #\S+.*: Audio:")


def parse_probe(stderr: str) -> tuple[float | None, bool]:
    """The duration (None when ffmpeg doesn't know it) and whether there is an audio stream, from `ffmpeg -i`'s report."""
    found = _DURATION.search(stderr)
    duration = int(found[1]) * 3600 + int(found[2]) * 60 + float(found[3]) if found else None
    return duration, bool(_AUDIO.search(stderr))


async def _run(binary: str, *args: str, timeout: float = FRAME_SECONDS) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        binary, "-hide_banner", "-nostdin", *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:  # a timeout or a cancelled task: never leave ffmpeg running
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, error.decode("utf-8", "replace")


async def _frame(binary: str, source: Path, at: float, target: Path, gate: asyncio.Semaphore) -> bytes | None:
    async with gate:
        try:
            await _run(
                binary, "-v", "error", "-ss", f"{at:.2f}", "-i", str(source), "-frames:v", "1",
                "-vf", f"scale={CELL_WIDTH}:-2", "-q:v", "5", "-y", str(target),
            )
            return await asyncio.to_thread(target.read_bytes)
        except (TimeoutError, OSError):
            return None  # a frame past the end of a clip that reported a longer length, or one that stalled


def _font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=16)
    except (TypeError, OSError):  # a Pillow without FreeType has only the small bitmap font
        return ImageFont.load_default()


def _grid(frames: list[tuple[float, bytes]]) -> bytes:
    """The frames as one JPEG grid, the time of each burnt into its corner."""
    cells = [(at, Image.open(io.BytesIO(data)).convert("RGB")) for at, data in frames]
    width, height = cells[0][1].size
    rows = ceil(len(cells) / COLUMNS)
    sheet = Image.new("RGB", (width * min(COLUMNS, len(cells)), height * rows), "black")
    draw, font = ImageDraw.Draw(sheet), _font()
    for index, (at, cell) in enumerate(cells):
        left, top = (index % COLUMNS) * width, (index // COLUMNS) * height
        sheet.paste(cell.resize((width, height)), (left, top))
        text = clock(at)
        box = draw.textbbox((left + 4, top + 4), text, font=font)
        draw.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill="black")
        draw.text((left + 4, top + 4), text, fill="white", font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=82)
    return out.getvalue()


async def _audio(binary: str, source: Path, target: Path, seconds: float) -> bytes | None:
    code, _ = await _run(
        binary, "-v", "error", "-i", str(source), "-vn", "-t", f"{seconds:.1f}", "-ac", "1", "-ar", "16000",
        "-c:a", "aac", "-b:a", "32k", "-y", str(target), timeout=WATCH_SECONDS,
    )
    if code != 0:
        return None
    try:
        data = await asyncio.to_thread(target.read_bytes)
    except OSError:
        return None
    return data or None


async def watch(clip: bytes, known_duration: float | None = None) -> Watched:
    """Strips and the sound of a clip. `known_duration` (Telegram's own figure) is used when ffmpeg can't tell.
    Raises Unwatchable when there is no ffmpeg, the clip can't be read or yields no frame; never blocks the loop."""
    binary = ffmpeg()
    if binary is None:
        raise Unwatchable("ffmpeg is not installed")
    try:
        return await asyncio.wait_for(_watch(binary, clip, known_duration), WATCH_SECONDS)
    except TimeoutError:
        raise Unwatchable("watching the clip took too long") from None


async def _watch(binary: str, clip: bytes, known_duration: float | None) -> Watched:
    with tempfile.TemporaryDirectory(prefix="dom-video-") as folder:
        source = Path(folder, "clip")
        await asyncio.to_thread(source.write_bytes, clip)
        _, report = await _run(binary, "-i", str(source))  # no output file: ffmpeg reports the streams and exits 1
        duration, has_sound = parse_probe(report)
        if duration is None:
            duration = known_duration if known_duration is not None else STRIP_SECONDS / 4
        spans, _ = plan(duration)
        gate = asyncio.Semaphore(_PARALLEL)
        strips: list[Strip] = []
        for number, span in enumerate(spans):
            frames = await asyncio.gather(*(
                _frame(binary, source, at, Path(folder, f"f{number}-{slot}.jpg"), gate)
                for slot, at in enumerate(span.times)
            ))
            got = [(at, data) for at, data in zip(span.times, frames) if data]
            if got:
                strips.append(Strip(span.start, span.end, await asyncio.to_thread(_grid, got)))
        if not strips:
            raise Unwatchable("no frames could be read")
        covered = min(duration, MAX_SECONDS)
        sound = await _audio(binary, source, Path(folder, "sound.m4a"), covered) if has_sound else None
        return Watched(strips, covered, duration, sound)
