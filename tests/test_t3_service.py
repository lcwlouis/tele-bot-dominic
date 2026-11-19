import json
import subprocess

import pytest

from dom.cli import main
from dom.config import load_config, starter_config
from dom.home import init_home
from dom.t3 import service


@pytest.fixture
def installation(tmp_path, monkeypatch):
    account = tmp_path / "account space"
    home = tmp_path / "instance"
    init_home(home, "UTC")
    config_path = home / "config.toml"
    config_path.write_text(config_path.read_text().replace(
        'version = "0.1.10"', 'version = "1.2.3"',
    ).replace("port = 39731", "port = 39800"))
    node = account / ".local/node/bin/node"
    node.parent.mkdir(parents=True)
    node.touch(mode=0o755)
    package = account / ".local/t3/1.2.3/node_modules/t3"
    entry = package / "bin/t3.mjs"
    entry.parent.mkdir(parents=True)
    entry.touch()
    (package / "package.json").write_text(json.dumps({
        "name": "t3", "version": "1.2.3", "bin": {"t3": "bin/t3.mjs"},
    }))
    units = account / "config/systemd/user"
    calls = []

    def run(args, *, check, timeout):
        assert check and timeout == 60
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setenv("HOME", str(account))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(account / "config"))
    monkeypatch.setattr(service.subprocess, "run", run)
    return home, node, entry, units, calls


def test_t3_config_defaults_are_off(tmp_path):
    from dom.config import Config

    assert Config(home=tmp_path).t3.model_dump() == starter_config()["t3"] == {
        "enabled": False,
        "port": 39731,
        "base_url": "http://127.0.0.1:39731",
        "version": "0.1.10",
        "node": "~/.local/node/bin/node",
    }


def test_t3_service_dry_run_renders_pinned_unit_without_writes(installation, capsys):
    home, node, entry, units, calls = installation
    assert load_config(home).t3.version == "1.2.3"
    assert main(["t3-service", str(home)]) == 0
    out = capsys.readouterr().out
    assert f"--- {units / 't3.service'} ---" in out
    assert (
        f'ExecStart="{node}" "{entry}" serve --no-browser '
        "--base-dir %h/.t3-dom --port 39800 --host 127.0.0.1"
    ) in out
    for line in (
        "MemoryHigh=450M", "MemoryMax=600M", "Restart=on-failure",
        "KillMode=control-group", "WantedBy=default.target",
        "systemctl --user daemon-reload", "systemctl --user enable --now t3.service",
    ):
        assert line in out
    assert not units.exists() and not calls


def test_t3_service_writes_and_enables_only_with_yes(installation):
    home, node, entry, units, calls = installation
    assert main(["t3-service", str(home), "--yes"]) == 0
    path = units / "t3.service"
    assert path.read_text() == service.render_unit(node, entry, 39800)
    assert path.stat().st_mode & 0o777 == 0o644
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "t3.service"],
    ]


def test_t3_service_rejects_package_with_a_different_version(installation, capsys):
    home, _, entry, units, calls = installation
    metadata = entry.parents[1] / "package.json"
    metadata.write_text(metadata.read_text().replace("1.2.3", "1.2.4"))
    assert main(["t3-service", str(home), "--yes"]) == 1
    assert "t3@1.2.3" in capsys.readouterr().out
    assert not units.exists() and not calls
