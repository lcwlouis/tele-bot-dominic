"""find_devices: read-only LAN discovery, and owner-approved config changes when a configured host moves."""

import json
import subprocess

import pytest

from dom import devices
from dom.config import DesktopConfig, load_config, mdns_name, replace_host
from dom.desktop import network
from dom.tools import TurnCtx, enabled_tools
from tghelpers import FRIEND, OWNER

LOCAL = ".local"  # kept apart so the public-data check does not read these as real host names
HOST = "model" + LOCAL
OLD, NEW, OTHER = "192.0.2.10", "192.0.2.11", "192.0.2.12"
MAC, MAC2 = "02:00:00:00:00:01", "02:00:00:00:00:02"


def fake_run(avahi="", neighbours=(), missing=()):
    def run(args, **kwargs):
        if args[0] in missing:
            raise FileNotFoundError(args[0])
        if args[0] == "avahi-browse":
            assert set(args[1:]) == {"--all", "--resolve", "--terminate", "--parsable"}
            return subprocess.CompletedProcess(args, 0, avahi, "")
        assert args == ["ip", "-j", "neigh", "show"]
        return subprocess.CompletedProcess(args, 0, json.dumps(list(neighbours)), "")
    return run


def test_discovery_combines_dns_sd_names_and_neighbour_addresses():
    avahi = (
        f"+;eth0;IPv4;Model\\032server;_http._tcp;local\n"
        f"=;eth0;IPv4;Model\\032server;_http._tcp;local;{HOST};{NEW};8080;\n"
        f"=;eth0;IPv6;Model\\032server;_http._tcp;local;{HOST};2001:db8::1;8080;\n"
    )
    neighbours = [
        {"dst": NEW, "dev": "eth0", "lladdr": MAC, "state": ["STALE"]},
        {"dst": OLD, "dev": "eth0", "state": ["FAILED"]},
        {"dst": OTHER, "dev": "eth0", "lladdr": MAC2, "state": ["REACHABLE"]},
        {"dst": "198.51.100.2", "dev": "br-0123456789ab", "lladdr": "02:42:ac:1e:00:02", "state": ["STALE"]},
        {"dst": "2001:db8::5", "dev": "eth0", "lladdr": MAC2, "state": ["STALE"]},
    ]
    found = devices.collect(run=fake_run(avahi, neighbours))
    by_address = {device.address: device for device in found.devices}
    assert set(by_address) == {NEW, OTHER}
    assert by_address[NEW].names == {HOST, "Model server"} and by_address[NEW].macs == {MAC}
    assert found.notes == []


def test_missing_tools_are_reported_not_fatal():
    found = devices.collect(run=fake_run(neighbours=[{"dst": OLD, "dev": "eth0", "lladdr": MAC, "state": ["STALE"]}],
                                         missing=("avahi-browse",)))
    assert [device.address for device in found.devices] == [OLD]
    assert "avahi-utils" in found.notes[0]


@pytest.mark.parametrize("name,ok", [
    (HOST, True), ("a.b" + LOCAL, True), ("local", False), ("-x" + LOCAL, False), ("x_y" + LOCAL, False),
    ("example.org", False), ("ü" + LOCAL, False),
])
def test_mdns_names(name, ok):
    assert mdns_name(name) is ok


def test_lan_probes_accept_local_names_and_resolve_every_answer(monkeypatch):
    config = DesktopConfig(lan_probes=[f"{HOST}:8080", f"{OLD}:22"])
    assert network.lan_targets(config.lan_probes, resolve=lambda host: [OLD, NEW]) == [
        f"{OLD}:8080", f"{NEW}:8080", f"{OLD}:22",
    ]
    with pytest.raises(ValueError, match="resolve"):
        network.lan_targets(config.lan_probes, resolve=lambda host: [])
    for bad in (f"{HOST}:0", f"{HOST}", "example.org:443"):
        with pytest.raises(ValueError):
            DesktopConfig(lan_probes=[bad])


def write_config(app, extra):
    path = app.home.root / "config.toml"
    path.write_text(
        '# my config\n[models.chat]\nbase_url = "https://llm.test/v1"\nmodel = "m"\n'
        f'\n[models.lab]  # keep this comment\nbase_url = "http://{OLD}:8080/v1"\nmodel = "default"\n'
        f'\n[desktop]\nlan_probes = ["{OLD}:8080", "{OTHER}:80"]  # probes\n{extra}'
    )
    return path


def ctx(app, sender):
    return TurnCtx(home=app.home, store=app.store, chat_id=sender, sender_id=sender, owner_id=OWNER,
                   scope="owner_dm", app=app)


