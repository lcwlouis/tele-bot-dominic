"""Render and install Dom's systemd user units."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

RELOAD = ["systemctl", "--user", "daemon-reload"]
IS_ACTIVE = ["systemctl", "--user", "is-active", "--quiet", "dom.service"]
RESTART = ["systemctl", "--user", "restart", "dom.service"]
ENABLE = ["systemctl", "--user", "enable", "--now", "dom.service", "dom-backup.timer"]


def _app_and_python() -> tuple[Path, Path]:
    physical = Path.cwd()
    if not (physical / "dom" / "__main__.py").is_file():
        raise ValueError("Run install-service from the Dom app directory.")
    current = Path(os.environ.get("PWD", ""))
    app = current if current.is_absolute() and current.resolve() == physical else physical
    python = Path(sys.executable)
    # Keep an app/.venv path valid when the app symlink points at the next release.
    if python.is_relative_to(physical):
        python = app / python.relative_to(physical)
    return app, python


def _literal_path(path: Path) -> str:
    value = str(path)
    if not path.is_absolute() or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("Unit paths must be absolute and contain no control characters.")
    return value.replace("%", "%%")


def _word(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./%+-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_units(app: Path, python: Path, home: Path) -> dict[str, str]:
    working_dir = _literal_path(app)
    # WorkingDirectory is read as a whole line, without ExecStart's quoting rules.
    if working_dir[-1].isspace() or working_dir.endswith("\\"):
        raise ValueError("The app directory cannot end in whitespace or a backslash.")
    executable = _word(_literal_path(python))
    home_arg = _word(_literal_path(home).replace("$", "$$"))
    return {
        "dom.service": f"""[Unit]
Description=Dom Telegram bot

[Service]
WorkingDirectory={working_dir}
ExecStart={executable} -m dom run {home_arg}
MemoryHigh=500M
MemoryMax=700M
Restart=on-failure
RestartSec=15
Nice=5

[Install]
WantedBy=default.target
""",
        "dom-backup.service": f"""[Unit]
Description=Back up the Dom instance

[Service]
Type=oneshot
WorkingDirectory={working_dir}
ExecStart={executable} -m dom backup {home_arg} --keep 7
IOSchedulingClass=idle
""",
        "dom-backup.timer": """[Unit]
Description=Back up the Dom instance nightly

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
""",
    }


def _write_unit(path: Path, content: str) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        pending = Path(temp.name)
        try:
            temp.write(content)
            temp.close()
            pending.chmod(0o644)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def install(home: Path, *, yes: bool, force: bool) -> int:
    app, python = _app_and_python()
    units = render_units(app, python, home.resolve())
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    folder = config_home / "systemd" / "user"
    changes = {}
    conflicts = []
    for name, content in units.items():
        path = folder / name
        if path.is_symlink():
            changes[path] = content
            conflicts.append(name)
        elif not path.exists():
            changes[path] = content
        elif not path.is_file():
            raise ValueError(f"{path} is not a regular file. Move it aside before installing.")
        elif path.read_bytes() != content.encode("utf-8"):
            changes[path] = content
            conflicts.append(name)
    if conflicts and not force:
        raise ValueError(f"Existing units differ: {', '.join(conflicts)}. Review them before using --force.")

    if conflicts:
        print(f"Existing units to replace with --force: {', '.join(conflicts)}.")

    uid = str(os.getuid())
    show_linger = ["loginctl", "show-user", uid, "--property=Linger", "--value"]
    enable_linger = shlex.join(["loginctl", "enable-linger", uid])
    for name, content in units.items():
        path = folder / name
        if yes:
            print(f"{'Writing' if path in changes else 'Keeping'} {path}")
        else:
            print(f"--- {path} ---\n{content}", end="")
    print(shlex.join(RELOAD), flush=True)
    if force:
        print(f"if {shlex.join(IS_ACTIVE)}; then {shlex.join(RESTART)}; fi", flush=True)
    print(shlex.join(ENABLE), flush=True)
    if not yes:
        print(shlex.join(show_linger))
        print(f"If linger is off, run: {enable_linger}")
        print("Dry run. Add --yes to install the units.")
        return 0

    folder.mkdir(parents=True, exist_ok=True)
    for path, content in changes.items():
        _write_unit(path, content)
    subprocess.run(RELOAD, check=True, timeout=60)
    if force and subprocess.run(IS_ACTIVE, check=False, timeout=60).returncode == 0:
        subprocess.run(RESTART, check=True, timeout=60)
    subprocess.run(ENABLE, check=True, timeout=60)
    try:
        linger = subprocess.run(show_linger, capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        linger = ""
    if linger == "no":
        print(f"Linger is off. To keep Dom running after logout, run: {enable_linger}")
    elif linger != "yes":
        print(f"Could not check linger. To keep Dom running after logout, run: {enable_linger}")
    return 0
