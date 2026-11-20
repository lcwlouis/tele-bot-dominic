import subprocess
import sys
from pathlib import Path

from dom.config import Config, DesktopConfig, DesktopHost, ModelEndpoint, OpenersConfig, PresenceConfig, T3Config, UpdateConfig, WebConfig

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_reference_matches_generator():
    generated = subprocess.check_output(
        [sys.executable, "scripts/config_reference.py"], cwd=ROOT, text=True,
    )
    assert generated == (ROOT / "docs/configuration.md").read_text()


def test_configuration_reference_has_every_toml_key():
    doc = (ROOT / "docs/configuration.md").read_text()
    root = Config.model_fields.keys() - {"home", "models", "desktop_mode", "desktop", "openers", "presence", "web", "t3", "update"}
    for name in root:
        assert f"| `{name}` |" in doc
    for section, model in (
        ("desktop", DesktopConfig), ("models.<name>", ModelEndpoint),
        ("web", WebConfig), ("openers", OpenersConfig), ("presence", PresenceConfig), ("t3", T3Config), ("update", UpdateConfig),
    ):
        for name in model.model_fields:
            assert f"| `{section}.{name}` |" in doc
    for name in DesktopHost.model_fields:
        assert f"| `desktop.hosts[].{name}` |" in doc
    assert "| `desktop.mode` | `\"off\"` |" in doc
    assert "| `owner_id` | Unset |" in doc
    assert "| `models.chat_fallback` | Optional" in doc


def test_readme_links_to_the_release_guides():
    readme = (ROOT / "README.md").read_text()
    for guide in ("setup", "configuration", "privacy"):
        assert f"](docs/{guide}.md)" in readme
        assert (ROOT / f"docs/{guide}.md").is_file()


def test_changelog_has_unreleased_then_the_first_release():
    headings = [line for line in (ROOT / "CHANGELOG.md").read_text().splitlines() if line.startswith("## ")]
    assert headings[:2] == ["## [Unreleased]", "## [0.7.0] - 2026-11-19"]


def test_pi_guide_explains_cutting_a_release_from_a_tag():
    guide = (ROOT / "docs/deploy-pi.md").read_text()
    for step in ("scripts/changelog.py", "git tag vX.Y.Z", "gh release create vX.Y.Z", "git archive \"$tag\""):
        assert step in guide
    assert "Deploy from tags" in guide
