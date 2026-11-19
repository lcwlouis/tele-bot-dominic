import errno

import pytest

import seccomp_sim as sim
from dom.plugins import runner

# Linux syscall numbers: aarch64 uses the generic table, x86_64 arch/x86/entry/syscalls/syscall_64.tbl,
# armv7l arch/arm/tools/syscall.tbl (EABI).
SOCKETS = {
    "aarch64": {"read": 63, "socket": 198, "connect": 203, "bind": 200, "listen": 201, "accept": 202, "accept4": 242},
    "x86_64": {"read": 0, "socket": 41, "connect": 42, "bind": 49, "listen": 50, "accept": 43, "accept4": 288},
    "armv7l": {"read": 3, "socket": 281, "connect": 283, "bind": 282, "listen": 284, "accept": 285, "accept4": 366},
}
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK, AF_PACKET = 1, 2, 10, 16, 17
EPERM = errno.EPERM


def verdict(machine, network, name, *args):
    """errno for a denied call, otherwise the raw seccomp action."""
    action = sim.run(runner._seccomp(network, machine), machine, SOCKETS[machine][name], args)
    return sim.errno_of(action) or action


@pytest.mark.parametrize("machine", SOCKETS)
def test_offline_filter_refuses_every_socket(machine):
    for domain in (AF_UNIX, AF_INET, AF_INET6, AF_NETLINK):
        assert verdict(machine, False, "socket", domain) == EPERM
    assert verdict(machine, False, "read") == sim.RET_ALLOW


@pytest.mark.parametrize("machine", SOCKETS)
def test_network_filter_allows_only_internet_clients(machine):
    for domain in (AF_INET, AF_INET6):
        assert verdict(machine, True, "socket", domain) == sim.RET_ALLOW
    for domain in (AF_UNIX, AF_NETLINK, AF_PACKET):
        assert verdict(machine, True, "socket", domain) == EPERM
    assert verdict(machine, True, "connect") == sim.RET_ALLOW
    assert verdict(machine, True, "read") == sim.RET_ALLOW
    for server_call in ("bind", "listen", "accept", "accept4"):
        assert verdict(machine, True, server_call) == EPERM


def test_unknown_architecture_has_no_filter():
    with pytest.raises(runner.PluginsUnavailable):
        runner._seccomp(False, "riscv64")


def test_foreign_architecture_is_killed():
    program = runner._seccomp(True, "aarch64")
    assert sim.run(program, "x86_64", 0) == sim.RET_KILL
