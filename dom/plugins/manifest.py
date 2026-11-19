"""Validate and capture the exact plugin files presented for approval."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import tokenize
import tomllib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from dom.skills import _visible

MAX_BYTES = 64 * 1024
MAX_FILES = 10
MAX_PARAMETERS = 4 * 1024
SCOPES = ("owner_dm", "friend_dm", "group")
_NAME = re.compile(r"[a-z][a-z0-9_]{1,31}")
_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]+")


def validate_name(name: object) -> None:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Plugin name must match [a-z][a-z0-9_]{1,31}.")


def _label(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > maximum
        or not value.isprintable() or not _visible(value)
    ):
        raise ValueError(f"{field} must be printable text of up to {maximum} characters.")
    return value


def _integer(data: dict, field: str, default: int, lower: int, upper: int) -> int:
    value = data.get(field, default)
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{field} must be an integer from {lower} to {upper}.")
    return value


@dataclass(frozen=True)
class ToolManifest:
    name: str
    description: str
    parameters: dict[str, Any]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    name: str
    description: str
    version: str
    tools: tuple[ToolManifest, ...]
    network: bool
    timeout_seconds: int
    memory_mb: int

    @classmethod
    def parse(cls, text: str) -> Manifest:
        if not _visible(text.replace("\r\n", "\n")):
            raise ValueError("Manifest contains invisible or control characters.")
        data = tomllib.loads(text)
        name = data.get("name")
        validate_name(name)
        description = _label(data.get("description"), "Description", 200)
        version = _label(data.get("version"), "Version", MAX_BYTES)
        tools = data.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError("Manifest must declare at least one tool.")
        parsed = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("Each tool must be a TOML table.")
            tool_name = tool.get("name")
            if (
                not isinstance(tool_name, str) or not _TOOL_NAME.fullmatch(tool_name)
                or len(f"plugin_{name}_{tool_name}") > 64
                or any(previous.name == tool_name for previous in parsed)
            ):
                raise ValueError("Invalid, duplicate or too long plugin tool name.")
            params = tool.get("parameters")
            if not isinstance(params, dict) or params.get("type") != "object":
                raise ValueError("Tool parameters must be a JSON schema of type object.")
            try:
                encoded = json.dumps(params, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError, RecursionError):
                raise ValueError("Tool parameters must be a JSON schema object.") from None
            if len(encoded.encode("utf-8")) > MAX_PARAMETERS or not _visible(encoded):
                raise ValueError("Tool parameters must be visible JSON of at most 4 KB.")
            scopes = tool.get("scopes", ["owner_dm"])
            if not isinstance(scopes, list) or any(scope not in SCOPES for scope in scopes):
                raise ValueError("Tool scopes must be a list of supported chat scopes.")
            parsed.append(ToolManifest(tool_name, _label(tool.get("description"), "Tool description", 200),
                                       params, tuple(dict.fromkeys(scopes))))
        network = data.get("network", False)
        if type(network) is not bool:
            raise ValueError("network must be a boolean.")
        if any(set(tool) & {"network", "timeout_seconds", "memory_mb"} for tool in tools):
            raise ValueError("Set network, timeout_seconds and memory_mb before the tools tables.")
        return cls(name, description, version, tuple(parsed), network,
                   _integer(data, "timeout_seconds", 10, 1, 30), _integer(data, "memory_mb", 64, 16, 256))


@dataclass(frozen=True)
class Plugin:
    manifest: Manifest
    files: dict[str, bytes]
    hash: str

    def tool_name(self, tool: ToolManifest) -> str:
        return f"plugin_{self.manifest.name}_{tool.name}"


def validate_plugin(name: str, files: dict[str, bytes]) -> Plugin:
    validate_name(name)
    if not {"plugin.toml", "main.py"} <= files.keys():
        raise ValueError("Plugins require plugin.toml and main.py.")
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError("Plugins are limited to 10 files and 64 KB in total.")
    digest = sha256()
    for relative, contents in sorted(files.items()):
        parts = relative.split("/")
        if (
            not _visible(relative) or not relative.isprintable() or "\\" in relative
            or any(part in ("", ".", "..", "_dom") for part in parts)
            or (relative != "plugin.toml" and not relative.endswith(".py"))
        ):
            raise ValueError("Plugins may contain only plugin.toml and Python source files.")
        if relative.endswith(".py"):
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(contents).readline)
                visible = _visible(contents.decode("utf-8").replace("\r\n", "\n"))
            except (UnicodeError, SyntaxError):
                raise ValueError("Python source must be UTF-8.") from None
            if encoding != "utf-8" or not visible:
                raise ValueError("Python source must be UTF-8 without invisible or control characters.")
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big") + path_bytes)
        digest.update(len(contents).to_bytes(4, "big") + contents)
    manifest = Manifest.parse(files["plugin.toml"].decode("utf-8"))
    if manifest.name != name:
        raise ValueError("Manifest name must match its plugin folder.")
    return Plugin(manifest, files, digest.hexdigest())


def read_plugin(folder: Path) -> Plugin:
    """Open only regular, non-linked files and bound reads before hashing or parsing."""
    validate_name(folder.name)
    files: dict[str, bytes] = {}
    remaining = MAX_BYTES
    entries = 0

    def walk(directory: int, prefix: str = "") -> None:
        nonlocal remaining, entries
        with os.scandir(directory) as listing:
            for entry in listing:
                # Every useful directory is an ancestor of a permitted file. Bound traversal too.
                entries += 1
                if entries > MAX_BYTES:
                    raise ValueError("Plugin folder contains too many entries.")
                relative = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        walk(child, relative + "/")
                    finally:
                        os.close(child)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError("Plugins require regular files without links.")
                fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Plugins require regular files without links.")
                    if len(files) >= MAX_FILES or info.st_size > remaining:
                        raise ValueError("Plugins are limited to 10 files and 64 KB in total.")
                    files[relative] = file.read(remaining + 1)
                remaining -= len(files[relative])
                if remaining < 0:
                    raise ValueError("Plugins are limited to 64 KB in total.")

    if folder.is_symlink():
        raise ValueError("Plugin folders cannot be links.")
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd)
    finally:
        os.close(fd)
    return validate_plugin(folder.name, files)
