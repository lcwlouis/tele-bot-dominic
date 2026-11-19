"""Check tracked source and docs for likely personal data without printing the values."""

from __future__ import annotations

import ipaddress
import io
import os
import re
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Iterator
from pathlib import Path

TELEGRAM_ID = re.compile(r"(?<!\w)(?:-100[0-9]{10}|-?[0-9]{8,12})(?!\w)")
HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}(?!\w)")
IPV4 = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?!\w)")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,63}(?!\w)")
EXAMPLE_EMAIL_DOMAIN = re.compile(r"(?:[a-z0-9-]+\.)*example\.[a-z0-9-]+")
DOC_NETWORKS = tuple(map(ipaddress.IPv4Network, ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")))
CARRIER_NAT = ipaddress.IPv4Network("100.64.0.0/10")
VENDORED = ("dom/web/static/novnc/", "dom/web/static/fonts/")
EXCLUDED = {"uv.lock", "dom/web/static/htmx.min.js"}
POLICY_PATH = Path("scripts/check_public.toml")
CSS_RULES = {"charset", "container", "import", "keyframes", "layer", "media", "namespace", "starting", "supports"}


def decorators(content: str) -> set[tuple[int, int]]:
    """Identify actual decorator tokens in Python without ignoring handles in strings."""
    found = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(content).readline):
            if token.type == tokenize.OP and token.string == "@" and not token.line[:token.start[1]].strip():
                found.add(token.start)
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass
    return found


def is_private_address(value: str, networks: set[str]) -> bool:
    try:
        address = ipaddress.IPv4Address(value.partition("/")[0])
    except ipaddress.AddressValueError:
        return False
    return (
        (address.is_private or address in CARRIER_NAT)
        and not (address.is_loopback or address.is_unspecified)
        and value not in networks
        and not any(address in network for network in DOC_NETWORKS)
    )


def categories(
    line: str, path: Path, line_number: int, python_decorators: set[tuple[int, int]], allow: dict[str, set[str]],
) -> Iterator[str]:
    # Mask emails before the other checks, including numeric GitHub noreply prefixes.
    for match in EMAIL.finditer(line):
        value = match.group().lower()
        domain = value.rsplit("@", 1)[1]
        if (
            domain != "users.noreply.github.com"
            and not EXAMPLE_EMAIL_DOMAIN.fullmatch(domain)
            and value not in allow["emails"]
        ):
            yield "email address"
    line = EMAIL.sub(lambda match: " " * len(match.group()), line)
    fixtures = path.parts[0] == "tests" or path == POLICY_PATH
    if any(
        not (fixtures and match.group() in allow["test_telegram_ids"])
        for match in TELEGRAM_ID.finditer(line)
    ):
        yield "Telegram ID"
    for match in HANDLE.finditer(line):
        handle = match.group().lower()
        if (line_number, match.start()) in python_decorators:
            continue
        if path.suffix in (".css", ".html") and handle[1:] in CSS_RULES:
            continue
        if fixtures and handle in allow["test_handles"]:
            continue
        yield "Telegram handle"
    if any(is_private_address(match.group(), allow["generic_cidrs"]) for match in IPV4.finditer(line)):
        yield "private IPv4"


def read_tracked(path: Path) -> str | None:
    if path.is_symlink():
        return os.readlink(path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    root = Path(os.fsdecode(subprocess.check_output(["git", "rev-parse", "--show-toplevel"])).rstrip("\n"))
    index = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=root)
    with Path(__file__).with_suffix(".toml").open("rb") as file:
        allow = {key: set(values) for key, values in tomllib.load(file).items()}
    findings = scanned = 0
    for name in sorted(set(os.fsdecode(index).split("\0")) - {""}):
        if name in EXCLUDED or name.startswith(VENDORED):
            continue
        relative = Path(name)
        path_categories = sorted(set(categories(name, relative, 0, set(), allow)))
        for category in path_categories:
            print(f"[redacted tracked path]:0: {category}")
            findings += 1
        label = "[redacted tracked path]" if path_categories else name
        content = read_tracked(root / relative)
        if content is None:
            continue
        scanned += 1
        python_decorators = decorators(content) if relative.suffix == ".py" else set()
        for number, line in enumerate(content.splitlines(), start=1):
            for category in sorted(set(categories(line, relative, number, python_decorators, allow))):
                print(f"{label}:{number}: {category}")
                findings += 1
    if findings:
        print(f"Public data check failed: {findings} finding(s) in {scanned} scanned tracked text files.")
        return 1
    print(f"Public data check passed ({scanned} tracked text files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
