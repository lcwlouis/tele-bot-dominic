import subprocess
import sys
from pathlib import Path

from dom.config import Config, DesktopConfig, ModelEndpoint, OpenersConfig, WebConfig

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_reference_matches_generator():
    generated = subprocess.check_output(
        [sys.executable, "scripts/config_reference.py"], cwd=ROOT, text=True,
    )
    assert generated == (ROOT / "docs/configuration.md").read_text()


def test_configuration_reference_has_every_toml_key():
    doc = (ROOT / "docs/configuration.md").read_text()
    root = Config.model_fields.keys() - {"home", "models", "desktop_mode", "desktop", "openers", "web"}
    for name in root:
        assert f"| `{name}` |" in doc
    for section, model in (
        ("desktop", DesktopConfig), ("models.<name>", ModelEndpoint),
        ("web", WebConfig), ("openers", OpenersConfig),
    ):
        for name in model.model_fields:
            assert f"| `{section}.{name}` |" in doc
    assert "| `desktop.mode` | `\"off\"` |" in doc
    assert "| `owner_id` | Unset |" in doc
    assert "| `models.chat_fallback` | Optional" in doc


def test_readme_links_to_the_release_guides():
    readme = (ROOT / "README.md").read_text()
    for guide in ("setup", "configuration", "privacy"):
        assert f"](docs/{guide}.md)" in readme
        assert (ROOT / f"docs/{guide}.md").is_file()
