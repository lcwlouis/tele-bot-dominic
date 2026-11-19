import json
import subprocess

import pytest

from dom.cli import main
from dom.config import Config
from dom.desktop.network import check, rules, setup


def test_rules_for_subnet():
    subnet = "172.30.1.0/24"
    expected = [
        ("DOCKER-USER", "-s", subnet, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"),
        ("DOCKER-USER", "-s", subnet, "-d", subnet, "-j", "RETURN"),
        *[("DOCKER-USER", "-s", subnet, "-d", dst, "-j", "DROP") for dst in (
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
            "0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/3",
        )],
        ("INPUT", "-s", subnet, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"),
        ("INPUT", "-s", subnet, "-j", "DROP"),
    ]
    assert rules(subnet) == expected


class Runner:
    def __init__(self, subnet="172.30.0.0/24"):
        self.rules = set()
        self.created = False
        self.subnet = subnet
        self.commands = []
        self.check_status = None  # a status for every iptables -C that finds nothing

    def __call__(self, args, **kwargs):
        args = tuple(args)
        self.commands.append(args)
        if args[:3] == ("docker", "network", "inspect"):
            body = [{"Driver": "bridge", "EnableIPv6": False, "IPAM": {"Config": [{"Subnet": self.subnet}]}}]
            return subprocess.CompletedProcess(args, int(not self.created), json.dumps(body))
        if args[:3] == ("docker", "network", "create"):
            self.created = True
        elif args[:3] == ("iptables", "-w", "-C"):
            absent = args[3:] not in self.rules
            return subprocess.CompletedProcess(args, (self.check_status or 1) if absent else 0)
        elif args[:3] == ("iptables", "-w", "-I"):
            self.rules.add((args[3], *args[5:]))
        elif args[0] == "iptables":
            raise AssertionError(f"iptables must be called with -w: {args}")
        return subprocess.CompletedProcess(args, 0)


def test_setup_idempotence(tmp_path, capsys):
    config = Config(home=tmp_path, desktop_mode="local")
    runner = Runner()
    setup(config, runner)
    assert runner.rules == set(rules(config.desktop.subnet))
    assert ("docker", "network", "create", "--driver", "bridge", "--ipv6=false",
            "--subnet", "172.30.0.0/24", "dom-desktop") in runner.commands
    assert capsys.readouterr().out.count("iptables -w -I") == len(runner.rules)
    runner.commands.clear()
    setup(config, runner)
    assert not any(cmd[2] == "-I" or cmd[1:3] == ("network", "create") for cmd in runner.commands if cmd[0] == "iptables" or cmd[1:3] == ("network", "create"))
    assert capsys.readouterr().out == ""


def test_check_uses_throwaway_container_and_no_pull(tmp_path):
    runner = Runner()
    check(Config(home=tmp_path, desktop_mode="local"), "192.0.2.1", run=runner)
    cmd, = runner.commands
    assert cmd[:10] == ("docker", "run", "--rm", "--pull=never", "--network", "dom-desktop",
                        "--entrypoint", "python3", "ghcr.io/agent-infra/sandbox:latest", "-c")
    assert cmd[-3:] == ("192.0.2.1", "443", "1.1.1.1")


def test_desktop_cli_accepts_home(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[desktop]\nmode = "local"\n')
    seen = []
    monkeypatch.setattr("dom.cli.desktop_network.setup", lambda config: seen.append(config.home))
    assert main(["desktop-setup", str(tmp_path)]) == 0
    assert seen == [tmp_path]


@pytest.mark.parametrize("status", [2, 4, 127])
def test_only_status_one_means_the_rule_is_absent(tmp_path, status):
    runner = Runner()
    runner.check_status = status
    with pytest.raises(subprocess.CalledProcessError):
        setup(Config(home=tmp_path, desktop_mode="local"), runner)
    assert not any(cmd[:3] == ("iptables", "-w", "-I") for cmd in runner.commands)
