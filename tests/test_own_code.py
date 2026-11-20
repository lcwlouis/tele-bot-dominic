import json
import os

import pytest

from dom import own_code
from dom.tools import OWN_CODE_TOOL, SELF_INSPECT_TOOL, TurnCtx, enabled_tools
from tghelpers import FRIEND, GROUP, OWNER


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "site"
    (root / "dom/web").mkdir(parents=True)
    (root / "dom/__pycache__").mkdir()
    (root / "docs").mkdir()
    (root / "dom/tools.py").write_text("def alpha():\n    return 'Needle one'\n\n\ndef beta():\n    return 'needle two'\n")
    (root / "dom/web/page.html").write_text("<p>NEEDLE html</p>\n")
    (root / "dom/__pycache__/tools.cpython-312.pyc").write_bytes(b"\x00\x01")
    (root / "dom/blob.bin").write_bytes(b"\x00\x01needle")
    (root / "dom/.env").write_text("needle=secret\n")
    (root / "docs/guide.md").write_text("# Guide\nneedle in docs\n")
    (root / "README.md").write_text("# Dom\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\nintro\n\n## [Unreleased]\n\n- later\n\n## [1.0.0] - 2026-01-01\n\n- a\n- b\n")
    (root / "outside.md").write_text("needle outside\n")
    (root / "private").mkdir()
    (root / "private/NOTES.md").write_text("needle private\n")
    return root


def test_list_shows_text_files_with_sizes_and_nothing_else(base):
    lines = own_code.list_files(base).splitlines()
    assert lines == [
        "CHANGELOG.md (%d bytes)" % (base / "CHANGELOG.md").stat().st_size,
        "README.md (%d bytes)" % (base / "README.md").stat().st_size,
        "docs/guide.md (%d bytes)" % (base / "docs/guide.md").stat().st_size,
        "dom/tools.py (%d bytes)" % (base / "dom/tools.py").stat().st_size,
        "dom/web/page.html (%d bytes)" % (base / "dom/web/page.html").stat().st_size,
    ]


def test_list_can_be_narrowed_to_a_folder(base):
    assert own_code.list_files(base, "dom/web").splitlines() == [
        "dom/web/page.html (%d bytes)" % (base / "dom/web/page.html").stat().st_size
    ]


def test_list_is_capped_and_says_so(base):
    for n in range(400):
        (base / "docs" / f"{'x' * 40}{n}.md").write_text("x")
    listing = own_code.list_files(base)
    assert len(listing) <= own_code.READ_LIMIT + 200 and "[truncated" in listing


def test_read_returns_the_file(base):
    assert own_code.read_file(base, "docs/guide.md") == "# Guide\nneedle in docs\n"


def test_read_is_capped_with_a_truncated_note(base):
    (base / "docs/long.md").write_text("a" * 20000)
    text = own_code.read_file(base, "docs/long.md")
    assert text.startswith("a" * 12000) and "a" * 12001 not in text
    assert "truncated" in text and "12000 of 20000" in text


@pytest.mark.parametrize("path", [
    "../site/outside.md", "dom/../outside.md", "/etc/passwd", "outside.md", "private/NOTES.md",
    "dom/.env", "dom/blob.bin", "dom/__pycache__/tools.cpython-312.pyc", "dom", "dom/missing.py", "", "dom/\x00x.py",
])
def test_read_refuses_everything_outside_the_allowed_files(base, path):
    assert own_code.read_file(base, path).startswith("error:")


