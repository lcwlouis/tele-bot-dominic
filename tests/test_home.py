import pytest

from dom.home import LIMITS, Home, PrivateAccessError, init_home


@pytest.fixture
def root(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "private").mkdir()
    return tmp_path


def test_write_over_limit_rejected_unchanged(root):
    path = root / "memory" / "HABITS.md"
    path.write_text("sleep: late")
    before = path.read_bytes()
    res = Home(root).memory_write("memory/HABITS.md", "add", "x" * (LIMITS["memory/HABITS.md"] + 1))
    assert res.ok is False
    assert res.error
    assert res.content == "sleep: late"
    assert path.read_bytes() == before


def test_over_limit_file_can_shrink_but_not_grow(root):
    # Imported files may start over their limit; Dom must be able to prune them.
    limit = LIMITS["memory/HABITS.md"]
    path = root / "memory" / "HABITS.md"
    path.write_text("- keep\n- " + "y" * limit)
    home = Home(root)
    assert home.memory_write("memory/HABITS.md", "add", "more").ok is False
    res = home.memory_write("memory/HABITS.md", "remove", "", "y" * 10)
    assert res.ok is True
    assert path.read_text() == "- keep"


def test_add_replace_remove_roundtrip(root):
    home = Home(root)
    assert home.memory_write("memory/MEMORY.md", "add", "likes tea").content == "- likes tea"
    home.memory_write("memory/MEMORY.md", "add", "has a dog")
    res = home.memory_write("memory/MEMORY.md", "replace", "text", old="a dog")
    assert res.ok and "- has text" in res.content
    res = home.memory_write("memory/MEMORY.md", "remove", "", old="likes tea")
    assert res.ok and res.content == "- has text"
    assert home.read("memory/MEMORY.md") == "- has text"


def test_private_read_blocked_without_flag(root):
    (root / "private" / "USER.md").write_text("secret")
    with pytest.raises(PrivateAccessError):
        Home(root).read("private/USER.md")
    with pytest.raises(PrivateAccessError):
        Home(root).memory_write("private/NOTES.md", "add", "x")
    assert Home(root).scoped(allow_private=True).read("private/USER.md") == "secret"
    # scoping back down removes access again
    with pytest.raises(PrivateAccessError):
        Home(root, allow_private=True).scoped(allow_private=False).read("private/USER.md")


def test_missing_file_reads_empty(root):
    assert Home(root).read("memory/people/9.md") == ""


def test_replace_requires_unique_match(root):
    home = Home(root)
    (root / "memory" / "MEMORY.md").write_text("- a cat\n- a cat\n- a bird")
    res = home.memory_write("memory/MEMORY.md", "replace", "dog", old="a cat")
    assert not res.ok and "2" in res.error
    res = home.memory_write("memory/MEMORY.md", "replace", "dog", old="a fish")
    assert not res.ok
    res = home.memory_write("memory/MEMORY.md", "replace", "dog", old=None)
    assert not res.ok
    assert (root / "memory" / "MEMORY.md").read_text() == "- a cat\n- a cat\n- a bird"
    assert home.memory_write("memory/MEMORY.md", "replace", "dog", old="a bird").ok


def test_path_traversal_rejected(root):
    (root / "SOUL.md").write_text("soul")
    home = Home(root, allow_private=True)
    for bad in ("memory/../SOUL.md", "/etc/passwd", "private/../SOUL.md"):
        res = home.memory_write(bad, "add", "x")
        assert not res.ok
        with pytest.raises(ValueError):
            home.read(bad)
    assert (root / "SOUL.md").read_text() == "soul"


def test_only_known_memory_files_writable(root):
    res = Home(root).memory_write("SOUL.md", "add", "x")
    assert not res.ok
    res = Home(root).memory_write("memory/other.md", "add", "x")
    assert not res.ok
    assert Home(root).memory_write("memory/JOURNAL.md", "add", "x" * 5000).ok


def test_soul_front_matter_parsed(root):
    (root / "SOUL.md").write_text(
        '---\nname: "Dom"\nage: 24\ntraits:\n  humor_level: 0.5\n  emoji_level: 0.25\n---\nBody line.\n'
    )
    meta, body = Home(root).soul()
    assert meta == {"name": "Dom", "age": 24, "traits": {"humor_level": 0.5, "emoji_level": 0.25}}
    assert body.strip() == "Body line."


def test_soul_without_front_matter(root):
    (root / "SOUL.md").write_text("just text")
    assert Home(root).soul() == ({}, "just text")


def test_init_home_refuses_nonempty(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        init_home(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_init_home_copies_starter_kit(tmp_path):
    target = tmp_path / "newhome"
    init_home(target)
    for rel in ("config.toml", "SOUL.md", ".env.example", "memory/MEMORY.md", "memory/HABITS.md",
                "memory/JOURNAL.md", "private/USER.md", "private/NOTES.md"):
        assert (target / rel).is_file(), rel
    meta, body = Home(target).soul()
    assert len(meta["traits"]) == 8 and set(meta["traits"].values()) == {0.5}
    assert 'sleep: "23:59-07:30"' in Home(target).read("memory/HABITS.md")
    # starter files respect their own limits
    h = Home(target, allow_private=True)
    for rel, limit in LIMITS.items():
        if "*" not in rel:
            assert len(h.read(rel)) <= limit
