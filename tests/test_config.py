import pytest
from pydantic import ValidationError

from dom.config import Config, load_config, save_owner

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
    assert (cfg.window_budget_tokens, cfg.window_keep_tokens, cfg.window_size) == (32000, 20000, 500)
    assert cfg.presence.seen_reaction is True
    assert cfg.timezone == "UTC"
    assert cfg.models["chat"].model == "some/model"
    assert cfg.home == tmp_path


def test_seen_reaction_can_be_switched_off(tmp_path):
    write(tmp_path, MINIMAL + "\n[presence]\nseen_reaction = false\n")
    assert load_config(tmp_path).presence.seen_reaction is False


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


def test_save_owner_sets_root_key_not_a_nested_one(tmp_path):
    import tomllib
    (tmp_path / "config.toml").write_text('timezone = "UTC"\n\n[extra]\nowner_id = 5\n')
    save_owner(tmp_path, 111)
    raw = tomllib.loads((tmp_path / "config.toml").read_text())
    assert raw["owner_id"] == 111
    assert raw["extra"]["owner_id"] == 5
    assert load_config(tmp_path).owner_id == 111


def test_save_owner_replaces_only_root_line_and_leaves_no_temp_files(tmp_path):
    (tmp_path / "config.toml").write_text('owner_id = 1\n[extra]\nowner_id = 5\n')
    save_owner(tmp_path, 7)
    assert (tmp_path / "config.toml").read_text() == 'owner_id = 7\n[extra]\nowner_id = 5\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_failed_save_keeps_claim_code_and_config(tmp_path):
    from dom.claim import try_claim, write_claim_code
    bad = "this is = not [valid toml"
    (tmp_path / "config.toml").write_text(bad)
    code = write_claim_code(tmp_path)
    config = Config(home=tmp_path)
    assert try_claim(tmp_path, config, 111, "private", code) is False
    assert (tmp_path / ".claim").exists()
    assert (tmp_path / "config.toml").read_text() == bad


def test_daily_spend_cap_default_and_override(tmp_path):
    write(tmp_path, MINIMAL)
    assert load_config(tmp_path).daily_spend_cap_usd == 1.0
    write(tmp_path, "daily_spend_cap_usd = 0\n" + MINIMAL)
    assert load_config(tmp_path).daily_spend_cap_usd == 0


def test_legacy_single_desktop_becomes_one_host(tmp_path):
    write(tmp_path, MINIMAL + '\n[desktop]\nmode = "local"\nlan_probes = ["192.0.2.1:443"]\n')
    (host,) = load_config(tmp_path).desktop_hosts
    assert (host.name, host.mode, host.max_containers, host.memory_floor_mb) == ("default", "local", 1, 512)

    write(tmp_path, MINIMAL + '\n[desktop]\nmode = "remote"\nurl = "https://sandbox.example.test"\n'
                              'remote_isolation_confirmed = true\n')
    (host,) = load_config(tmp_path).desktop_hosts
    assert (host.name, host.mode, host.url, host.remote_isolation_confirmed, host.memory_floor_mb) == (
        "default", "remote", "https://sandbox.example.test", True, 0,
    )

    write(tmp_path, MINIMAL)
    assert load_config(tmp_path).desktop_hosts == []


def test_desktop_hosts_table(tmp_path):
    write(tmp_path, MINIMAL + """
[desktop]
lan_probes = ["192.0.2.1:443"]

[[desktop.hosts]]
name = "pi"
mode = "local"
max_containers = 2
min_free_memory_mb = 900

[[desktop.hosts]]
name = "studio"
mode = "remote"
url = "https://studio.example.test"
min_free_memory_mb = 16384
""")
    config = load_config(tmp_path)
    assert config.desktop_mode == "local"  # hosts alone switch the desktop on
    assert [(h.name, h.mode, h.max_containers, h.memory_floor_mb) for h in config.desktop_hosts] == [
        ("pi", "local", 2, 900), ("studio", "remote", 1, 16384),
    ]


@pytest.mark.parametrize("hosts, message", [
    ('[[desktop.hosts]]\nname = "Pi"\nmode = "local"\n', "name"),
    ('[[desktop.hosts]]\nname = "a"\nmode = "local"\n[[desktop.hosts]]\nname = "a"\nmode = "local"\n', "unique"),
    ('[[desktop.hosts]]\nname = "a"\nmode = "local"\n[[desktop.hosts]]\nname = "b"\nmode = "local"\n', "one local"),
    ('[[desktop.hosts]]\nname = "a"\nmode = "remote"\nmax_containers = 2\n', "one container"),
    ('[[desktop.hosts]]\nname = "a"\nmode = "local"\nmax_containers = 0\n', "max_containers"),
])
def test_invalid_desktop_hosts_rejected(tmp_path, hosts, message):
    write(tmp_path, MINIMAL + "\n[desktop]\n" + hosts)
    with pytest.raises(ValidationError, match=message):
        load_config(tmp_path)


def test_mode_off_disables_listed_hosts(tmp_path):
    write(tmp_path, MINIMAL + '\n[desktop]\nmode = "off"\n[[desktop.hosts]]\nname = "pi"\nmode = "local"\n')
    config = load_config(tmp_path)
    assert config.desktop_mode == "off" and config.desktop_hosts == []
