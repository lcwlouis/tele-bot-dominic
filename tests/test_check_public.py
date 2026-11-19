import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_public.py"
PLANTED = {
    "Telegram ID": "5738" + "26041",
    "Telegram handle": "@" + "unlisted_demo_bot",
    "private IPv4": ".".join(("192", "168", "45", "67")),
    "email address": "person" + "@" + "unlisted.invalid",
}


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def track(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    subprocess.run(["git", "add", "--", name], cwd=root, check=True)
    return path


def scan(root):
    return subprocess.run([sys.executable, SCRIPT], cwd=root, text=True, capture_output=True)


def test_detects_planted_values_without_echoing_them(repository):
    track(repository, "settings.md", "\n".join(PLANTED.values()))
    result = scan(repository)
    assert result.returncode == 1
    for line, (label, value) in enumerate(PLANTED.items(), start=1):
        assert f"settings.md:{line}: {label}" in result.stdout
        assert value not in result.stdout


def test_detects_signed_ids_and_handles_in_python_strings(repository):
    identifier = PLANTED["Telegram ID"]
    track(repository, "sample.py", (
        f"CHAT = -{identifier}\n"
        f'NAME = """\n{PLANTED["Telegram handle"]}\n"""\n'
        "@" "decorator\ndef demo():\n    pass\n"
    ))
    result = scan(repository)
    assert result.returncode == 1
    assert "sample.py:1: Telegram ID" in result.stdout
    assert "sample.py:3: Telegram handle" in result.stdout
    assert "sample.py:5:" not in result.stdout


def test_generic_values_and_scope_exclusions(repository):
    noreply = "1234" + "5678+author@users.noreply.github.com"
    track(repository, "readme.md", f"""
Allow example addresses at 192.0.2.1, 198.51.100.2 and 203.0.113.3.
A loopback listener can bind 127.0.0.1 or 0.0.0.0 for all interfaces.
A firewall can drop 192.168.0.0/16 and 100.64.0.0/10.
Use owner@example.org or {noreply}.
""")
    track(repository, "main.css", "@" "media (min-width: 1em) {}")
    track(repository, "uv.lock", PLANTED["Telegram ID"])
    track(repository, "dom/web/static/novnc/core/fixture.js", PLANTED["Telegram handle"])
    track(repository, "image.dat", b"\x00" + PLANTED["Telegram ID"].encode())
    (repository / "untracked.txt").write_text(PLANTED["Telegram ID"])
    result = scan(repository)
    assert result.returncode == 0
    assert "Public data check passed" in result.stdout


def test_allowlisted_cidr_only_accepts_the_exact_generic_network(repository):
    address = PLANTED["private IPv4"]
    track(repository, "config.toml", f'gateway = "{address}/16"\n')
    result = scan(repository)
    assert result.returncode == 1
    assert "config.toml:1: private IPv4" in result.stdout


def test_scans_current_tracked_contents_instead_of_old_index(repository):
    path = track(repository, "file.txt", "initial safe text")
    path.write_text(PLANTED["Telegram ID"])
    assert scan(repository).returncode == 1


def test_omitting_tracked_file_with_a_symlink_does_not_hide_its_target_name(repository):
    path = repository / "desktop"
    path.symlink_to(PLANTED["private IPv4"])
    subprocess.run(["git", "add", "--", path.name], cwd=repository, check=True)
    result = scan(repository)
    assert result.returncode == 1
    assert "desktop:1: private IPv4" in result.stdout


def test_allowlisted_handles_are_limited_to_test_fixtures(repository):
    handle = "@" + "newbie"
    track(repository, "tests/test_sample.py", f"USER = {handle!r}")
    assert scan(repository).returncode == 0
    track(repository, "config.toml", f"username = {handle!r}")
    assert scan(repository).returncode == 1


def test_finding_in_tracked_filename_redacts_that_name(repository):
    path = track(repository, PLANTED["Telegram ID"] + ".txt", "plain")
    result = scan(repository)
    assert result.returncode == 1
    assert "[redacted tracked path]:0: Telegram ID" in result.stdout
    assert path.name not in result.stdout
