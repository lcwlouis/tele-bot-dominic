#!/bin/bash
set -euo pipefail
umask 077
studio=$(cd "$(dirname "$0")/.." && pwd -P)
config=${1:?Usage: install.sh CONFIG}
[[ $# -eq 1 && -f "$config" ]]
[[ $(uname -s) = Darwin && $(uname -m) = arm64 ]]
cd "$studio"
uv sync --locked --no-dev --python 3.12
python="$studio/.venv/bin/python"
PYTHONPATH="$studio" "$python" - "$config" <<'PY'
import os
from pathlib import Path
import plistlib
import sys
import tempfile

from studio_service.config import load, read_token

config_path = Path(sys.argv[1]).resolve()
config = load(config_path)
read_token(config.token_file)
studio = Path.cwd()
template = plistlib.loads((studio / "scripts/launch-agent.plist").read_bytes())
template["ProgramArguments"] = [sys.executable, "-m", "studio_service", str(config_path)]
template["WorkingDirectory"] = str(studio)
log = config_path.parent / "studio.log"
template["StandardOutPath"] = template["StandardErrorPath"] = str(log)
target = Path.home() / "Library/LaunchAgents/org.dom.studio.plist"
target.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
    plistlib.dump(template, stream)
os.replace(stream.name, target)
PY
domain="gui/$(id -u)"
if launchctl print "$domain/org.dom.studio" >/dev/null 2>&1; then
    launchctl bootout "$domain/org.dom.studio"
    deadline=$((SECONDS + 15))
    while launchctl print "$domain/org.dom.studio" >/dev/null 2>&1; do
        if (( SECONDS >= deadline )); then
            echo "Timed out waiting for org.dom.studio to unload" >&2
            exit 1
        fi
        sleep 0.2
    done
fi
launchctl bootstrap "$domain" "$HOME/Library/LaunchAgents/org.dom.studio.plist"
