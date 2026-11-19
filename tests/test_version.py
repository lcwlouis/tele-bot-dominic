import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from dom import _version

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "$Format" + ":%h$"  # split so export-subst can never touch this test file


def test_substituted_describe_is_used_as_is(tmp_path):
    assert _version.resolve("v0.7.0-3-gabc1234", "abc1234", tmp_path) == "v0.7.0-3-gabc1234"


def test_substituted_without_tags_uses_the_short_hash(tmp_path):
    assert _version.resolve("", "abc1234", tmp_path) == "abc1234"


def test_unsubstituted_placeholders_ask_git(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []

    def fake_check_output(command, **kwargs):
        calls.append((command, kwargs["cwd"]))
        return "v0.7.0-2-gdeadbee-dirty\n"

    monkeypatch.setattr(_version.subprocess, "check_output", fake_check_output)
    assert _version.resolve(PLACEHOLDER, PLACEHOLDER, tmp_path) == "v0.7.0-2-gdeadbee-dirty"
    assert calls == [(["git", "describe", "--tags", "--always", "--dirty"], tmp_path)]


def test_unsubstituted_without_a_git_directory_is_unknown(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("git must not run without a .git")

    monkeypatch.setattr(_version.subprocess, "check_output", fail)
    assert _version.resolve(PLACEHOLDER, PLACEHOLDER, tmp_path) == "unknown"


@pytest.mark.parametrize("error", [OSError("no git"), subprocess.CalledProcessError(128, "git")])
def test_git_failure_is_unknown(tmp_path, monkeypatch, error):
    (tmp_path / ".git").mkdir()

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(_version.subprocess, "check_output", fail)
    assert _version.resolve(PLACEHOLDER, PLACEHOLDER, tmp_path) == "unknown"


def test_this_checkout_has_a_version():
    assert isinstance(_version.VERSION, str) and _version.VERSION


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_archive_writes_the_tag_into_the_version_file(tmp_path):
    repo, out = tmp_path / "repo", tmp_path / "out"
    (repo / "dom").mkdir(parents=True)
    out.mkdir()
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    shutil.copy(ROOT / "dom/_version.py", repo / "dom/_version.py")
    env = os.environ | {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def git(*args, **kwargs):
        return subprocess.run(["git", *args], cwd=repo, env=env, check=True, **kwargs)

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("tag", "v9.8.7")
    archive = git("archive", "HEAD", stdout=subprocess.PIPE).stdout
    subprocess.run(["tar", "-x", "-C", str(out)], input=archive, check=True)
    assert runpy.run_path(str(out / "dom/_version.py"))["VERSION"] == "v9.8.7"
