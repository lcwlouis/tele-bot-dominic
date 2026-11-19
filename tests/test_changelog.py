import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("changelog", ROOT / "scripts/changelog.py")
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)

TRAILER = "Co-Authored-By: Someone <noreply@example.com>"


def test_commits_are_grouped_and_kept_newest_first():
    commits = [
        "fix(web): stop clipping the rail",
        "docs: explain releases",
        "feat(cli): dom doctor",
        "test(plugins): check descriptors",
        "feat: second feature",
        "ci: run the tests",
        "chore: tidy",
    ]
    assert changelog.build_section("[Unreleased]", commits) == (
        "## [Unreleased]\n\n"
        "### Added\n\n- **cli:** dom doctor\n- second feature\n\n"
        "### Fixed\n\n- **web:** stop clipping the rail\n\n"
        "### Other\n\n- docs: explain releases\n- test(plugins): check descriptors\n- ci: run the tests\n- chore: tidy\n"
    )


def test_empty_categories_are_left_out():
    assert changelog.build_section("[0.1.0] - 2026-01-01", ["fix: one"]) == (
        "## [0.1.0] - 2026-01-01\n\n### Fixed\n\n- one\n"
    )


def test_no_commits_gives_just_the_heading():
    assert changelog.build_section("[Unreleased]", []) == "## [Unreleased]\n"


def test_unconventional_and_breaking_subjects_land_in_other():
    section = changelog.build_section("[Unreleased]", ["Initial commit", "refactor(store): split module", "feat!: new format"])
    assert "### Added\n\n- new format\n" in section
    assert "### Other\n\n- Initial commit\n- refactor(store): split module\n" in section


def test_trailers_are_stripped_from_messages():
    message = f"feat(x): add y\n\nbody text\n\n{TRAILER}\nco-authored-by: Other <o@example.com>\n"
    assert changelog.subject(message) == "feat(x): add y"
    assert changelog.subject(f"{TRAILER}\nfeat: late subject") == "feat: late subject"
    assert changelog.subject(f"fix: one {TRAILER}") == "fix: one"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_command_line_reads_a_range_from_git(tmp_path):
    env = os.environ | {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    for message in ("chore: before", f"feat(a): first\n\n{TRAILER}", "fix(b): second"):
        git("commit", "-q", "--allow-empty", "-m", message)
    base = git("rev-list", "--max-parents=0", "HEAD").strip()
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts/changelog.py"), base, "HEAD", "--title", "[1.0.0] - 2026-01-02"],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    ).stdout
    assert out == "## [1.0.0] - 2026-01-02\n\n### Added\n\n- **a:** first\n\n### Fixed\n\n- **b:** second\n"
