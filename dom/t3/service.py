"""Install a version-pinned T3 Code systemd user service."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from dom import service
from dom.config import T3Config

BASE_DIR = ".t3-dom"
UNIT = "t3.service"
ENABLE = ["systemctl", "--user", "enable", "--now", UNIT]


def _package_entry(version: str) -> Path:
    prefix = Path.home() / ".local" / "t3" / version
    package = (prefix / "node_modules/t3").resolve()
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        relative_entry = metadata["bin"]["t3"]
        entry = (package / relative_entry).resolve(strict=True)
        if (
            metadata["name"] == "t3" and metadata["version"] == version
            and entry.is_relative_to(package) and entry.is_file()
        ):
            return entry
    except (OSError, ValueError, TypeError, KeyError):
        pass
    raise ValueError(f"Install the pinned t3@{version} npm package under {prefix} first.")


def render_unit(node: Path, entry: Path, port: int) -> str:
    executable = service._word(service._literal_path(node))
    script = service._word(service._literal_path(entry).replace("$", "$$"))
    return f"""[Unit]
Description=T3 Code gateway for Dom

[Service]
Environment=T3CODE_HOME=%h/{BASE_DIR}
Environment=T3CODE_AUTO_BOOTSTRAP_PROJECT_FROM_CWD=false
ExecStart={executable} {script} serve --no-browser --base-dir %h/{BASE_DIR} --port {port} --host 127.0.0.1
UMask=0077
MemoryHigh=450M
MemoryMax=600M
Restart=on-failure
RestartSec=15
KillMode=control-group

[Install]
WantedBy=default.target
"""


def install(config: T3Config, *, yes: bool) -> int:
    node = Path(config.node).expanduser()
    if not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError("Set [t3] node to the absolute path of an executable Node.js binary.")
    content = render_unit(node, _package_entry(config.version), config.port)
    path = service.user_unit_folder() / UNIT
    if not yes:
        print(f"--- {path} ---\n{content}", end="")
    print(shlex.join(service.RELOAD), flush=True)
    print(shlex.join(ENABLE), flush=True)
    if not yes:
        print("Dry run. Add --yes to install the unit.")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    restart = False
    if path.is_symlink() or not path.is_file() or path.read_bytes() != content.encode("utf-8"):
        restart = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", UNIT], check=False, timeout=60,
        ).returncode == 0
        service._write_unit(path, content)
    subprocess.run(service.RELOAD, check=True, timeout=60)
    subprocess.run(ENABLE, check=True, timeout=60)
    if restart:
        subprocess.run(["systemctl", "--user", "restart", UNIT], check=True, timeout=60)
    return 0
