"""Dom's version.

`git archive` fills the two placeholders (see .gitattributes), so a release folder deployed from an
archive knows its own tag. Wheels and sdists get this file rewritten by setup.py. A plain checkout
asks git instead.
"""

import subprocess
from pathlib import Path

_DESCRIBE = "$Format:%(describe:tags=true)$"
_HASH = "$Format:%h$"


def resolve(describe: str, short_hash: str, root: Path) -> str:
    if not describe.startswith("$Format"):  # substituted by git archive; empty when no tag is reachable
        return describe or short_hash or "unknown"
    if not (root / ".git").exists():
        return "unknown"
    try:
        version = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return version or "unknown"


VERSION = resolve(_DESCRIBE, _HASH, Path(__file__).resolve().parent.parent)
