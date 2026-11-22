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


@pytest.mark.parametrize(("line", "category"), [
    ("sk-" + "A" * 24, "API key"),
    ("sk-or-v1-" + "Ab_" * 10, "API key"),
    ("gsk_" + "B" * 24, "API key"),
    ("hf_" + "C" * 24, "API key"),
    ("abcd_" + "D" * 20, "API key"),
    ("Authorization: " + "Bearer " + "token.with-a_test~signature", "Bearer token"),
    ("123456" + ":" + "E_-" * 12, "Telegram bot token"),
    ("https://t.me/" + "example_handle", "Telegram link"),
    ("https://t.me/" + "+fixture_invite", "Telegram link"),
    ("https://t.me/" + "addstickers/fixture_set", "Telegram link"),
    ("[fd12:" + ":1]:443", "private IPv6"),
    ("FC00:" + ":a:0/48", "private IPv6"),
    ("fe80:" + ":1234%en0", "private IPv6"),
    ("fixture." + "ts.net", "private hostname"),
    ("fixture." + "local", "private hostname"),
    ("fixture." + "lan", "private hostname"),
    ("fixture." + "internal", "private hostname"),
    ("fixture." + "home.arpa", "private hostname"),
    ("chat_id = -" + "1234567", "Telegram ID"),
    ('"user_id": "' + '1234567"', "Telegram ID"),
    ("owner_id = " + "1234567", "Telegram ID"),
])
def test_detects_additional_personal_data_without_echoing_it(repository, line, category):
    track(repository, "settings.md", line)
    result = scan(repository)
    assert result.returncode == 1
    assert f"settings.md:1: {category}" in result.stdout
    assert line not in result.stdout


def test_seven_digit_numbers_without_telegram_id_fields_are_allowed(repository):
    track(repository, "settings.md", "count = 1234567\nchat_id_limit = 1234567\n")
    assert scan(repository).returncode == 0


def test_the_bare_sticker_share_prefix_is_allowed(repository):
    prefix = "https://t" + ".me/addstickers/"
    track(repository, "page.html", f'<a href="{prefix}{{{{ name }}}}">\nLINK = "{prefix}"\n')
    assert scan(repository).returncode == 0


def test_allowlisted_bearer_fixture_is_limited_to_tests(repository):
    header = "Bearer " + "fake-key"
    track(repository, "tests/test_sample.py", f"AUTH = {header!r}")
    assert scan(repository).returncode == 0
    track(repository, "config.toml", f"authorization = {header!r}")
    assert scan(repository).returncode == 1


def test_seccomp_constants_are_allowed_only_on_value_lines_in_the_profile(repository):
    path = "dom/desktop/seccomp-chromium.json"
    mask = str(0x0e020000)
    personality_query = str(0xffffffff)
    values = f'    "value": {mask},\n    "value": {personality_query},\n'
    profile = track(repository, path, values)
    assert scan(repository).returncode == 0

    profile.write_text(values + f'    "value": {PLANTED["Telegram ID"]},\n    "owner_id": {mask},\n')
    track(repository, "other.json", values)
    result = scan(repository)
    assert result.returncode == 1
    assert f"{path}:3: Telegram ID" in result.stdout
    assert f"{path}:4: Telegram ID" in result.stdout
    assert "other.json:1: Telegram ID" in result.stdout
