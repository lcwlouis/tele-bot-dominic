"""Reference photos of Dom's known places, so a picture of him there looks like the place.

A place is a note under `memory/places/<slug>.md`. Its photo is optional and the owner's call: the owner adds one on the
dashboard. (A photo Dom finds by search and the owner approves is not built yet.) The file is kept like the media Dom sends: `media/places/<slug>.jpg` under the
home, in private folders, written to a temporary name and moved into place, so a cancelled save leaves nothing
half-written. The upload is decoded and saved again as a JPEG of at most 1024 px, which drops any location data the
camera wrote into it.

A photo is offered to the image model only when the model accepts a reference, Dom named the place by its key on the
image or video note (never guessed from the scene's words), and the chat may know it: the owner's DM, or a chat the
place note has a line for (the same tag rules as every note).
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from dom import notes
from dom.home import Home
from dom.media_files import MEDIA_DIR, MediaFiles
from dom.models import Scope

log = logging.getLogger("dom.place_photos")
FOLDER = f"{MEDIA_DIR}/places"
MAX_UPLOAD = 8 * 1024 * 1024  # bytes of the picture the owner adds
MAX_OFFERED = 12  # place keys listed in one turn note
SIDE = 1024  # longest side kept; a reference needs no more
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_FORMATS = {"JPEG", "PNG", "WEBP"}


class PhotoError(ValueError):
    """A picture that can't be kept, with a sentence the owner can read."""


def slugify(name: str) -> str:
    """A place's note name from what the owner calls it: lowercase letters, digits and dashes, or "" when nothing is left."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].strip("-")


def tidy(data: bytes) -> bytes:
    """The upload as the JPEG that is kept. Raises PhotoError for anything that is not a picture we can read."""
    if len(data) > MAX_UPLOAD:
        raise PhotoError(f"That picture is over {MAX_UPLOAD // 1024 // 1024} MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in _FORMATS:
                raise PhotoError("Use a JPEG, PNG or WebP picture.")
            picture = ImageOps.exif_transpose(image)
            if picture.mode in ("RGBA", "LA", "P"):
                picture = picture.convert("RGBA")
                flat = Image.new("RGB", picture.size, "white")
                flat.paste(picture, mask=picture.getchannel("A"))
                picture = flat
            else:
                picture = picture.convert("RGB")
            picture.thumbnail((SIDE, SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            picture.save(out, "JPEG", quality=85)
    except PhotoError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise PhotoError("That file is not a picture Dom can read.") from None
    return out.getvalue()


class PlacePhotos:
    def __init__(self, home: Home, media: MediaFiles):
        self._home, self._media = home, media

    def _rel(self, slug: str) -> str:
        if not SLUG.fullmatch(slug):
            raise PhotoError("That is not a place name.")
        return f"{FOLDER}/{slug}.jpg"

    def _path(self, slug: str) -> Path:
        return self._media.home / self._rel(slug)

    def read(self, slug: str) -> bytes | None:
        """The kept photo's bytes, or None. Blocking: run it in a thread."""
        try:
            path = self._path(slug)
            if path.is_symlink() or not path.is_file():
                return None
            return path.read_bytes()
        except (OSError, PhotoError):
            return None

    def has(self, slug: str) -> bool:
        try:
            path = self._path(slug)
            return path.is_file() and not path.is_symlink()
        except (OSError, PhotoError):
            return False

    async def save(self, slug: str, data: bytes) -> None:
        """Keep `data` as `slug`'s reference photo, replacing the old one. Raises PhotoError for a bad picture."""
        rel = self._rel(slug)
        kept = await asyncio.to_thread(tidy, data)
        await asyncio.to_thread(self._media.save, rel, kept)

    async def remove(self, slug: str) -> bool:
        path = self._path(slug)
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            return False
        return True

    def places_for(self, scope: Scope, chat_id: int) -> list[str]:
        """The keys of the places with an owner-added photo that this chat may see: all of them in the owner's DM, else
        those whose note has a line this chat may read. Filtered here in code, so the choice offered is already safe."""
        try:
            keys = sorted(p.stem for p in (self._media.home / FOLDER).glob("*.jpg"))
        except OSError:
            return []
        return [key for key in keys if SLUG.fullmatch(key) and self.has(key) and (scope == "owner_dm" or self._known_to(key, chat_id))]

    def offer(self, scope: Scope, chat_id: int) -> str:
        """The turn-note line that lists the places this chat may show, or "" when there are none. The place of a picture
        is only ever one of these keys, named by Dom in the action, never guessed from the scene's words."""
        keys = self.places_for(scope, chat_id)[:MAX_OFFERED]
        if not keys:
            return ""
        return (
            'places with a photo you may show (only when the scene really is there, set "image_place" next to "image", '
            'or "place" inside "video_note", to exactly one of these keys; otherwise leave it out): ' + ", ".join(keys)
        )

    def _known_to(self, slug: str, chat_id: int) -> bool:
        """Whether the place's note has a line this chat may read, so a photo never names a place the chat never heard of."""
        try:
            text = self._home.scoped(allow_private=False).read(f"memory/places/{slug}.md")
        except (OSError, ValueError):
            return False
        rel = f"memory/places/{slug}.md"
        return any(notes.is_bullet(line) for line in notes.file_in_chat(rel, text, chat_id).split("\n"))

    async def reference_for(self, place: str | None, scope: Scope, chat_id: int) -> bytes | None:
        """The photo of the place Dom named, if it is one this chat may see. A name that is not on that list gets none."""
        if not place or not SLUG.fullmatch(place):
            return None
        allowed = await asyncio.to_thread(self.places_for, scope, chat_id)
        return await asyncio.to_thread(self.read, place) if place in allowed else None
