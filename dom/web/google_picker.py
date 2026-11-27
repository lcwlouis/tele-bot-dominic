"""The page that hands Dom a Google Drive file (v0.8.6 W4, D21).

Under `drive.file` a shared link gives an app nothing: Google grants Dom a file only when the owner opens it with Dom's
OAuth client through Google Picker. This page does that. It is a normal authenticated dashboard page (Cloudflare Access
or the dashboard token, the same as every other page), and the only one whose CSP names Google's origins.

The browser gets its own `drive.file`-only token from Google Identity Services, so no server token ever reaches the page;
all it carries is the OAuth client id (public by design), the browser API key and the Cloud project number the Picker
needs. After picking, the page tells the server, which asks Google (with its own connection) whether Dom can now see the file.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dom.google import PICKER_PATH
from dom.google_files import FILE_ID
from dom.web.shared import owner_app

router = APIRouter()
templates = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")), autoescape=select_autoescape(("html",)),
    trim_blocks=True, lstrip_blocks=True,
)
# Only this page may load Google's scripts and frames; everything else on the dashboard keeps the strict default.
PICKER_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; "
        "script-src 'self' https://apis.google.com https://accounts.google.com/gsi/client; "
        "frame-src https://docs.google.com https://accounts.google.com/gsi/; "
        "connect-src 'self' https://accounts.google.com/gsi/ https://apis.google.com; "
        "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; "
        "img-src 'self' data: https://*.gstatic.com https://*.googleusercontent.com"
    ),
    # The API key is restricted to this site's address, which Google reads from the Referer: the origin only, never the path.
    "Referrer-Policy": "strict-origin",
}


@router.get(PICKER_PATH)
async def picker_page(request: Request, file: str = "") -> HTMLResponse:
    g = owner_app(request).google
    config = g.picker_config()
    problem = ""
    if config is None:
        problem = "The file picker is not set up yet. Add GOOGLE_PICKER_API_KEY and GOOGLE_PROJECT_NUMBER to .env, then restart Dom (docs/google.md has the steps)."
    elif not await g.available("drive_files"):
        problem = "Turn on Files in Google Drive in Settings → Services → Google first."
    return HTMLResponse(templates.get_template("google_picker.html").render(
        problem=problem, config=config if not problem else None, file=file if FILE_ID.fullmatch(file) else "",
    ))
