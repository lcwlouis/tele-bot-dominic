"""Run a staged plugin with synthetic input and summarize it for owner review."""

from __future__ import annotations

import json
from time import monotonic

from dom.plugins.manifest import Plugin
from dom.plugins.runner import PluginBusy, PluginRunner, PluginsUnavailable
from dom.plugins.surfaces import validate_panel
from dom.skills import _visible

STORAGE_NOTICE = "Storage enabled. This plugin has one 64 KB JSON blob for its owner-only calls."
_LINK_PUNCTUATION = str.maketrans({".": "·", "。": "·", "．": "·", "｡": "·", ":": "∶", "@": "＠"})
_PREVIEW_LIMIT = 160


def _preview(text: str) -> str:
    text = " ".join("".join(char if _visible(char) else " " for char in text).split())
    text = text.translate(_LINK_PUNCTUATION)
    return text if len(text) <= _PREVIEW_LIMIT else text[:_PREVIEW_LIMIT - 1] + "…"


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
    except (PluginBusy, PluginsUnavailable):
        raise
    except (ValueError, OverflowError) as error:
        return {"label": label, "elapsed_ms": round((monotonic() - started) * 1000),
                "output": None, "error": _preview(str(error)) or "Plugin test failed."}


def test_run_detail(result: dict) -> str:
    detail = _preview(result["error"] or result["output"] or "")
    return f"Plugin said: {json.dumps(detail, ensure_ascii=False)}" if detail else "No output."


def test_run_summary(result: dict) -> str:
    status = "failed" if result["error"] is not None else "passed"
    return f"Test run {status} · {result['label']} · {result['elapsed_ms']} ms\n{test_run_detail(result)}"
