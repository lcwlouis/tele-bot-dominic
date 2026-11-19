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


# name: (aarch64, x86_64, armv7l), from the same kernel tables as SOCKETS above.
HARDENED = {
    "ptrace": (117, 101, 26), "bpf": (280, 321, 386), "perf_event_open": (241, 298, 364),
    "userfaultfd": (282, 323, 388), "keyctl": (219, 250, 311), "add_key": (217, 248, 309),
    "request_key": (218, 249, 310), "mount": (40, 165, 21), "umount2": (39, 166, 52),
    "pivot_root": (41, 155, 218), "unshare": (97, 272, 337), "setns": (268, 308, 375),
    "process_vm_readv": (270, 310, 376), "process_vm_writev": (271, 311, 377),
    "open_by_handle_at": (265, 304, 371), "kcmp": (272, 312, 378),
}
# fork, vfork, clone, clone3, socketpair, io_uring_setup
PROCESS = {
    "aarch64": (220, 435, 199, 425), "x86_64": (56, 57, 58, 435, 53, 425), "armv7l": (2, 120, 190, 435, 288, 425),
}
# execve, openat, read, write, mmap, futex, prlimit64 are what bwrap, prlimit and Python themselves need.
ORDINARY = {
    "aarch64": (221, 56, 63, 64, 222, 98, 261), "x86_64": (59, 257, 0, 1, 9, 202, 302),
    "armv7l": (11, 322, 3, 4, 192, 240, 369),
}


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("machine", SOCKETS)
def test_dangerous_syscalls_are_denied(machine, network):
    program = runner._seccomp(network, machine)
    column = list(SOCKETS).index(machine)
    for name, numbers in HARDENED.items():
        action = sim.run(program, machine, numbers[column])
        assert sim.errno_of(action) == EPERM, name
    for number in PROCESS[machine]:
        assert sim.errno_of(sim.run(program, machine, number)) == EPERM, number
    for number in ORDINARY[machine]:
        assert sim.run(program, machine, number) == sim.RET_ALLOW, number


@pytest.mark.parametrize("machine", SOCKETS)
def test_x32_calls_are_denied(machine):
    assert sim.errno_of(sim.run(runner._seccomp(True, machine), machine, 0x40000000 | 1)) == EPERM
