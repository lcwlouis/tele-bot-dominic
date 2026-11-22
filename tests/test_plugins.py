import pytest

from dom.home import Home
from dom.plugins.manifest import Manifest, read_plugin, validate_plugin
from dom.plugins.registry import PluginRegistry
from dom.store import Store

MANIFEST = """name = "convert"
description = "Convert units"
version = "1"
[[tools]]
name = "length"
description = "Convert lengths"
parameters = {type = "object"}
"""


def write_plugin(root, manifest=MANIFEST):
    folder = root / "plugins" / "convert"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.toml").write_text(manifest)
    (folder / "main.py").write_text("print('hello')\n")
    return folder


def test_manifest_defaults():
    manifest = Manifest.parse(MANIFEST)
    assert manifest.name == "convert"
    assert manifest.version == "1"
    assert manifest.tools[0].scopes == ("owner_dm",)
    assert manifest.network is False
    assert manifest.storage is False
    assert manifest.timeout_seconds == 10
    assert manifest.memory_mb == 64


def test_storage_is_an_offline_opt_in():
    manifest = MANIFEST.replace('version = "1"', 'version = "1"\nstorage = true')
    assert Manifest.parse(manifest).storage
    with pytest.raises(ValueError, match="offline"):
        Manifest.parse(manifest.replace("storage = true", "storage = true\nnetwork = true"))


@pytest.mark.parametrize("scope", ["group", "friend_dm"])
@pytest.mark.parametrize("surface", ["tool", "command"])
def test_storage_surfaces_are_owner_only(scope, surface):
    manifest = MANIFEST.replace('version = "1"', 'version = "1"\nstorage = true')
    if surface == "command":
        manifest += '\n[[commands]]\nname = "echo"\ndescription = "Echo"\n'
    with pytest.raises(ValueError, match='storage.*owner_dm'):
        Manifest.parse(manifest + f'scopes = ["owner_dm", "{scope}"]\n')


@pytest.mark.parametrize(("old", "new"), [
    ('"convert"', '"c"'),
    ('"convert"', '"Convert"'),
    ('"convert"', '"convert-tool"'),
    ('"convert"', '"' + "a" * 33 + '"'),
    ('"Convert units"', '"' + "a" * 201 + '"'),
    ('"Convert units"', '"hidden\u034f"'),
    ('"Convert units"', '"two\\nlines"'),
    ('version = "1"', ''),
    ('version = "1"', 'version = 1'),
    ('[[tools]]', '[tools]'),
    ('"length"', '"bad name"'),
    ('"Convert lengths"', '"hidden\\u3164"'),
    ('{type = "object"}', '"object"'),
    ('{type = "object"}', '{type = "string"}'),
    ('{type = "object"}', '{type = "object", title = "' + "a" * 4096 + '"}'),
    ('{type = "object"}', '{type = "object"}\nscopes = ["private"]'),
    ('{type = "object"}', '{type = "object"}\nscopes = "owner_dm"'),
    ('version = "1"', 'version = "1"\nnetwork = "false"'),
    ('version = "1"', 'version = "1"\ntimeout_seconds = 0'),
    ('version = "1"', 'version = "1"\ntimeout_seconds = 31'),
    ('version = "1"', 'version = "1"\ntimeout_seconds = true'),
    ('version = "1"', 'version = "1"\nmemory_mb = 15'),
    ('version = "1"', 'version = "1"\nmemory_mb = 257'),
])
def test_manifest_validation(old, new):
    with pytest.raises(ValueError):
        Manifest.parse(MANIFEST.replace(old, new))


def test_manifest_custom_settings():
    manifest = Manifest.parse(
        MANIFEST.replace('version = "1"', 'version = "1"\nnetwork = true\ntimeout_seconds = 30\nmemory_mb = 16')
    )
    assert (manifest.network, manifest.timeout_seconds, manifest.memory_mb) == (True, 30, 16)
    assert manifest.tools[0].scopes == ("owner_dm",)
    offline = Manifest.parse(MANIFEST + 'scopes = ["friend_dm", "group"]\n')
    assert offline.tools[0].scopes == ("friend_dm", "group")


@pytest.mark.parametrize("scopes", [
    '["friend_dm"]', '["group"]', '["owner_dm", "group"]', '["owner_dm", "friend_dm", "group"]', "[]",
])
def test_network_plugins_are_owner_dm_only(scopes):
    online = MANIFEST.replace('version = "1"', 'version = "1"\nnetwork = true')
    with pytest.raises(ValueError, match="owner_dm"):
        Manifest.parse(online + f"scopes = {scopes}\n")
    assert Manifest.parse(online + 'scopes = ["owner_dm", "owner_dm"]\n').tools[0].scopes == ("owner_dm",)
    second = '[[tools]]\nname = "other"\ndescription = "Other"\nparameters = {type = "object"}\nscopes = ["group"]\n'
    with pytest.raises(ValueError, match="owner_dm"):
        Manifest.parse(online + second)


def test_hash_stable_in_filename_order_and_sensitive_to_paths_and_bytes(tmp_path):
    folder = write_plugin(tmp_path)
    (folder / "util.py").write_text("VALUE = 1\n")
    original = read_plugin(folder).hash
    (folder / "main.py").unlink()
    (folder / "main.py").write_text("print('hello')\n")
    assert read_plugin(folder).hash == original
    (folder / "util.py").rename(folder / "helper.py")
    renamed = read_plugin(folder).hash
    assert renamed != original
    (folder / "helper.py").write_text("VALUE = 2\n")
    assert read_plugin(folder).hash != renamed


