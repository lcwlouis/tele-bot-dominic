"""Stage disk plugins and expose tools only for their currently approved bytes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory

from dom.home import Home
from dom.plugins.manifest import Plugin, ToolManifest, read_plugin, validate_name
from dom.store import Store

log = logging.getLogger(__name__)
_HASH = re.compile(r"[0-9a-f]{64}")


class PluginRegistry:
    def __init__(self, home: Home, store: Store):
        self.home = home.scoped(allow_private=False)
        self.store = store
        self.lock = asyncio.Lock()

    def _read_all(self) -> dict[str, Plugin]:
        found = {}
        try:
            folder = self.home._regular_path("plugins")
            if not folder.exists():
                return found
            paths = sorted(folder.iterdir())
        except (OSError, ValueError, RuntimeError) as error:
            log.warning("Could not scan plugins: %s", type(error).__name__)
            return found
        for path in paths:
            if not path.is_dir() and not path.is_symlink():
                continue
            try:
                plugin = read_plugin(path)
                found[plugin.manifest.name] = plugin
            except (OSError, ValueError, RecursionError) as error:
                log.warning("Skipping invalid plugin %r: %s", path.name, type(error).__name__)
        return found

    def _write(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        parent = self.home._regular_path("plugins")
        target = self.home._regular_path(f"plugins/{name}")
        parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError("The plugin path must be a regular directory.")
        with TemporaryDirectory(prefix=".draft-", dir=parent) as temporary:
            staged = Path(temporary) / "draft"
            for relative, contents in plugin.files.items():
                path = staged / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            previous = Path(temporary) / "previous"
            if target.exists():
                target.rename(previous)
            try:
                staged.rename(target)
            except OSError:
                if previous.exists():
                    previous.rename(target)
                raise

    async def draft(self, plugin: Plugin) -> None:
        async with self.lock:
            name = plugin.manifest.name
            rows = await self.store.list_plugins()
            if any(row["name"] == name and row["status"] == "approved" and row["hash"] == plugin.hash for row in rows):
                raise ValueError("This plugin version is already approved.")
            await asyncio.to_thread(self._write, plugin)
            await self.store.stage_plugins([(name, plugin.hash, json.dumps(asdict(plugin.manifest)))])
            await self.store.set_plugin_status(name, plugin.hash, "pending")

    async def _scan(self) -> dict[str, Plugin]:
        found = await asyncio.to_thread(self._read_all)
        await self.store.stage_plugins([
            (name, plugin.hash, json.dumps(asdict(plugin.manifest))) for name, plugin in found.items()
        ])
        return found

    async def scan(self) -> dict[str, Plugin]:
        async with self.lock:
            return await self._scan()

    async def listing(self) -> list[dict]:
        async with self.lock:
            found = await self._scan()
            return [
                {**row, "available": row["name"] in found and found[row["name"]].hash == row["hash"]}
                for row in await self.store.list_plugins()
            ]

    async def _approved(self) -> list[Plugin]:
        found = await self._scan()
        return [
            found[row["name"]] for row in await self.store.list_plugins()
            if row["status"] == "approved" and row["name"] in found
            and found[row["name"]].hash == row["approved_hash"]
        ]

    async def approved(self) -> list[Plugin]:
        async with self.lock:
            return await self._approved()

    async def review(self, name: str, digest: str) -> dict | None:
        validate_name(name)
        if not _HASH.fullmatch(digest):
            return None
        async with self.lock:
            plugin = (await self._scan()).get(name)
            if plugin is None or plugin.hash != digest:
                return None
            row = next(row for row in await self.store.list_plugins() if row["name"] == name)
            old = json.loads(await self.store.kv_get(f"plugin-files:{name}") or "{}")
            current = {path: content.decode("utf-8") for path, content in plugin.files.items()}
            documents = []
            for path in sorted(old.keys() | current.keys()):
                previous, text = old.get(path, ""), current.get(path, "")
                changes = list(unified_diff(previous.splitlines(), text.splitlines(), lineterm=""))[2:] if old else []
                documents.append({"path": path, "text": text, "changes": changes, "removed": path not in current})
            return {"plugin": plugin, "status": row["status"], "documents": documents}

    async def mark_reviewed(self, name: str, digest: str) -> None:
        key = f"plugin-reviewed:{name}:{digest}"
        async with self.lock:
            if await self.store.kv_get(key) is None:
                await self.store.kv_set(key, "yes")
                await self.store.record_owner_event(f"Viewed plugin {name} ({digest[:12]}) on the dashboard")

    async def decide(self, name: str, digest: str, decision: str) -> str:
        async with self.lock:
            found = await self._scan()
            if decision != "disable" and (name not in found or found[name].hash != digest):
                return "Plugin changed. Open the dashboard to review the current version."
            snapshot = None
            if decision == "approve":
                if not await self.store.kv_get(f"plugin-reviewed:{name}:{digest}"):
                    return "Open the dashboard to review this plugin before approving."
                snapshot = {path: content.decode("utf-8") for path, content in found[name].files.items()}
            if not await self.store.decide_plugin(name, digest, decision, snapshot):
                return "already handled"
            label = {"approve": "Approved", "reject": "Rejected", "disable": "Disabled"}[decision]
            await self.store.record_owner_event(f"{label} plugin {name} ({digest[:12]})")
            return label

    async def entries(self, scope: str) -> list[tuple[Plugin, ToolManifest]]:
        unique: dict[str, tuple[Plugin, ToolManifest]] = {}
        collisions = set()
        for plugin in await self.approved():
            for tool in plugin.manifest.tools:
                if scope not in tool.scopes:
                    continue
                name = plugin.tool_name(tool)
                if name in unique:
                    collisions.add(name)
                unique[name] = (plugin, tool)
        return [entry for name, entry in unique.items() if name not in collisions]

    async def approved_tools(self, scope: str) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": plugin.tool_name(tool), "description": tool.description, "parameters": tool.parameters,
            }}
            for plugin, tool in await self.entries(scope)
        ]
