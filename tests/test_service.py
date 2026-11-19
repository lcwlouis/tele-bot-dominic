import subprocess

import pytest

from dom import service
from dom.cli import main
from dom.home import init_home


@pytest.fixture
def install(tmp_path, monkeypatch):
    account = tmp_path / "account"
    root = account / "dom"
    release = root / "releases" / "test-release"
    (release / "dom").mkdir(parents=True)
    (release / "dom" / "__main__.py").touch()
    app = root / "app"
    app.symlink_to(release, target_is_directory=True)
    work = root / 'instance space $HOME %h'
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "loginctl":
            return subprocess.CompletedProcess(args, 0, "no\n", "")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.chdir(app)
    monkeypatch.setenv("PWD", str(app))
    monkeypatch.setenv("HOME", str(account))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(account / "xdg-config"))
    monkeypatch.setattr(service.sys, "executable", str(release / ".venv/bin/python"))
    monkeypatch.setattr(service.subprocess, "run", run)
    return work, app, account / "xdg-config/systemd/user", calls


def test_install_dry_run_prints_units_and_commands_without_writes(install, capsys):
    home, app, units, calls = install
    assert main(["install-service", str(home)]) == 0
    out = capsys.readouterr().out
    assert f"WorkingDirectory={app}" in out
    assert f'ExecStart={app}/.venv/bin/python -m dom run "{home.parent}/instance space $$HOME %%h"' in out
    assert f'ExecStart={app}/.venv/bin/python -m dom backup "{home.parent}/instance space $$HOME %%h" --keep 7' in out
    for line in (
        "MemoryHigh=500M", "MemoryMax=700M", "Restart=on-failure", "RestartSec=15", "Nice=5",
        "IOSchedulingClass=idle", "OnCalendar=*-*-* 04:00:00", "Persistent=true",
        "systemctl --user daemon-reload", "systemctl --user enable --now dom.service dom-backup.timer",
        "loginctl enable-linger",
    ):
        assert line in out
    assert not units.exists() and not calls
    assert "/releases/" not in out
    assert f"--- {units / 'dom.service'} ---" in out
    assert "After=network-online.target" not in out
    assert "systemctl --user restart" not in out


def test_install_writes_user_units_enables_service_and_timer_and_reports_linger(install, capsys):
    home, app, units, calls = install
    assert main(["install-service", str(home), "--yes"]) == 0
    assert {path.name for path in units.iterdir()} == {"dom.service", "dom-backup.service", "dom-backup.timer"}
    assert f"WorkingDirectory={app}" in (units / "dom.service").read_text()
    assert "WantedBy=default.target" in (units / "dom.service").read_text()
    assert "Type=oneshot" in (units / "dom-backup.service").read_text()
    assert "WantedBy=timers.target" in (units / "dom-backup.timer").read_text()
    assert calls[:2] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "dom.service", "dom-backup.timer"],
    ]
    assert calls[2][:2] == ["loginctl", "show-user"]
    assert len(calls) == 3 and "loginctl enable-linger" in capsys.readouterr().out


def test_units_with_matching_content_are_reused(install):
    home, _, units, _ = install
    argv = ["install-service", str(home), "--yes"]
    assert main(argv) == 0
    before = {path.name: path.stat().st_ino for path in units.iterdir()}
    assert main(argv) == 0
    assert {path.name: path.stat().st_ino for path in units.iterdir()} == before


@pytest.mark.parametrize("flags", [[], ["--yes"], ["--force"]])
def test_existing_different_unit_requires_force_and_yes(install, capsys, flags):
    home, _, units, calls = install
    units.mkdir(parents=True)
    path = units / "dom-backup.timer"
    path.write_text("operator customization")
    result = main(["install-service", str(home), *flags])
    assert result == (0 if flags == ["--force"] else 1)
    assert path.read_text() == "operator customization"
    assert list(units.iterdir()) == [path] and not calls
    assert "--force" in capsys.readouterr().out


def test_force_dry_run_prints_conditional_restart(install, capsys):
    home, _, units, calls = install
    assert main(["install-service", str(home), "--force"]) == 0
    out = capsys.readouterr().out
    assert "systemctl --user is-active --quiet dom.service" in out
    assert "systemctl --user restart dom.service" in out
    assert not units.exists() and not calls


@pytest.mark.parametrize("running", [True, False])
def test_force_replaces_differing_unit_and_restarts_only_if_running(install, monkeypatch, running):
    home, _, units, calls = install
    units.mkdir(parents=True)
    path = units / "dom-backup.timer"
    path.write_text("operator customization")
    is_active = ["systemctl", "--user", "is-active", "--quiet", "dom.service"]
    run = service.subprocess.run

    def run_with_status(args, **kwargs):
        result = run(args, **kwargs)
        if args == is_active and not running:
            result.returncode = 3
        return result

    monkeypatch.setattr(service.subprocess, "run", run_with_status)
    assert main(["install-service", str(home), "--yes", "--force"]) == 0
    assert "Persistent=true" in path.read_text()
    expected = [
        ["systemctl", "--user", "daemon-reload"],
        is_active,
    ]
    if running:
        expected.append(["systemctl", "--user", "restart", "dom.service"])
    expected.append(["systemctl", "--user", "enable", "--now", "dom.service", "dom-backup.timer"])
    assert calls[:-1] == expected
    assert calls[-1][:2] == ["loginctl", "show-user"]


@pytest.mark.parametrize("xdg_home", [None, "", "relative"])
def test_unset_or_relative_xdg_config_home_uses_default(install, monkeypatch, capsys, xdg_home):
    home, _, units, calls = install
    if xdg_home is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg_home)
    assert main(["install-service", str(home)]) == 0
    default_unit = units.parents[2] / ".config/systemd/user/dom.service"
    assert f"--- {default_unit} ---" in capsys.readouterr().out
    assert not calls


def test_existing_linger_needs_no_command(install, monkeypatch, capsys):
    home, _, _, _ = install
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "yes\n"))
    assert main(["install-service", str(home), "--yes"]) == 0
    assert "enable-linger" not in capsys.readouterr().out


def test_desktop_setup_requires_yes_to_apply(tmp_path, monkeypatch, capsys):
    home = tmp_path / "instance"
    init_home(home, "UTC")
    calls = []
    monkeypatch.setattr("dom.cli.desktop_network.setup", lambda config: calls.append(config.home))
    assert main(["desktop-setup", str(home)]) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "dom-desktop" in out and "DOM-DESKTOP" in out and "--yes" in out
    assert main(["desktop-setup", str(home), "--yes"]) == 0
    assert calls == [home]
