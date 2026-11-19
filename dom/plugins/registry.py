"""Stage disk plugins and expose tools only for their currently approved bytes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from dom.home import Home
from dom.plugins.manifest import Plugin, ToolManifest, read_plugin
from dom.store import Store

log = logging.getLogger(__name__)


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

    async def _scan(self) -> dict[str, Plugin]:
        found = await asyncio.to_thread(self._read_all)
        await self.store.stage_plugins([
            (name, plugin.hash, json.dumps(asdict(plugin.manifest))) for name, plugin in found.items()
        ])
        return found

    async def scan(self) -> dict[str, Plugin]:
        async with self.lock:
            return await self._scan()

    async def approved(self) -> list[Plugin]:
        async with self.lock:
            found = await self._scan()
            return [
                found[row["name"]] for row in await self.store.list_plugins()
                if row["status"] == "approved" and row["name"] in found
                and found[row["name"]].hash == row["approved_hash"]
            ]

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
