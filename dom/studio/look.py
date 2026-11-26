"""Dom looks at a generated photo (or a video note's first frame) before sending it.

One short call on the chat route, which must take images: does the picture match the scene he asked for, and if
not, what to change. The caller regenerates once with the adjusted scene, then asks which of the two is better and
sends that one. It never loops, runs in the media delivery task (not the text reply path), uses the media request
context so it is counted and capped like any media call, and any failure just means the original is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from dom.modalities import Attachment

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger("dom.studio.look")
LOOK_SECONDS = 45
FRAME_SECONDS = 20
CHECK_PROMPT = (
    "You are checking a picture before it is sent. The scene that was asked for:\n{scene}\n\n"
    "Does the picture show that scene? Ignore small details. Answer with ONLY a JSON object: "
    '{{"match": true or false, "fix": "if it does not match, the scene rewritten in one or two sentences so '
    'a new picture would get it right; otherwise an empty string"}}'
)
CLOUD_VIDEO_PROMPT = (
    "You are checking the first frame of a video note before it is sent. The scene that was asked for:\n{scene}\n\n"
    "A redo costs real money, so say it is clearly wrong ONLY for a serious fault: the wrong person, a broken or "
    "garbled frame, or a wildly wrong setting. Small differences in detail, framing, light or expression do not "
    "count. Answer with ONLY a JSON object: "
    '{{"clearly_wrong": true or false}}'
)
COMPARE_PROMPT = (
    "You are choosing between two pictures. The scene that was asked for:\n{scene}\n\n"
    "Picture A comes first and picture B second. Which one shows the scene better? "
    'Answer with ONLY a JSON object: {{"better": "A" or "B"}}'
)


def looks_enabled(app: App, scope: str) -> bool:
    return bool(getattr(app.config.studio, f"look_{scope}", False))


def _json(text: object) -> dict:
    found = re.search(r"\{.*\}", text, re.DOTALL) if isinstance(text, str) else None
    try:
        value = json.loads(found.group(0)) if found else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def first_frame(video: bytes) -> bytes | None:
    """The first frame of a video as a JPEG, or None when ffmpeg isn't installed or can't read it."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    with tempfile.TemporaryDirectory(prefix="dom-look-") as folder:
        source, frame = Path(folder, "in.mp4"), Path(folder, "frame.jpg")
        await asyncio.to_thread(source.write_bytes, video)
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-v", "error", "-i", str(source), "-frames:v", "1", "-y", str(frame),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), FRAME_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None
        try:
            return await asyncio.to_thread(frame.read_bytes)
        except OSError:
            return None


def _mime(image: bytes) -> str:
    return "image/jpeg" if image.startswith(b"\xff\xd8\xff") else "image/png"


async def _ask(app: App, chat_id: int, prompt: str, images: list[bytes]) -> dict:
    message = {"role": "user", "content": prompt, "attachments": [
        Attachment("image", _mime(image), image, f"picture {'AB'[index]}") for index, image in enumerate(images)
    ]}
    async with asyncio.timeout(LOOK_SECONDS):
        async with app.dispatch._media_request(chat_id, background=True):
            reply = await app.client.complete([message], [], once=True)
    return _json(reply.get("content"))


async def can_look(app: App, scope: str) -> bool:
    """The setting is on for this scope and the chat model takes images (so nothing is described instead)."""
    return looks_enabled(app, scope) and "image" in await app.dispatch.native.accepted()


async def fix_for(app: App, chat_id: int, scene: str, image: bytes) -> str | None:
    """The adjusted scene when the picture does not match it; None when it matches or the check could not be made."""
    try:
        verdict = await _ask(app, chat_id, CHECK_PROMPT.format(scene=scene), [image])
    except Exception as exc:
        log.info("could not look at a picture before sending it in chat %s (%s)", chat_id, type(exc).__name__)
        return None
    fix = verdict.get("fix")
    return fix.strip()[:600] if verdict.get("match") is False and isinstance(fix, str) and fix.strip() else None


async def clearly_wrong(app: App, chat_id: int, scene: str, frame: bytes) -> bool:
    """Whether a cloud video note's first frame is clearly wrong for its scene. A redo costs money, so a minor
    mismatch, an unclear answer or a failed check all mean no."""
    try:
        verdict = await _ask(app, chat_id, CLOUD_VIDEO_PROMPT.format(scene=scene), [frame])
    except Exception as exc:
        log.info("could not look at a video note's first frame in chat %s (%s)", chat_id, type(exc).__name__)
        return False
    return verdict.get("clearly_wrong") is True


async def second_is_better(app: App, chat_id: int, scene: str, first: bytes, second: bytes) -> bool:
    """Whether the regenerated picture shows the scene better. Unknown counts for the new one, which was made to fix
    the old one's fault."""
    try:
        verdict = await _ask(app, chat_id, COMPARE_PROMPT.format(scene=scene), [first, second])
    except Exception as exc:
        log.info("could not compare two pictures in chat %s (%s)", chat_id, type(exc).__name__)
        return True
    return verdict.get("better") != "A"
