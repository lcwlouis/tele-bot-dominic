import json
import shlex
import subprocess

import pytest

from dom.cli import main
from dom.config import Config
from dom.desktop.network import CHAIN, PROBE_SCRIPT, check, jump, rules, setup

SUBNET = "172.30.0.0/24"
LAN = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
       "0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/3")


def test_rules_for_subnet():
    subnet = "198.51.100.0/24"
    assert CHAIN == "DOM-DESKTOP"
    assert rules(subnet) == [
        ("-m", "addrtype", "--dst-type", "LOCAL", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"),
        ("-m", "addrtype", "--dst-type", "LOCAL", "-j", "DROP"),
        ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"),
        ("-d", subnet, "-j", "RETURN"),
        *[("-d", dst, "-j", "DROP") for dst in LAN],
    ]
    assert jump(subnet) == ("-s", subnet, "-j", "DOM-DESKTOP")


class Runner:
    """A small stateful stand-in for docker and iptables."""

    def __init__(self, subnet=SUBNET):
        self.chains = {"DOCKER-USER": [], "INPUT": []}
        self.created = False
        self.subnet = subnet
        self.commands = []
        self.check_status = None  # a status for every query that finds nothing

    def mutations(self):
        return [cmd for cmd in self.commands if cmd[0] == "iptables" and cmd[2] != "-C" and cmd[2] != "-S"]

    def __call__(self, args, **kwargs):
        args = tuple(args)
        self.commands.append(args)
        if args[:3] == ("docker", "network", "inspect"):
            body = [{"Driver": "bridge", "EnableIPv6": False, "IPAM": {"Config": [{"Subnet": self.subnet}]}}]
            return subprocess.CompletedProcess(args, int(not self.created), json.dumps(body))
        if args[:3] == ("docker", "network", "create"):
            self.created = True
            return subprocess.CompletedProcess(args, 0)
        if args[0] != "iptables":
            return subprocess.CompletedProcess(args, 0)
        assert args[1] == "-w", f"iptables must be called with -w: {args}"
        op, chain, rest = args[2], args[3], args[4:]
        absent = subprocess.CompletedProcess(args, self.check_status or 1)
        if op == "-N":
            assert chain not in self.chains
            self.chains[chain] = []
        elif op == "-S":
            if chain not in self.chains:
                return absent
            lines = [f"-A {chain} {shlex.join(rule)}" for rule in self.chains[chain]]
            return subprocess.CompletedProcess(args, 0, "\n".join([f"-N {chain}", *lines]) + "\n")
        elif op == "-C":
            if chain not in self.chains or tuple(rest) not in self.chains[chain]:
                return absent
        elif op == "-I":
            self.chains[chain].insert(int(rest[0]) - 1, tuple(rest[1:]))
        elif op == "-A":
            self.chains[chain].append(tuple(rest))
        elif op == "-D":
            del self.chains[chain][int(rest[0]) - 1]
        else:
            raise AssertionError(f"unexpected iptables call: {args}")
        return subprocess.CompletedProcess(args, 0)


def test_setup_builds_the_chain_and_puts_the_jump_first(tmp_path, capsys):
    config = Config(home=tmp_path, desktop_mode="local")
    runner = Runner()
    runner.chains["INPUT"] = [("-j", "ACCEPT")]
    runner.chains["DOCKER-USER"] = [("-j", "RETURN")]
    setup(config, runner)
    assert runner.chains[CHAIN] == rules(SUBNET)
    assert runner.chains["DOCKER-USER"] == [jump(SUBNET), ("-j", "RETURN")]
    assert runner.chains["INPUT"] == [jump(SUBNET), ("-j", "ACCEPT")]
    assert ("docker", "network", "create", "--driver", "bridge", "--ipv6=false",
            "--subnet", SUBNET, "dom-desktop") in runner.commands
    assert capsys.readouterr().out.count("iptables -w") == len(runner.mutations())


def test_setup_is_idempotent(tmp_path, capsys):
    config = Config(home=tmp_path, desktop_mode="local")
    runner = Runner()
    setup(config, runner)
    capsys.readouterr()
    runner.commands.clear()
    setup(config, runner)
    assert runner.mutations() == [] and not any(cmd[1:3] == ("network", "create") for cmd in runner.commands)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("before", [
    [("-j", "RETURN"), jump(SUBNET)],
    [("-j", "RETURN"), jump(SUBNET), ("-j", "DROP"), jump(SUBNET)],
    [("-j", "RETURN")],
])
def test_setup_moves_a_displaced_jump_back_to_rule_one(tmp_path, before):
    runner = Runner()
    runner.chains["DOCKER-USER"] = list(before)
    runner.chains["INPUT"] = list(before)
    setup(Config(home=tmp_path, desktop_mode="local"), runner)
    for parent in ("DOCKER-USER", "INPUT"):
        others = [rule for rule in before if rule != jump(SUBNET)]
        assert runner.chains[parent] == [jump(SUBNET), *others]


def test_setup_completes_a_partially_built_chain_in_order(tmp_path):
    runner = Runner()
    runner.chains[CHAIN] = [rules(SUBNET)[0], rules(SUBNET)[4]]
    setup(Config(home=tmp_path, desktop_mode="local"), runner)
    assert runner.chains[CHAIN] == rules(SUBNET)


def test_setup_never_leaves_the_jump_missing_while_moving_it(tmp_path):
    runner = Runner()
    runner.chains["DOCKER-USER"] = [("-j", "RETURN"), jump(SUBNET)]
    seen = []
    original = runner.__call__

    def watch(args, **kwargs):
        result = original(args, **kwargs)
        if tuple(args)[:3] == ("iptables", "-w", "-D"):
            seen.append(jump(SUBNET) in runner.chains["DOCKER-USER"])
        return result

    setup(Config(home=tmp_path, desktop_mode="local"), watch)
    assert seen == [True]


def test_check_runs_the_probe_in_a_throwaway_container_and_never_pulls(tmp_path):
    runner = Runner()
    check(Config(home=tmp_path, desktop_mode="local"), "192.0.2.1", run=runner)
    cmd, = runner.commands
    assert cmd == ("docker", "run", "--rm", "--pull=never", "--network", "dom-desktop", "alpine:3.22",
                   "sh", "-c", PROBE_SCRIPT, "probe", "3", "1.1.1.1:443", "192.0.2.1:443")


@pytest.mark.parametrize(("status", "message"), [(7, "can reach"), (8, "control"), (125, "could not verify")])
def test_check_reports_each_probe_outcome(tmp_path, status, message):
    with pytest.raises(ValueError, match=message):
        check(Config(home=tmp_path, desktop_mode="local"), "192.0.2.1", run=lambda args, **kw: subprocess.CompletedProcess(args, status))


def test_desktop_cli_accepts_home(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[desktop]\nmode = "local"\n')
    seen = []
    monkeypatch.setattr("dom.cli.desktop_network.setup", lambda config: seen.append(config.home))
    assert main(["desktop-setup", str(tmp_path), "--yes"]) == 0
    assert seen == [tmp_path]


@pytest.mark.parametrize("status", [2, 4, 127])
def test_only_status_one_means_the_rule_is_absent(tmp_path, status):
    runner = Runner()
    runner.check_status = status
    with pytest.raises(subprocess.CalledProcessError):
        setup(Config(home=tmp_path, desktop_mode="local"), runner)
    assert runner.mutations() == []
