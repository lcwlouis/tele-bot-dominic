"""The old server-rendered dashboard is gone: its addresses redirect to the app, Dom's links point at the app,
and nothing but the desktop viewer still serves a server-rendered page."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import dom.web
from dom.web.app import create_app
from dom.web.auth import Auth
from test_plugin_tools import draft, review_path, status
from test_web_auth import ORIGIN, POST_HEADERS, TOKEN
from tghelpers import OWNER

HASH = "ab" * 32
MOVED = [
    ("/", "/app"),
    ("/chats", "/app/chats"),
    ("/memory", "/app/memory"),
    ("/desktop", "/app/work"),
    ("/own-work", "/app/work/own-work"),
    ("/reminders", "/app/work/reminders"),
    ("/briefs", "/app/work/briefs"),
    ("/settings", "/app/system"),
    ("/plugins", "/app/system/plugins"),
    (f"/plugins/convert/{HASH}", f"/app/system/plugins/convert/{HASH}"),
    ("/usage", "/app/system/usage"),
    ("/turns", "/app/system/turns"),
    ("/proposals", "/app/waiting"),
]
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def web(tmp_path, monkeypatch):
    from dom.config import Config

    monkeypatch.setenv("DOM_TEST_TOKEN", TOKEN)
    config = Config(home=tmp_path, web={"dev_token_env": "DOM_TEST_TOKEN"})
    return create_app(SimpleNamespace(owner_id=None), Auth(config))


def client(web):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN, headers=AUTH, follow_redirects=False)


@pytest.mark.parametrize(("old", "new"), MOVED)
async def test_every_old_page_redirects_to_its_place_in_the_app(web, old, new):
    async with client(web) as http:
        for method in ("GET", "HEAD"):
            response = await http.request(method, old)
            assert response.status_code == 307 and response.headers["location"] == new
        # Whatever the request carries, the target is fixed: a query string never travels.
        response = await http.get(old, params={"next": "https://evil.example.org", "container": "x"})
        assert response.headers["location"] == new
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=ORIGIN) as stranger:
        assert (await stranger.get(old)).status_code == 403  # the redirect never answers the unauthenticated


@pytest.mark.parametrize("old", [path for path, _ in MOVED])
async def test_a_redirect_is_for_reading_only(web, old):
    async with client(web) as http:
        for method in ("POST", "PUT", "DELETE"):
            response = await http.request(method, old, headers=POST_HEADERS)
            assert response.status_code in (403, 405) and "location" not in response.headers


@pytest.mark.parametrize("name,digest", [
    ("Bad", HASH), ("a", HASH), ("convert", "0" * 63), ("convert", HASH.upper()), ("convert", "g" * 64),
    ("convert", HASH + "0"), ("convert", "..%2F..%2Fapp"),
])
async def test_a_plugin_review_redirect_checks_the_name_and_hash_like_the_api(web, name, digest):
    async with client(web) as http:
        response = await http.get(f"/plugins/{name}/{digest}")
        assert response.status_code == 404 and "location" not in response.headers


async def test_no_old_route_still_serves_html(web):
    gone = [
        "/desktop/state", "/desktop/tile", "/desktop/history", "/own-work/1/files/shot.png", "/plugin-panels/convert/summary",
        "/chats/1/wake", "/memory/save", "/proposals/1/approve", "/skills/notes/retire", "/static/dashboard.js",
        "/static/dashboard.css", "/static/htmx.min.js", "/static/model-settings.js", "/static/htmx-LICENSE",
    ]
    async with client(web) as http:
        for path in gone:
            response = await http.get(path)
            assert response.status_code in (404, 405), path
            assert "text/html" not in response.headers.get("content-type", ""), path
        for path in ("/chats/1/wake", "/memory/save", "/proposals/1/approve", "/stickers", "/plugins/convert/delete"):
            assert (await http.post(path, headers=POST_HEADERS)).status_code == 403, path
    # The only server-rendered pages left are the desktop viewer and the Google file picker (the one page with its own CSP
    # for Google's scripts, so they never load into the app); the built app is static files.
    templates = {path.name for path in (Path(dom.web.__file__).parent / "templates").iterdir()}
    assert templates == {"desktop_viewer.html", "google_picker.html"}
    statics = {path.name for path in (Path(dom.web.__file__).parent / "static").iterdir()}
    assert statics == {"app", "desktop-viewer.css", "desktop-viewer.js", "fonts", "google-picker.css", "google-picker.js", "novnc"}


async def test_the_plugin_review_link_dom_sends_opens_the_app_page(make_app, browser):
    app = make_app()
    app.config = app.config.model_copy(update={
        "web": app.config.web.model_copy(update={"public_url": "https://example.org"}),
    })
    await draft(app)
    row = await status(app)
    sent, = app.bot.of("send", OWNER)
    urls = [button.url for buttons in sent[3]["reply_markup"].inline_keyboard for button in buttons if button.url]
    assert urls == [f"https://example.org/app/system/plugins/convert/{row['hash']}"]
    assert review_path(row) == urls[0].removeprefix("https://example.org")
    async with browser(app) as http:  # the link serves the app page, and the review the page shows is there
        assert (await http.get(review_path(row))).headers["content-type"].startswith("text/html")
        assert (await http.get(f"/api/system/plugins/convert/{row['hash']}")).status_code == 200


async def test_the_desktop_handoff_link_opens_the_live_viewer_on_the_right_computer(make_app):
    app = make_app()
    app.config = app.config.model_copy(update={
        "web": app.config.web.model_copy(update={"public_url": "https://example.org"}),
    })
    await app.dispatch.desktop_handoff(7, 3, OWNER, "log in to the site")
    await app.dispatch.desktop_handoff(8, 1, OWNER, "log in to the shop", container="shop")
    links = [
        button.url for call in app.bot.of("send", OWNER) for buttons in call[3]["reply_markup"].inline_keyboard
        for button in buttons if button.url
    ]
    assert links == ["https://example.org/desktop/vnc/index.html", "https://example.org/desktop/vnc/index.html?container=shop"]
