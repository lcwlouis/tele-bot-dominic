"""Stage disk plugins and expose tools only for their currently approved bytes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from tempfile import TemporaryDirectory

from dom.deletion import STALE_MESSAGE, hash_matches, prune_deletions, stage_deletion
from dom.home import Home
from dom.plugins.manifest import (
    CommandManifest, Manifest, PanelManifest, Plugin, ToolManifest, read_plugin, validate_name,
)
from dom.plugins.runner import PluginBusy, PluginRunner, PluginsUnavailable
from dom.plugins.trial import test_run
from dom.store import Store

log = logging.getLogger(__name__)
PLUGIN_HASH = re.compile(r"[0-9a-f]{64}")
DRAFT_PREFIX = ".draft-"  # staging folders made by _write; never plugins


def _surfaces(manifest: Manifest) -> tuple:
    """What a plugin offers and where: anything here changing is shown to the owner as a surface change."""
    return manifest.tools, manifest.commands, manifest.panels, manifest.network, manifest.storage


class PluginRegistry:
    def __init__(
        self, home: Home, store: Store, on_change: Callable[[], Awaitable[None]] | None = None,
        *, runner: PluginRunner | None = None, is_running: Callable[[str], bool] | None = None,
    ):
        self.home = home.scoped(allow_private=False)
        self.store = store
        self.lock = asyncio.Lock()
        self.runner = runner or PluginRunner(home=home.root, store=store)
        self._trial_lock = asyncio.Lock()
        self._trials: dict[tuple[str, str], asyncio.Task[dict | None]] = {}
        self.on_change = on_change  # called after the owner approves, rejects, disables or deletes a plugin
        self.is_running = is_running or self.runner.is_running

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
            if path.name.startswith(DRAFT_PREFIX) or (not path.is_dir() and not path.is_symlink()):
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

    def _clean_drafts(self) -> None:
        try:
            parent = self.home._regular_path("plugins")
            leftovers = sorted(parent.glob(DRAFT_PREFIX + "*")) if parent.is_dir() else []
        except (OSError, ValueError, RuntimeError) as error:
            log.warning("Could not clean plugin drafts: %s", type(error).__name__)
            return
        for path in leftovers:
            try:
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                    continue
                # A crash between the two renames in _write leaves the old version here. Put it back.
                previous = path / "previous"
                if previous.is_dir() and not previous.is_symlink():
                    name = tomllib.loads((previous / "plugin.toml").read_text(encoding="utf-8")).get("name")
                    validate_name(name)
                    if not (parent / name).exists():
                        previous.rename(parent / name)
                shutil.rmtree(path)
            except (OSError, ValueError, UnicodeError):
                log.warning("Could not clean plugin draft %r", path.name)

    async def cleanup_drafts(self) -> None:
        """Remove staging folders a crashed draft left behind. Run once at startup."""
        async with self.lock:
            await asyncio.to_thread(self._clean_drafts)

    def _queue_trial(self, plugin: Plugin) -> asyncio.Task[dict | None]:
        key = plugin.manifest.name, plugin.hash
        if key not in self._trials:
            self._trials[key] = asyncio.create_task(self._run_trial(plugin))
        return self._trials[key]

    async def _run_trial(self, plugin: Plugin) -> dict | None:
        key = plugin.manifest.name, plugin.hash
        try:
            async with self._trial_lock:
                result = await asyncio.to_thread(test_run, self.runner, plugin)
                await self.store.set_plugin_test_run(*key, result)
                return result
        except (PluginBusy, PluginsUnavailable):
            return None  # Leave the result unset so a later scan can retry.
        except Exception:
            log.exception("Could not finish plugin test run for %s", key[0])
            return None
        finally:
            self._trials.pop(key, None)

    async def draft(self, plugin: Plugin) -> dict | None:
        async with self.lock:
            name = plugin.manifest.name
            rows = await self.store.list_plugins()
            if any(row["name"] == name and row["status"] == "approved" and row["hash"] == plugin.hash for row in rows):
                raise ValueError("This plugin version is already approved.")
            # A disabled plugin keeps its files but gives up its command names.
            live = {row["name"] for row in rows if row["status"] in ("approved", "pending") and row["name"] != name}
            mine = {command.name for command in plugin.manifest.commands}
            for other in (await asyncio.to_thread(self._read_all)).values():
                taken = mine & {command.name for command in other.manifest.commands}
                if other.manifest.name in live and taken:
                    raise ValueError(f"/{min(taken)} is already a command of plugin {other.manifest.name}.")
            await asyncio.to_thread(self._write, plugin)
            await self.store.stage_plugins([(name, plugin.hash, json.dumps(asdict(plugin.manifest)))])
            await self.store.set_plugin_status(name, plugin.hash, "pending")
            trial = self._queue_trial(plugin)
        return await asyncio.shield(trial)

    async def _scan(self) -> dict[str, Plugin]:
        found = await asyncio.to_thread(self._read_all)
        await self.store.stage_plugins([
            (name, plugin.hash, json.dumps(asdict(plugin.manifest))) for name, plugin in found.items()
        ])
        for row in await self.store.list_plugins():
            plugin = found.get(row["name"])
            if plugin and row["status"] != "disabled" and row["test_run_json"] is None and plugin.hash == row["hash"]:
                self._queue_trial(plugin)
        return found

    async def scan(self) -> dict[str, Plugin]:
        async with self.lock:
            found = await self._scan()
            trials = self._matching_trials(found)
        await asyncio.gather(*(asyncio.shield(trial) for trial in trials))
        return found

    def _matching_trials(self, found: dict[str, Plugin]) -> list[asyncio.Task[dict | None]]:
        return [trial for (name, digest), trial in self._trials.items()
                if name in found and found[name].hash == digest]

    async def listing(self) -> list[dict]:
        async with self.lock:
            found = await self._scan()
            rows = await self.store.list_plugins()
        listing = []
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            listing.append({
                **row, "available": row["name"] in found and found[row["name"]].hash == row["hash"],
                "network": manifest["network"], "storage": manifest.get("storage", False),
                "test_run": json.loads(row["test_run_json"] or "null"),
            })
        return listing

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
        if not PLUGIN_HASH.fullmatch(digest):
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
            reviewed = await self.store.kv_get(f"plugin-reviewed:{name}:{digest}") is not None
            surfaces_changed = False
            if "plugin.toml" in old:
                try:
                    surfaces_changed = _surfaces(Manifest.parse(old["plugin.toml"])) != _surfaces(plugin.manifest)
                except (ValueError, tomllib.TOMLDecodeError):
                    surfaces_changed = True
            return {
                "plugin": plugin, "status": row["status"], "documents": documents, "reviewed": reviewed,
                "surfaces_changed": surfaces_changed, "test_run": json.loads(row["test_run_json"] or "null"),
            }

    async def mark_reviewed(self, name: str, digest: str) -> None:
        key = f"plugin-reviewed:{name}:{digest}"
        async with self.lock:
            if await self.store.kv_get(key) is None:
                await self.store.kv_set(key, "yes")
                await self.store.record_owner_event(f"Reviewed plugin {name} ({digest[:12]}) code on the dashboard")

    async def decide(self, name: str, digest: str, decision: str) -> str:
        label = await self._decide(name, digest, decision)
        if label in ("Approved", "Rejected", "Disabled") and self.on_change is not None:
            try:
                await self.on_change()
            except Exception:
                log.exception("could not update after a plugin decision")
        return label

    async def _decide(self, name: str, digest: str, decision: str) -> str:
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

    def _inspect_for_delete(self, name: str, db_row: dict | None) -> tuple[Path, bool, str | None]:
        """Inspect disk + DB state of `name`. Returns (folder, on_disk, live_hash_or_None)."""
        target = self.home._regular_path(f"plugins/{name}")
        on_disk = target.exists()
        if on_disk:
            if target.is_symlink() or not target.is_dir():
                raise ValueError("The plugin path must be a regular directory.")
            try:
                disk_hash = read_plugin(target).hash
            except (OSError, ValueError, RecursionError):
                disk_hash = db_row["hash"] if db_row is not None else None
            if db_row is not None and disk_hash is not None and db_row["hash"] != disk_hash:
                return target, True, disk_hash  # changed on disk since staged
            return target, True, disk_hash
        return target, False, db_row["hash"] if db_row is not None else None

    async def delete(self, name: str, expected_hash: str) -> str:
        """Move a plugin's files into the deletion archive and remove its DB/KV state in one tx.
        Refuses if mid-call or if the plugin changed since confirmation."""
        validate_name(name)
        if not expected_hash:
            return STALE_MESSAGE
        async with self.lock:
            if self.is_running is not None and self.is_running(name):
                return f"Cannot delete plugin {name} while it is running."
            snapshot = await self.store.plugin_state_snapshot(name)
            db_row = snapshot["plugin"]
            target, on_disk, live_hash = await asyncio.to_thread(self._inspect_for_delete, name, db_row)
            if db_row is None and not on_disk:
                return "Unknown plugin"
            if live_hash is not None and not hash_matches(live_hash, expected_hash):
                return STALE_MESSAGE
            if db_row is not None and not hash_matches(db_row["hash"], expected_hash):
                return STALE_MESSAGE
        event_at = datetime.now(timezone.utc).isoformat()
        event_text = f"Deleted plugin {name}"
        try:
            archive = await asyncio.to_thread(
                stage_deletion,
                self.home.root,
                "plugin",
                name,
                event_text=event_text,
                event_at=event_at,
                rows=snapshot,
                sources=[target] if on_disk else [],
            )
        except (OSError, ValueError) as error:
            log.warning("could not stage deletion archive for plugin %s: %s", name, type(error).__name__)
            return "Could not archive plugin before deleting."

        async with self.lock:
            try:
                if self.is_running is not None and self.is_running(name):
                    await asyncio.to_thread(archive.rollback)
                    return f"Cannot delete plugin {name} while it is running."
                # Re-check hash under self.lock (the same lock draft() and decide() use) so a concurrent
                # draft or approval is never deleted unseen.
                snapshot = await self.store.plugin_state_snapshot(name)
                db_row = snapshot["plugin"]
                target, on_disk, live_hash = await asyncio.to_thread(self._inspect_for_delete, name, db_row)
                if db_row is None and not on_disk:
                    await asyncio.to_thread(archive.rollback)
                    return "Unknown plugin"
                if (
                    (live_hash is not None and not hash_matches(live_hash, expected_hash))
                    or (db_row is not None and not hash_matches(db_row["hash"], expected_hash))
                ):
                    await asyncio.to_thread(archive.rollback)
                    return STALE_MESSAGE
                archive.sources = [target] if on_disk else []
                await asyncio.to_thread(archive.update_rows, snapshot)
                await asyncio.to_thread(archive.move_in)
                result = await self.store.delete_plugin(
                    name, expected_hash, event_text, event_at, allow_missing_row=on_disk,
                )
            except (OSError, ValueError, sqlite3.Error) as error:
                log.warning("could not delete plugin %s: %s", name, type(error).__name__)
                await asyncio.to_thread(archive.rollback)
                return "Could not delete plugin."
            if result != "deleted":
                await asyncio.to_thread(archive.rollback)
                return STALE_MESSAGE if result == "stale" else "Unknown plugin"
            await asyncio.to_thread(archive.commit)

        if self.on_change is not None:
            try:
                await self.on_change()
            except Exception:
                log.exception("could not update after deleting plugin %s", name)
        await asyncio.to_thread(prune_deletions, self.home.root)
        return "Deleted"

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

    async def approved_folder(self, plugin: Plugin) -> Path | None:
        """The plugin's folder if exactly this version is still approved. The runner pins the hash again."""
        async with self.lock:
            if not any(p.manifest.name == plugin.manifest.name and p.hash == plugin.hash for p in await self._approved()):
                return None
            return self.home._regular_path(f"plugins/{plugin.manifest.name}")

    async def command_entries(self, scope: str) -> list[tuple[Plugin, CommandManifest]]:
        """Approved commands offered in `scope`. A name two approved plugins declare is offered by neither."""
        unique: dict[str, tuple[Plugin, CommandManifest]] = {}
        collisions = set()
        for plugin in await self.approved():
            for command in plugin.manifest.commands:
                if command.name in unique:
                    collisions.add(command.name)
                unique[command.name] = (plugin, command)
        return [entry for name, entry in unique.items() if name not in collisions and scope in entry[1].scopes]

    async def panel_entries(self) -> list[tuple[Plugin, PanelManifest]]:
        return [(plugin, panel) for plugin in await self.approved() for panel in plugin.manifest.panels]
