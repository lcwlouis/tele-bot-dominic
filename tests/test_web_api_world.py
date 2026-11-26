"""The World API: auth and same-origin on every route, the sheet and its last change, places and their photos."""

import base64
import io
import json

import pytest
from PIL import Image

from dom import world_change
from dom.home import WORLD, content_version
from test_traits import put
from test_web_auth import ORIGIN

JSON = {"Origin": ORIGIN, "Content-Type": "application/json"}


def image(color=(10, 120, 40)):
    out = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


WRITES = [
    ("/api/world/place", {"name": "Gym", "note": "Climbing"}),
    ("/api/world/photo", {"slug": "gym", "data": ""}),
    ("/api/world/photo/remove", {"slug": "gym"}),
]


@pytest.mark.parametrize(("path", "body"), WRITES)
async def test_every_write_needs_auth_a_same_origin_json_request(make_app, browser, path, body):
    async with browser(make_app()) as client:
        assert (await client.post(path, json=body, headers={"Authorization": "", **JSON})).status_code == 403
        assert (await client.post(path, json=body)).status_code == 403  # no Origin
        assert (await client.post(path, json=body, headers={"Origin": "https://evil.example.org"})).status_code == 403
        assert (await client.post(path, content="{}", headers={"Origin": ORIGIN, "Content-Type": "text/plain"})).status_code == 403


async def test_reads_need_the_dashboard_auth_and_a_same_site_browser(make_app, browser):
    app = make_app()
    put(app.home, "memory/places/gym.md", "- Climbing {any}\n")
    await app.place_photos.save("gym", base64.b64decode(image()))
    async with browser(app) as client:
        for path in ("/api/world", "/api/world/places/gym/photo"):
            assert (await client.get(path, headers={"Authorization": ""})).status_code == 403
            assert (await client.get(path)).status_code == 200
        assert (await client.get("/api/world/places/gym/photo", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403


async def test_the_world_holds_the_sheet_the_last_change_and_every_place(make_app, browser):
    app = make_app()
    put(app.home, WORLD, "- Works from home\n")
    put(app.home, "memory/places/the-gym.md", "- Climbing gym with a blue door {chat -1001} {entities place:the-gym}\n- Open late {any}\n")
    put(app.home, "memory/projects/book.md", "- A book {any}\n")  # a project is not a place
    put(app.home, "private/USER.md", "Owner only")
    await world_change.apply(app, {"reason": "took up climbing", "ops": [{"op": "add", "text": "Climbs on Thursdays"}]}, app.dispatch.now().date())
    async with browser(app) as client:
        world = (await client.get("/api/world")).json()
    assert world["sheet"]["file"] == WORLD and "Climbs on Thursdays" in world["sheet"]["content"]
    assert world["sheet"]["version"] == content_version(world["sheet"]["content"]) and world["sheet"]["limit"] == 2400
    assert world["last_change"] == {"day": app.dispatch.now().date().isoformat(), "reason": "took up climbing"}
    assert world["interval_days"] == 7
    assert world["places"] == [{
        "slug": "the-gym", "name": "the gym", "note": "Climbing gym with a blue door Open late", "photo": False,
    }]
    assert "Owner only" not in json.dumps(world)


async def test_the_sheet_opens_and_saves_through_the_memory_api_with_the_owners_edit_pinned(make_app, browser):
    app = make_app()
    put(app.home, WORLD, "- Works from home\n")
    async with browser(app) as client:
        listed = (await client.get("/api/memory")).json()
        assert "memory/WORLD.md" in [f["path"] for s in listed["sections"] for f in s["files"]]
        opened = (await client.get("/api/memory/file", params={"file": WORLD})).json()
        assert opened["label"] == "WORLD.md" and not opened["readonly"]
        saved = await client.post("/api/memory/save", headers=JSON, json={
            "file": WORLD, "version": opened["version"], "content": opened["content"] + "- Lives by the sea\n",
        })
    assert saved.status_code == 200 and "- Lives by the sea {core owner" in saved.json()["content"]


async def test_the_owner_adds_a_place_and_its_line_is_pinned(make_app, browser):
    app = make_app()
    async with browser(app) as client:
        made = await client.post("/api/world/place", headers=JSON, json={"name": "The Blue-Door Gym!", "note": "Small {climbing} gym"})
        assert made.status_code == 200
        assert [p["slug"] for p in made.json()["places"]] == ["the-blue-door-gym"]
        assert (await client.post("/api/world/place", headers=JSON, json={"name": "the blue door gym", "note": "again"})).status_code == 409
        for bad in ({"name": "???", "note": "x"}, {"name": "x", "note": ""}, {"name": "x", "note": "y", "extra": 1}):
            assert (await client.post("/api/world/place", headers=JSON, json=bad)).status_code == 422
    text = app.home.read("memory/places/the-blue-door-gym.md")
    assert text.startswith("- Small (climbing) gym {any} {entities place:the-blue-door-gym} {core owner")


async def test_a_photo_is_added_served_replaced_and_removed(make_app, browser):
    app = make_app()
    put(app.home, "memory/places/gym.md", "- Climbing {any}\n")
    async with browser(app) as client:
        added = await client.post("/api/world/photo", headers=JSON, json={"slug": "gym", "data": image()})
        assert added.status_code == 200 and added.json()["places"][0]["photo"] is True
        served = await client.get("/api/world/places/gym/photo")
        assert served.headers["content-type"] == "image/jpeg" and served.content.startswith(b"\xff\xd8\xff")
        assert (await client.post("/api/world/photo", headers=JSON, json={"slug": "gym", "data": image((0, 0, 0))})).status_code == 200
        assert (await client.post("/api/world/photo/remove", headers=JSON, json={"slug": "gym"})).json()["places"][0]["photo"] is False
        assert (await client.get("/api/world/places/gym/photo")).status_code == 404
        assert (await client.post("/api/world/photo/remove", headers=JSON, json={"slug": "gym"})).status_code == 404


async def test_a_bad_photo_or_place_is_refused_with_a_sentence(make_app, browser):
    app = make_app()
    put(app.home, "memory/places/gym.md", "- Climbing {any}\n")
    async with browser(app) as client:
        post = lambda slug, data: client.post("/api/world/photo", headers=JSON, json={"slug": slug, "data": data})  # noqa: E731
        assert (await post("gym", base64.b64encode(b"not a picture").decode())).status_code == 422
        assert (await post("gym", "!!! not base64")).status_code == 422
        for slug in ("nowhere", "../../private/USER", "Gym", ""):
            assert (await post(slug, image())).status_code in (404, 422)
        assert (await client.get("/api/world/places/nowhere/photo")).status_code == 404
        assert (await client.get("/api/world/places/..%2Fx/photo")).status_code in (404, 422)
    assert not app.place_photos.has("gym")
