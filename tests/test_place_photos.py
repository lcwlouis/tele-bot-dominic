"""A known place's reference photo: kept like kept media, tidied, private to the chats that know the place."""

import base64
import io
import json
import os
import stat

import pytest
from PIL import Image

from dom.place_photos import FOLDER, MAX_UPLOAD, PhotoError, slugify, tidy
from test_studio_cloud import IMAGE, PORTRAIT, allowed, cloud_server, configure  # noqa: F401
from test_traits import put
from tghelpers import FRIEND, GROUP, OWNER


def picture(fmt="PNG", size=(1600, 1200), color=(200, 30, 30), exif=False):
    out = io.BytesIO()
    image = Image.new("RGB", size, color)
    kwargs = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = "SecretCameraMaker"  # a stand-in for the GPS and camera data a phone writes
        kwargs["exif"] = data
    image.save(out, fmt, **kwargs)
    return out.getvalue()


def test_a_picture_is_kept_as_a_small_jpeg_without_its_metadata():
    raw = picture(exif=True)
    assert b"SecretCameraMaker" in raw
    kept = tidy(raw)
    assert kept.startswith(b"\xff\xd8\xff") and b"SecretCameraMaker" not in kept
    with Image.open(io.BytesIO(kept)) as image:
        assert max(image.size) == 1024 and image.format == "JPEG"
    for fmt in ("JPEG", "WEBP"):
        assert tidy(picture(fmt)).startswith(b"\xff\xd8\xff")
    flat = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    out = io.BytesIO()
    flat.save(out, "PNG")
    with Image.open(io.BytesIO(tidy(out.getvalue()))) as image:
        assert image.getpixel((1, 1)) == (255, 255, 255)  # clear pixels become white, not black


@pytest.mark.parametrize("raw", [b"", b"not a picture", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"\x89PNG\r\n\x1a\nbroken"])
def test_anything_else_is_refused(raw):
    with pytest.raises(PhotoError):
        tidy(raw)


def test_a_gif_and_an_oversized_upload_are_refused():
    with pytest.raises(PhotoError, match="JPEG, PNG or WebP"):
        tidy(picture("GIF"))
    with pytest.raises(PhotoError, match="MB"):
        tidy(b"0" * (MAX_UPLOAD + 1))


def test_a_name_becomes_a_note_name():
    assert slugify("The Blue-Door Gym!") == "the-blue-door-gym"
    assert slugify("  ../etc/passwd ") == "etc-passwd"
    assert slugify("???") == "" and len(slugify("a" * 200)) <= 64


async def test_a_photo_is_kept_privately_replaced_and_removed(make_app):
    app = make_app()
    await app.place_photos.save("the-gym", picture())
    path = app.config.home / FOLDER / "the-gym.jpg"
    assert path.is_file() and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["the-gym.jpg"]  # no temporary file is left
    assert app.place_photos.has("the-gym") and app.place_photos.read("the-gym").startswith(b"\xff\xd8\xff")
    first = path.read_bytes()
    await app.place_photos.save("the-gym", picture(color=(0, 0, 200)))
    assert path.read_bytes() != first
    assert await app.place_photos.remove("the-gym") and not app.place_photos.has("the-gym")
    assert not await app.place_photos.remove("the-gym")


async def test_a_bad_name_or_picture_writes_nothing(make_app):
    app = make_app()
    for slug in ("../evil", "Upper", "", "a b"):
        with pytest.raises(PhotoError):
            await app.place_photos.save(slug, picture())
    with pytest.raises(PhotoError):
        await app.place_photos.save("the-gym", b"nope")
    assert not (app.config.home / FOLDER).exists() or not list((app.config.home / FOLDER).iterdir())
    assert app.place_photos.read("../evil") is None and not app.place_photos.has("../evil")


