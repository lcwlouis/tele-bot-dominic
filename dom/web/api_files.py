"""JSON API for the Files page: browse the shared workspace and what Dom may read in his home, preview text and
images, upload into the workspace, download, and delete from it.

Every path goes through dom/workspace.py: no `..`, no links, nothing outside the workspace or the home's four readable
folders (memory, media, briefs, skills, which are read-only here). `private/`, `.env` and the database are never
reachable. The reads and the file bytes sit behind the dashboard's auth and the same-origin check
(GUARDED_PREFIX in dom/web/app.py); every POST sits in JSON_POSTS, so it also needs a same-origin Origin and a JSON
body. A file is served with a fixed type: a picture inline only when it is a PNG, JPEG, GIF or WebP, anything else as
a download, so a file in the workspace can never run as a page.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from dom import workspace
from dom.web.model_settings import _input
from dom.web.shared import owner_app
from dom.workspace import IMAGE_TYPES, MAX_FILE_BYTES, Tree, WorkspaceError

log = logging.getLogger(__name__)
router = APIRouter()
PREFIX = "/api/files"
GUARDED_PREFIX = PREFIX + "/"
JSON_POSTS = {f"{PREFIX}/upload", f"{PREFIX}/delete"}
MAX_UPLOAD = MAX_FILE_BYTES * 4 // 3 + 4096  # the file as base64 with JSON around it
Area = Literal["workspace", "home"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Upload(Strict):
    folder: str = Field(default="", max_length=500)  # inside the workspace
    name: str = Field(max_length=255)
    data: str  # the file's bytes, base64


class Remove(Strict):
    path: str = Field(max_length=1000)


def _tree(request: Request, area: str) -> Tree:
    app = owner_app(request)
    if area == "home":
        return workspace.home_view(app.home.root)
    try:
        return workspace.workspace(app.home.root)
    except WorkspaceError as error:
        raise HTTPException(409, str(error)) from None


def _refused(error: WorkspaceError) -> HTTPException:
    return HTTPException(404 if str(error).startswith("Not found") else 422, str(error))


@router.get(PREFIX + "/list")
async def list_files(request: Request, area: Area = "workspace", path: str = Query("", max_length=1000)) -> dict:
    tree = _tree(request, area)
    try:
        listing = await asyncio.to_thread(tree.list, path)
    except WorkspaceError as error:
        raise _refused(error) from None
    shared = area == "workspace"
    return {
        "area": area, "writable": tree.writable, **listing,
        # What the page tells the owner beside the files: the desktop runs without the workspace, or it is over its cap.
        "mount_problem": workspace.mount_problem(request.app.state.dom.home.root) if shared else None,
        "full": shared and await asyncio.to_thread(workspace.measure, request.app.state.dom.home.root, fresh=True) > workspace.MAX_TOTAL_BYTES,
    }


@router.get(PREFIX + "/preview")
async def preview_file(
    request: Request, area: Area = "workspace", path: str = Query(..., max_length=1000), offset: int = Query(0, ge=0),
) -> dict:
    """What a file shows as: its text (text, Markdown, CSV, JSON, PDF), an image (the page asks /raw), or neither."""
    tree = _tree(request, area)
    name, suffix = PurePosixPath(path).name, PurePosixPath(path).suffix.lower()
    try:
        if suffix in IMAGE_TYPES or (suffix != workspace.PDF_SUFFIX and suffix not in workspace.TEXT_SUFFIXES):
            info = await asyncio.to_thread(tree.stat, path)  # also proves the path is a regular file in the folder
            return {"kind": "image" if suffix in IMAGE_TYPES else "none", "name": name, **info}
        return {"kind": "text", **await workspace.read_document(tree, path, offset)}
    except WorkspaceError as error:
        raise _refused(error) from None


@router.get(PREFIX + "/raw")
async def raw_file(
    request: Request, area: Area = "workspace", path: str = Query(..., max_length=1000), download: bool = False,
) -> Response:
    tree = _tree(request, area)
    try:
        name, data = await asyncio.to_thread(tree.read_bytes, path)
    except WorkspaceError as error:
        raise _refused(error) from None
    image = IMAGE_TYPES.get(PurePosixPath(name).suffix.lower())
    if image is not None and not download:
        return Response(data, media_type=image, headers={"Content-Disposition": "inline"})
    fallback = workspace.safe_name(name)
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
    return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": disposition})


@router.post(PREFIX + "/upload")
async def upload_file(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Upload, MAX_UPLOAD)
    try:
        raw = base64.b64decode(update.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Couldn't read that file. Choose it again.") from None
    tree = workspace.workspace(app.home.root)
    try:
        await asyncio.to_thread(workspace.ensure_workspace, app.home.root)
        saved = await asyncio.to_thread(tree.save_new, update.folder, update.name, raw)
    except WorkspaceError as error:
        raise _refused(error) from None
    return {"ok": True, "message": f"Uploaded {PurePosixPath(saved).name}.", "path": saved}


@router.post(PREFIX + "/delete")
async def delete_file(request: Request) -> dict:
    app = owner_app(request)
    update = await _input(request, Remove)
    try:
        kind = await asyncio.to_thread(workspace.workspace(app.home.root).delete, update.path)
    except WorkspaceError as error:
        raise _refused(error) from None
    return {"ok": True, "message": f"Deleted the {kind} {PurePosixPath(update.path).name}."}
