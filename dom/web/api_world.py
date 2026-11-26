"""JSON API for the World page: Dom's life sheet, his known places and their reference photos.

The sheet itself is `memory/WORLD.md`, opened and saved through the Memory API like any note (the owner's edits are
pinned there). This module adds what that API doesn't have: the last change Dom made and why, the place notes with
their photos, adding a place, and keeping or removing a place's reference photo. Every POST sits in JSON_POSTS, so it
needs the dashboard's auth, a same-origin Origin and a JSON body, and a photo is read only for the owner's own browser
(`GUARDED_PREFIX`: same-site requests only).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from dom import notes, world_change
from dom.home import WORLD, content_version, limit_for
from dom.place_photos import MAX_UPLOAD, PhotoError, slugify
from dom.telegram import owner
from dom.web.model_settings import _input
from dom.web.shared import entity_labels, owner_app

log = logging.getLogger(__name__)
router = APIRouter()
PREFIX = "/api/world"
GUARDED_PREFIX = f"{PREFIX}/places/"
JSON_POSTS = {f"{PREFIX}/place", f"{PREFIX}/photo", f"{PREFIX}/photo/remove"}
MAX_PHOTO_BODY = MAX_UPLOAD * 4 // 3 + 4096  # the picture as base64 with JSON around it
NOTE_CHARS = 300


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewPlace(Strict):
    name: str = Field(min_length=1, max_length=60)
    note: str = Field(min_length=1, max_length=NOTE_CHARS)


class Photo(Strict):
    slug: str = Field(max_length=64)
    data: str  # the picture's bytes, base64


class PhotoRef(Strict):
    slug: str = Field(max_length=64)


def _today(app) -> date:
    return app.dispatch.now().astimezone(ZoneInfo(app.config.timezone)).date()


def _places(app) -> list[dict]:
    """Every place note with what it says (the owner reads all of it, with no tags) and whether it has a photo."""
    home = app.home.scoped(allow_private=False)
    found = []
    for rel in home.list_entity_notes():
        if not rel.startswith("memory/places/"):
            continue
        slug = Path(rel).stem
        text = notes.strip_tags(home.read_memory_file(rel))
        found.append({
            "slug": slug, "name": slug.replace("-", " "),
            "note": " ".join(line.strip().lstrip("-* ").strip() for line in text.split("\n") if line.strip()),
            "photo": app.place_photos.has(slug),
        })
    return found


async def _world(app) -> dict:
    home = app.home.scoped(allow_private=False)
    content = await asyncio.to_thread(home.read_memory_file, WORLD)
    changed = await world_change.last_change(app)
    return {
        "sheet": {
            "file": WORLD, "limit": limit_for(WORLD), "content": content, "version": content_version(content),
            "entities": await entity_labels(app, content),
        },
        "last_change": None if changed is None else {"day": changed[0].isoformat(), "reason": changed[1]},
        "interval_days": world_change.INTERVAL.days,
        "places": await asyncio.to_thread(_places, app),
        "max_photo_mb": MAX_UPLOAD // 1024 // 1024,
    }


@router.get(PREFIX)
async def read_world(request: Request) -> dict:
    return await _world(owner_app(request))


@router.get(PREFIX + "/places/{slug}/photo")
async def get_photo(request: Request, slug: str) -> Response:
    app = owner_app(request)
    data = await asyncio.to_thread(app.place_photos.read, slug)
    if data is None:
        raise HTTPException(404, "No photo for that place.")
    return Response(data, media_type="image/jpeg", headers={"Content-Disposition": "inline", "Cache-Control": "private, no-cache"})


@router.post(PREFIX + "/place")
async def add_place(request: Request) -> dict:
    """A new place note, written by the owner (so its line is pinned and Dom can't rewrite it)."""
    app = owner_app(request)
    update = await _input(request, NewPlace, 4096)
    slug = slugify(update.name)
    note = " ".join(update.note.replace("{", "(").replace("}", ")").split())
    if not slug or not note:
        raise HTTPException(422, "Give the place a name and one line about it.")
    rel = f"memory/places/{slug}.md"
    home = app.home.scoped(allow_private=False)
    if await asyncio.to_thread(home.read_memory_file, rel):
        raise HTTPException(409, "There is already a place with that name.")
    # Tagged `{any}` and linked to itself: the owner chose to write it down, so every chat that hears of the place may know it.
    text = f"- {note} {{any}} {{entities place:{slug}}}"
    result = await asyncio.to_thread(home.save_memory, rel, text, content_version(""), today=_today(app))
    if not result.ok:
        raise HTTPException(422 if result.over_limit else 409, result.error)
    await owner.record_event(app, f"Added the place {slug} in the dashboard")
    return await _world(app)


async def _existing(app, slug: str) -> str:
    if f"memory/places/{slug}.md" not in app.home.scoped(allow_private=False).list_entity_notes():
        raise HTTPException(404, "Unknown place.")
    return slug


@router.post(PREFIX + "/photo")
async def set_photo(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Photo, MAX_PHOTO_BODY)
    slug = await _existing(app, update.slug)
    try:
        raw = base64.b64decode(update.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Couldn't read that file. Choose it again.") from None
    try:
        await app.place_photos.save(slug, raw)
    except PhotoError as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        log.exception("could not keep a place photo")
        raise HTTPException(500, "Couldn't save the photo. Check that Dom can write to his home.") from None
    await owner.record_event(app, f"Set the reference photo of {slug} in the dashboard")
    return await _world(app)


@router.post(PREFIX + "/photo/remove")
async def remove_photo(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, PhotoRef, 1024)
    slug = await _existing(app, update.slug)
    if not await app.place_photos.remove(slug):
        raise HTTPException(404, "That place has no photo.")
    await owner.record_event(app, f"Removed the reference photo of {slug} in the dashboard")
    return await _world(app)
