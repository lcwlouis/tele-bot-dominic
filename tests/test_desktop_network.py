import json
import subprocess

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

    def __call__(self, args, **kwargs):
        args = tuple(args)
        self.commands.append(args)
        if args[:3] == ("docker", "network", "inspect"):
            body = [{"Driver": "bridge", "EnableIPv6": False, "IPAM": {"Config": [{"Subnet": self.subnet}]}}]
            return subprocess.CompletedProcess(args, int(not self.created), json.dumps(body))
        if args[:3] == ("docker", "network", "create"):
            self.created = True
        elif args[:2] == ("iptables", "-C"):
            return subprocess.CompletedProcess(args, int(args[2:] not in self.rules))
        elif args[:2] == ("iptables", "-I"):
            self.rules.add((args[2], *args[4:]))
        return subprocess.CompletedProcess(args, 0)


def test_setup_idempotence(tmp_path, capsys):
    config = Config(home=tmp_path, desktop_mode="local")
    runner = Runner()
    setup(config, runner)
    assert runner.rules == set(rules(config.desktop.subnet))
    assert ("docker", "network", "create", "--driver", "bridge", "--ipv6=false",
            "--subnet", "172.30.0.0/24", "dom-desktop") in runner.commands
    assert capsys.readouterr().out.count("iptables -I") == len(runner.rules)
    runner.commands.clear()
    setup(config, runner)
    assert not any(cmd[1] == "-I" or cmd[1:3] == ("network", "create") for cmd in runner.commands)
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