@pytest.mark.parametrize("invalid", ["symlink", "root_link", "hardlink", "size", "count", "other", "missing", "name", "invisible"])
def test_plugin_file_refusal(tmp_path, invalid):
    folder = write_plugin(tmp_path)
    if invalid == "symlink":
        (folder / "helper.py").symlink_to(folder / "main.py")
    elif invalid == "root_link":
        actual = folder.rename(tmp_path / "actual")
        folder.symlink_to(actual, target_is_directory=True)
    elif invalid == "hardlink":
        (folder / "helper.py").hardlink_to(folder / "main.py")
    elif invalid == "size":
        (folder / "main.py").write_text("x" * 65536)
    elif invalid == "count":
        for n in range(9):
            (folder / f"helper{n}.py").touch()
    elif invalid == "other":
        (folder / "notes.md").touch()
    elif invalid == "missing":
        (folder / "main.py").unlink()
    elif invalid == "name":
        (folder / "plugin.toml").write_text(MANIFEST.replace('"convert"', '"another"'))
    elif invalid == "invisible":
        (folder / "main.py").write_text("# hidden\u034f\n")
    with pytest.raises(ValueError):
        read_plugin(folder)


async def test_scan_scope_filtering_and_restaging(tmp_path):
    folder = write_plugin(tmp_path, MANIFEST + 'scopes = ["owner_dm", "friend_dm"]\n')
    (folder.parent / "invalid").mkdir()
    store = Store(tmp_path, owner_id=111)
    try:
        registry = PluginRegistry(Home(tmp_path), store)
        await registry.scan()
        row, = await store.list_plugins()
        assert row["status"] == "pending"
        assert await registry.approved_tools("owner_dm") == []
        assert await store.set_plugin_status("convert", row["hash"], "approved")
        for scope in ("owner_dm", "friend_dm"):
            schema, = await registry.approved_tools(scope)
            assert schema["function"]["name"] == "plugin_convert_length"
            assert schema["function"]["parameters"] == {"type": "object"}
        assert await registry.approved_tools("group") == []
        (folder / "main.py").write_text("print('changed')\n")
        assert await registry.approved_tools("owner_dm") == []
        changed, = await store.list_plugins()
        assert changed["status"] == "pending"
        assert changed["hash"] != row["hash"]
        assert changed["approved_hash"] == row["hash"]
        assert not await store.set_plugin_status("convert", row["hash"], "approved")
    finally:
        store.close()


@pytest.mark.parametrize("separator", [" ", " "])
def test_line_and_paragraph_separators_count_as_invisible(separator):
    from dom.skills import _visible

    assert not _visible(f"one{separator}two")
    assert _visible("one\ntwo\tthree")
    with pytest.raises(ValueError):
        validate_plugin("convert", {
            "plugin.toml": MANIFEST.encode(), "main.py": f"x = 1{separator}y = 2\n".encode(),
        })
    with pytest.raises(ValueError):
        Manifest.parse(MANIFEST.replace("Convert units", f"Convert{separator}units"))


async def test_drafts_are_skipped_silently_and_cleaned_up_at_startup(tmp_path, caplog):
    folder = write_plugin(tmp_path)
    leftover = folder.parent / ".draft-abc123" / "draft"
    leftover.mkdir(parents=True)
    (leftover / "main.py").write_text("print('half written')\n")
    (folder.parent / ".draft-file").write_text("junk")
    (folder.parent / ".draft-link").symlink_to(tmp_path, target_is_directory=True)
    store = Store(tmp_path, owner_id=111)
    try:
        registry = PluginRegistry(Home(tmp_path), store)
        with caplog.at_level("WARNING"):
            assert set(await registry.scan()) == {"convert"}
        assert "Skipping" not in caplog.text
        await registry.cleanup_drafts()
        assert [path.name for path in folder.parent.iterdir()] == ["convert"]
        assert (tmp_path / "plugins" / "convert" / "main.py").exists() and tmp_path.is_dir()
    finally:
        store.close()


async def test_cleanup_restores_a_plugin_whose_swap_was_interrupted(tmp_path):
    folder = write_plugin(tmp_path)
    previous = folder.parent / ".draft-crash" / "previous"
    previous.parent.mkdir()
    folder.rename(previous)
    store = Store(tmp_path, owner_id=111)
    try:
        await PluginRegistry(Home(tmp_path), store).cleanup_drafts()
        assert (folder / "main.py").read_text() == "print('hello')\n"
        assert not previous.parent.exists()
    finally:
        store.close()


async def test_disabled_plugin_returns_to_pending_when_its_files_change(tmp_path):
    folder = write_plugin(tmp_path)
    store = Store(tmp_path, owner_id=111)
    try:
        registry = PluginRegistry(Home(tmp_path), store)
        await registry.scan()
        row, = await store.list_plugins()
        assert await store.set_plugin_status("convert", row["hash"], "disabled")
        await registry.scan()
        assert (await store.list_plugins())[0]["status"] == "disabled"
        (folder / "main.py").write_text("print('changed')\n")
        await registry.scan()
        changed, = await store.list_plugins()
        assert changed["status"] == "pending" and changed["hash"] != row["hash"]
    finally:
        store.close()
