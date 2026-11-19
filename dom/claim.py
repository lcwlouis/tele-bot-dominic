"""One-time owner claim: `dom init` writes a code, the owner sends `/claim <code>` to the bot."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from dom.config import Config, save_owner

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I lookalikes
CODE_LENGTH = 8


def write_claim_code(home: Path) -> str:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    fd = os.open(home / ".claim", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(code)
    return code


def try_claim(home: Path, config: Config, sender_id: int, chat_type: str, code: str) -> bool:
    """Make `sender_id` the owner. Only while no owner is set, in a private chat, with the right code."""
    if config.owner_id is not None or chat_type != "private" or not code:
        return False
    try:
        expected = (home / ".claim").read_text().strip()
    except FileNotFoundError:
        return False
    if not expected or not hmac.compare_digest(code.encode(), expected.encode()):
        return False
    try:
        save_owner(home, sender_id)
    except (OSError, ValueError):  # not saved: keep the code so the claim can be retried
        return False
    (home / ".claim").unlink()
    return True