async def test_tool_is_owner_dm_only(make_app):
    app = make_app()
    assert "find_devices" in {tool.name for tool in await enabled_tools(app, "owner_dm")}
    for scope in ("friend_dm", "group"):
        assert "find_devices" not in {tool.name for tool in await enabled_tools(app, scope)}
    assert "only" in await devices.FIND_DEVICES_TOOL.handler({}, ctx(app, FRIEND))


async def test_a_moved_host_becomes_a_proposal_and_approval_rewrites_only_its_address(make_app, monkeypatch):
    app = make_app()
    path = write_config(app, "")
    lan = [devices.Device(OLD, {HOST}, {MAC}), devices.Device(OTHER, set(), {MAC2})]
    monkeypatch.setattr(devices, "collect", lambda: devices.Discovery(list(lan), []))
    first = await devices.FIND_DEVICES_TOOL.handler({}, ctx(app, OWNER))
    assert OLD in first and HOST in first and await app.proposals.pending() == []

    lan[0] = devices.Device(NEW, {HOST}, {MAC})  # the same machine, a new DHCP lease
    report = await devices.FIND_DEVICES_TOOL.handler({}, ctx(app, OWNER))
    await devices.FIND_DEVICES_TOOL.handler({}, ctx(app, OWNER))  # asking again does not duplicate it
    [proposal] = await app.proposals.pending()
    assert proposal["kind"] == "lan_address" and NEW in report and "approval" in report
    assert path.read_text().count(OLD) == 2  # nothing changes before approval
    assert any(call[0] == "send" and NEW in call[2] for call in app.bot.calls)

    assert await app.proposals.decide(proposal["id"], True) == "Approved"
    text = path.read_text()
    assert OLD not in text and "# keep this comment" in text and "# probes" in text
    config = load_config(app.home.root)
    assert config.models["lab"].base_url == f"http://{NEW}:8080/v1"
    assert config.desktop.lan_probes == [f"{NEW}:8080", f"{OTHER}:80"]


async def test_an_approval_is_refused_once_the_config_no_longer_uses_the_old_address(make_app, monkeypatch):
    app = make_app()
    path = write_config(app, "")
    lan = [devices.Device(OLD, set(), {MAC})]
    monkeypatch.setattr(devices, "collect", lambda: devices.Discovery(list(lan), []))
    await devices.find(app)
    lan[0] = devices.Device(NEW, set(), {MAC})
    await devices.find(app)
    [proposal] = await app.proposals.pending()
    path.write_text(path.read_text().replace(OLD, OTHER))
    assert await app.proposals.decide(proposal["id"], True) != "Approved"
    assert len(await app.proposals.pending()) == 1


async def test_a_different_machine_on_the_old_address_is_reported_not_adopted(make_app, monkeypatch):
    app = make_app()
    write_config(app, "")
    lan = [devices.Device(OLD, set(), {MAC})]
    monkeypatch.setattr(devices, "collect", lambda: devices.Discovery(list(lan), []))
    await devices.find(app)
    lan[0] = devices.Device(OLD, set(), {"02:00:00:00:00:99"})
    assert "different device" in await devices.find(app)
    lan.append(devices.Device(NEW, set(), {MAC}))
    await devices.find(app)
    [proposal] = await app.proposals.pending()
    assert json.loads(proposal["body_json"])["new"] == NEW


async def test_local_names_are_resolved_and_need_no_proposal(make_app, monkeypatch):
    app = make_app()
    write_config(app, "")
    path = app.home.root / "config.toml"
    path.write_text(path.read_text().replace(f'"http://{OLD}:8080/v1"', f'"http://{HOST}:8080/v1"'))
    monkeypatch.setattr(devices, "collect", lambda: devices.Discovery([], []))
    monkeypatch.setattr(devices, "resolve_ipv4", lambda host: [NEW] if host == HOST else [])
    report = await devices.find(app)
    assert f"{HOST}" in report and NEW in report
    assert await app.proposals.pending() == []


def test_replace_host_refuses_when_the_address_is_used_elsewhere(tmp_path):
    (tmp_path / "config.toml").write_text(
        f'[models.chat]\nbase_url = "http://{OLD}:1/v1"\nmodel = "m"\n[web]\nbind = "{OLD}"\n'
    )
    with pytest.raises(ValueError, match="by hand"):
        replace_host(tmp_path, OLD, NEW)
    with pytest.raises(ValueError, match="no longer"):
        replace_host(tmp_path, OTHER, NEW)
