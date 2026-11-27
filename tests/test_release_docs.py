import subprocess
import sys
from pathlib import Path

from dom.config import AcpAgent, Config, DesktopConfig, DesktopHost, ModelEndpoint, KnowledgeConfig, OpenersConfig, PresenceConfig, ProviderSetup, SelfWorkConfig, StudioConfig, StudioTier, StudioTimeouts, T3Config, TracerConfig, UpdateConfig, WebConfig, WorldConfig

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_reference_matches_generator():
    generated = subprocess.check_output(
        [sys.executable, "scripts/config_reference.py"], cwd=ROOT, text=True,
    )
    assert generated == (ROOT / "docs/configuration.md").read_text()


def test_configuration_reference_has_every_toml_key():
    doc = (ROOT / "docs/configuration.md").read_text()
    root = Config.model_fields.keys() - {"home", "models", "provider_setups", "desktop_mode", "desktop", "openers", "assistant", "dispatch", "presence", "self_work", "studio", "knowledge", "world", "web", "tracer", "t3", "acp", "update"}
    for name in root:
        assert f"| `{name}` |" in doc
    for section, model in (
        ("desktop", DesktopConfig), ("models.<name>", ModelEndpoint), ("providers.<name>", ProviderSetup),
        ("web", WebConfig), ("openers", OpenersConfig), ("presence", PresenceConfig), ("self_work", SelfWorkConfig), ("t3", T3Config), ("update", UpdateConfig), ("knowledge", KnowledgeConfig), ("world", WorldConfig), ("tracer", TracerConfig),
    ):
        for name in model.model_fields:
            assert f"| `{section}.{name}` |" in doc
    for name in AcpAgent.model_fields:
        assert f"| `acp.<name>.{name}` |" in doc
    for name in StudioConfig.model_fields.keys() - {"limits", "timeouts"}:
        assert f"| `studio.{name}` |" in doc
    for scope in ("owner_dm", "friend_dm", "group"):
        for name in StudioTier.model_fields:
            assert f"| `studio.limits.{scope}.{name}` |" in doc
    for name in StudioTimeouts.model_fields:
        assert f"| `studio.timeouts.{name}` |" in doc
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


def test_readme_links_to_every_guide():
    readme = (ROOT / "README.md").read_text()
    for guide in sorted((ROOT / "docs").glob("*.md")):
        assert f"](docs/{guide.name})" in readme, guide.name


def test_changelog_has_unreleased_then_the_first_release():
    headings = [line for line in (ROOT / "CHANGELOG.md").read_text().splitlines() if line.startswith("## ")]
    assert headings[:2] == ["## [Unreleased]", "## [0.8.5] - 2026-11-27"]


def test_pi_guide_explains_cutting_a_release_from_a_tag():
    guide = (ROOT / "docs/deploy-pi.md").read_text()
    for step in ("scripts/changelog.py", "git tag vX.Y.Z", "gh release create vX.Y.Z", "git archive \"$tag\""):
        assert step in guide
    assert "Deploy from tags" in guide