async def test_only_a_named_place_a_chat_may_see_gets_its_photo(make_app):
    app = make_app()
    await app.place_photos.save("the-gym", picture())
    await app.place_photos.save("the-gym-annex", picture(color=(1, 2, 3)))
    await app.place_photos.save("no-note", picture())
    put(app.home, "memory/places/the-gym.md", "- Climbing gym {chat -1001}\n- Open until ten {any}\n")
    put(app.home, "memory/places/the-gym-annex.md", "- A back room {chat -1001}\n")
    photos = app.place_photos
    assert await photos.reference_for("the-gym", "owner_dm", OWNER) == photos.read("the-gym")  # the owner's DM knows every place
    assert await photos.reference_for("the-gym", "friend_dm", FRIEND) == photos.read("the-gym")  # it has an {any} line
    assert await photos.reference_for("the-gym", "group", -1001) == photos.read("the-gym")
    assert await photos.reference_for("the-gym-annex", "owner_dm", OWNER) == photos.read("the-gym-annex")
    assert await photos.reference_for("the-gym-annex", "friend_dm", FRIEND) is None  # not this chat's to know: refused
    assert await photos.reference_for("no-note", "friend_dm", FRIEND) is None  # a photo with no note says nothing to a chat
    for bad in (None, "", "nowhere", "../evil", "The Gym", "the gym"):  # no place, an unknown one, or not a key at all
        assert await photos.reference_for(bad, "owner_dm", OWNER) is None
    put(app.home, "memory/places/the-gym.md", "- Climbing gym {chat -1001}\n")
    assert await photos.reference_for("the-gym", "group", -1009) is None  # a place only another chat has lines for


async def test_the_turn_note_lists_the_places_a_chat_may_show_by_key(make_app):
    app = make_app()
    await app.place_photos.save("the-gym", picture())
    await app.place_photos.save("the-gym-annex", picture())
    await app.place_photos.save("the-cafe", picture())
    put(app.home, "memory/places/the-gym.md", "- Open until ten {any}\n")
    put(app.home, "memory/places/the-gym-annex.md", "- A back room {chat -1001}\n")
    put(app.home, "memory/places/the-cafe.md", "- Coffee {chat -1001}\n")
    photos = app.place_photos
    assert photos.places_for("owner_dm", OWNER) == ["the-cafe", "the-gym", "the-gym-annex"]
    assert photos.places_for("friend_dm", FRIEND) == ["the-gym"]
    note = photos.offer("friend_dm", FRIEND)
    assert "the-gym" in note and "annex" not in note and "cafe" not in note
    (app.config.home / FOLDER / "the-gym.jpg").unlink()
    assert photos.offer("friend_dm", FRIEND) == ""  # nothing to offer, no line


def test_a_place_that_is_not_a_key_is_dropped_not_an_error():
    from dom.actions import Actions, VideoNote

    assert Actions.model_validate({"bubbles": ["x"], "image": "a gym", "image_place": "the-gym"}).image_place == "the-gym"
    assert Actions.model_validate({"bubbles": ["x"], "image": "a gym", "image_place": "At The Gym!"}).image_place is None
    assert VideoNote.model_validate({"text": "hi", "scene": "a gym", "place": "the-gym"}).place == "the-gym"
    assert VideoNote.model_validate({"text": "hi", "place": "../etc"}).place == ""


async def test_the_cloud_photo_model_gets_the_place_as_a_second_reference(make_app, cloud_server):
    app = make_app()
    configure(app, cloud_server, images=(IMAGE,))
    path = app.config.home / "studio-portraits"
    path.mkdir()
    (path / "casual.png").write_bytes(PORTRAIT)
    place = tidy(picture())
    await app.cloud_studio.generate(app.config, "photo", "At the gym", "casual", OWNER, allowed, place)
    [(_, payload)] = cloud_server[1]
    portrait, where = [ref["image_url"]["url"] for ref in payload["input_references"]]
    assert portrait == "data:image/png;base64," + base64.b64encode(PORTRAIT).decode()
    assert where == "data:image/jpeg;base64," + base64.b64encode(place).decode()
    assert "real place shown in the last reference" in payload["prompt"]


