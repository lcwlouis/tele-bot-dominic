import pytest
from pydantic import ValidationError

from dom.config import load_config, save_owner

MINIMAL = """\
[models.chat]
base_url = "https://example.test/v1"
model = "some/model"
api_key_env = "TEST_KEY"
"""


def write(home, text):
    (home / "config.toml").write_text(text)


def test_defaults_when_minimal_toml(tmp_path):
    write(tmp_path, MINIMAL)
    cfg = load_config(tmp_path)
    assert cfg.owner_id is None
    assert cfg.desktop_mode == "off"
    assert cfg.window_size == 35
    assert cfg.timezone == "Asia/Singapore"
    assert cfg.models["chat"].model == "some/model"
    assert cfg.home == tmp_path


def test_secret_prefers_dotenv_over_environ(tmp_path, monkeypatch):
    write(tmp_path, MINIMAL)
    (tmp_path / ".env").write_text("# c\nTEST_KEY=from-file\nOTHER = spaced \n")
    monkeypatch.setenv("TEST_KEY", "from-env")
    monkeypatch.setenv("ONLY_ENV", "env-only")
    cfg = load_config(tmp_path)
    assert cfg.secret("TEST_KEY") == "from-file"
    assert cfg.secret("OTHER") == "spaced"
    assert cfg.secret("ONLY_ENV") == "env-only"
    assert cfg.secret("MISSING_KEY") is None


def test_save_owner_preserves_other_lines(tmp_path):
    text = "# my config\ntimezone = \"UTC\"\n\n" + MINIMAL + "# trailing\n"
    write(tmp_path, text)
    save_owner(tmp_path, 111)
    out = (tmp_path / "config.toml").read_text()
    assert "# my config" in out and "# trailing" in out
    assert out.count("[models.chat]") == 1
    assert load_config(tmp_path).owner_id == 111
    assert load_config(tmp_path).timezone == "UTC"
    # second save replaces rather than duplicates
    save_owner(tmp_path, 222)
    out = (tmp_path / "config.toml").read_text()
    assert out.count("owner_id") == 1
    assert load_config(tmp_path).owner_id == 222


def test_unknown_desktop_mode_rejected(tmp_path):
    write(tmp_path, MINIMAL + '\n[desktop]\nmode = "cloud"\n')
    with pytest.raises(ValidationError):
        load_config(tmp_path)
