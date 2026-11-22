"""Run a staged plugin with synthetic input and summarize it for owner review."""

from __future__ import annotations

import json
from time import monotonic

from dom.plugins.manifest import Plugin
from dom.plugins.runner import PluginRunner
from dom.plugins.surfaces import validate_panel
from dom.skills import _visible

STORAGE_NOTICE = "Storage enabled. This plugin's calls share one 64 KB JSON blob across chat scopes."


def _preview(text: str) -> str:
    text = "".join(char for char in text if _visible(char)).strip()
    return text if len(text) <= 600 else text[:599] + "…"


def _sample(plugin: Plugin) -> tuple[str, dict]:
    manifest = plugin.manifest
    if manifest.panels:
        name = manifest.panels[0].id
        return f"panel {name}", {"panel": name}
    if manifest.commands:
        command = manifest.commands[0]
        return f"/{command.name}", {"command": command.name, "args": "", "scope": command.scopes[0]}
    tool = manifest.tools[0]
    return f"tool {tool.name}", {"tool": tool.name, "args": {}}


def test_run(runner: PluginRunner, plugin: Plugin) -> dict:
    fallback, sample = _sample(plugin)
    label = f"self-test or {fallback}"
    started = monotonic()
    try:
        trial = runner.trial(plugin, sample)
        if (not isinstance(trial, dict) or set(trial) != {"output", "self_test"}
                or type(trial["self_test"]) is not bool):
            raise ValueError("Invalid plugin test result.")
        output = trial["output"]
        if trial["self_test"]:
            label = "self-test"
            if output is not None and not isinstance(output, str):
                raise ValueError("The self-test must return text or None.")
        elif "panel" in sample:
            validate_panel(output)
            output = json.dumps(output, ensure_ascii=True)
        elif not isinstance(output, str):
            raise ValueError("The sample must return text.")
        if not trial["self_test"]:
            label = fallback
        return {"label": label, "elapsed_ms": round((monotonic() - started) * 1000),
                "output": _preview(output or ""), "error": None}
    except (ValueError, OverflowError) as error:
        return {"label": label, "elapsed_ms": round((monotonic() - started) * 1000),
                "output": None, "error": _preview(str(error)) or "Plugin test failed."}


def test_run_summary(result: dict) -> str:
    status = "failed" if result["error"] is not None else "passed"
    detail = result["error"] if status == "failed" else result["output"] or "No output."
    return f"Test run {status} · {result['label']} · {result['elapsed_ms']} ms\n{detail}"