async def test_a_model_that_takes_no_reference_gets_no_place_and_no_place_means_no_second_reference(make_app, cloud_server):
    app = make_app()
    no_reference = IMAGE.model_copy(update={"supports_reference": False, "allow_without_reference": True})
    configure(app, cloud_server, images=(no_reference,))
    await app.cloud_studio.generate(app.config, "photo", "At the gym", None, OWNER, allowed, tidy(picture()))
    [(_, payload)] = cloud_server[1]
    assert "input_references" not in payload and "real place" not in payload["prompt"]
    cloud_server[1].clear()
    app2 = make_app()
    configure(app2, cloud_server, images=(IMAGE,))
    (app2.config.home / "studio-portraits").mkdir()
    (app2.config.home / "studio-portraits" / "casual.png").write_bytes(PORTRAIT)
    await app2.cloud_studio.generate(app2.config, "photo", "At the gym", "casual", OWNER, allowed)
    [(_, payload)] = cloud_server[1]
    assert len(payload["input_references"]) == 1 and "real place" not in payload["prompt"]


async def test_a_photo_turn_in_a_known_place_sends_its_reference_to_the_cloud_model(make_app, cloud_server):
    from test_usage import deliver, online
    from tghelpers import tg

    app = make_app(contents=(json.dumps({"bubbles": ["look"], "image": "Dom chalking up at the gym, evening", "image_place": "the-gym"}),))
    configure(app, cloud_server, images=(IMAGE,), enabled=False, default_outfit="casual")
    (app.config.home / "studio-portraits").mkdir()
    (app.config.home / "studio-portraits" / "casual.png").write_bytes(PORTRAIT)
    await app.place_photos.save("the-gym", picture())
    put(app.home, "memory/places/the-gym.md", "- Climbing gym {any}\n")

    async def send_photo(chat_id, file, **kwargs):
        return await app.bot.send_message(chat_id, "fixture photo", **kwargs)

    app.bot.send_photo = send_photo
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [(_, payload)] = [call for call in cloud_server[1] if call[0] == "/api/v1/images"]
    assert len(payload["input_references"]) == 2
    assert payload["input_references"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_a_scene_that_only_mentions_a_place_sends_no_reference(make_app, cloud_server):
    from test_usage import deliver, online
    from tghelpers import tg

    scene = "Dom at home on the sofa, thinking about the gym"  # he is not there: the name in the text is no choice
    app = make_app(contents=(json.dumps({"bubbles": ["look"], "image": scene}),))
    configure(app, cloud_server, images=(IMAGE,), enabled=False, default_outfit="casual")
    (app.config.home / "studio-portraits").mkdir()
    (app.config.home / "studio-portraits" / "casual.png").write_bytes(PORTRAIT)
    await app.place_photos.save("the-gym", picture())
    put(app.home, "memory/places/the-gym.md", "- Climbing gym {any}\n")

    async def send_photo(chat_id, file, **kwargs):
        return await app.bot.send_message(chat_id, "fixture photo", **kwargs)

    app.bot.send_photo = send_photo
    await online(app, OWNER)
    await deliver(app, tg(chat_id=OWNER, sender=OWNER))
    [(_, payload)] = [call for call in cloud_server[1] if call[0] == "/api/v1/images"]
    assert len(payload["input_references"]) == 1  # the portrait alone


async def test_a_real_turn_offers_only_the_places_its_chat_may_show(make_app, cloud_server):
    from test_usage import deliver, online
    from tghelpers import GROUP, tg, turn_requests

    app = make_app()
    configure(app, cloud_server, images=(IMAGE,))
    await app.place_photos.save("the-gym", picture())
    await app.place_photos.save("the-cafe", picture())
    put(app.home, "memory/places/the-gym.md", "- Open until ten {any}\n")
    put(app.home, "memory/places/the-cafe.md", "- Coffee {chat -1001}\n")
    await online(app)
    await deliver(app, tg(GROUP, FRIEND, "hello", 1))
    text = json.dumps(turn_requests(app)[0]["messages"])
    assert "the-gym" in text and "the-cafe" not in text


async def test_a_place_known_only_through_the_owners_untagged_lines_is_not_shown_elsewhere(make_app):
    app = make_app()
    await app.place_photos.save("mums-flat", picture())
    put(app.home, "memory/places/mums-flat.md", "- Mum's flat, door code 4471\n")
    photos = app.place_photos
    assert await photos.reference_for("mums-flat", "owner_dm", OWNER) == photos.read("mums-flat")
    for scope, chat in (("group", GROUP), ("friend_dm", FRIEND)):
        assert await photos.reference_for("mums-flat", scope, chat) is None
        assert "mums-flat" not in photos.offer(scope, chat)