def test_read_refuses_symlinks_to_files_and_folders(base, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("needle secret\n")
    os.symlink(secret, base / "docs/link.md")
    os.symlink(tmp_path, base / "docs/dir")
    os.symlink(base / "outside.md", base / "dom/inside.py")
    for path in ("docs/link.md", "docs/dir/secret.md", "dom/inside.py"):
        assert own_code.read_file(base, path).startswith("error:")
    assert "link.md" not in own_code.list_files(base) and "inside.py" not in own_code.list_files(base)
    assert "secret" not in own_code.search(base, "needle")


def test_read_refuses_text_that_is_not_utf8(base):
    (base / "docs/latin.md").write_bytes("caf\xe9".encode("latin-1"))
    assert own_code.read_file(base, "docs/latin.md").startswith("error:")


def test_search_is_case_insensitive_and_reports_file_line_and_text(base):
    assert own_code.search(base, "needle").splitlines() == [
        "docs/guide.md:2: needle in docs",
        "dom/tools.py:2: return 'Needle one'",
        "dom/tools.py:6: return 'needle two'",
        "dom/web/page.html:1: <p>NEEDLE html</p>",
    ]


def test_search_is_limited_to_30_hits_and_the_given_folder(base):
    (base / "docs/many.md").write_text("needle\n" * 50)
    hits = own_code.search(base, "needle").splitlines()
    assert len(hits) == 31 and "limited to 30" in hits[-1]
    assert own_code.search(base, "needle", "dom/web").splitlines() == ["dom/web/page.html:1: <p>NEEDLE html</p>"]


def test_search_needs_a_query_and_reports_no_hits(base):
    assert own_code.search(base, "  ").startswith("error:")
    assert own_code.search(base, "absent") == "no matches"


def test_changelog_returns_the_top_sections_within_the_cap(base):
    text = own_code.changelog(base)
    assert text.startswith("## [Unreleased]") and "## [1.0.0] - 2026-01-01" in text and "intro" not in text
    (base / "CHANGELOG.md").write_text("## [2.0.0]\n\n" + "b" * 5000 + "\n\n## [1.0.0]\n\n" + "c" * 5000 + "\n")
    text = own_code.changelog(base)
    assert "bbbb" in text and "cccc" not in text and len(text) <= own_code.CHANGELOG_LIMIT
    (base / "CHANGELOG.md").write_text("## [2.0.0]\n\n" + "b" * 9000)
    text = own_code.changelog(base)
    assert len(text) <= own_code.CHANGELOG_LIMIT + 100 and "truncated" in text


def test_latest_release_summary_skips_unreleased_and_takes_three_bullets(base):
    assert own_code.latest_release(base) == {"title": "[1.0.0] - 2026-01-01", "highlights": ["a", "b"]}
    (base / "CHANGELOG.md").write_text("## [Unreleased]\n\n- x\n\n## [0.7.0] - d\n\n- 1\n- 2\n\n### Phase\n\n- 3\n- 4\n")
    assert own_code.latest_release(base) == {"title": "[0.7.0] - d", "highlights": ["1", "2", "3"]}
    (base / "CHANGELOG.md").unlink()
    assert own_code.latest_release(base) is None


def ctx(app, scope="owner_dm", chat=OWNER):
    return TurnCtx(scope, chat, chat, app.home, app.store, OWNER, app=app)


async def call(app, args, **kw):
    return await OWN_CODE_TOOL.handler(args, ctx(app, **kw))


async def test_tool_is_offered_in_the_owner_dm_only(make_app):
    app = make_app()
    assert OWN_CODE_TOOL.scopes == {"owner_dm"}
    assert "own_code" in [tool.name for tool in await enabled_tools(app, "owner_dm")]
    for scope in ("friend_dm", "group"):
        assert "own_code" not in [tool.name for tool in await enabled_tools(app, scope)]
    for scope, chat in (("group", GROUP), ("friend_dm", FRIEND)):
        assert (await call(app, {"action": "list"}, scope=scope, chat=chat)).startswith("error:")


async def test_tool_reads_this_installation_for_every_action(make_app):
    app = make_app()
    assert "dom/tools.py" in await call(app, {"action": "list"})
    assert "def resolve" in await call(app, {"action": "read", "path": "dom/_version.py"})
    assert "dom/own_code.py:" in await call(app, {"action": "search", "query": "READ_LIMIT"})
    assert (await call(app, {"action": "changelog"})).startswith("## [Unreleased]")  # older sections drop out as it grows


async def test_tool_reports_bad_arguments(make_app):
    app = make_app()
    for args in ({}, {"action": "write"}, {"action": "read"}, {"action": "search"}, {"action": "read", "path": 3}):
        assert (await call(app, args)).startswith("error:")
    assert (await call(app, {"action": "read", "path": "../x"})).startswith("error:")


async def test_self_inspect_reports_version_and_latest_release(make_app):
    app = make_app()
    found = json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))
    assert found["version"] and found["changelog_latest"]["title"] == "[0.7.0] - 2026-11-19"
    assert len(found["changelog_latest"]["highlights"]) == 3


async def test_self_inspect_reports_the_public_repository_url(make_app):
    from dom.config import REPO_URL, UpdateConfig

    app = make_app()
    assert json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))["repository"] == REPO_URL
    app.config = app.config.model_copy(update={"update": UpdateConfig(repo_url="https://example.test/fork.git")})
    assert json.loads(await SELF_INSPECT_TOOL.handler({}, ctx(app)))["repository"] == "https://example.test/fork.git"
