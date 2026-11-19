"""Operator commands for the Linux desktop network."""

from __future__ import annotations

import ipaddress
import json
import shlex
import subprocess

from dom.config import Config

LAN_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
ESTABLISHED = ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED")


def rules(subnet: str) -> list[tuple[str, ...]]:
    source = ("-s", str(ipaddress.IPv4Network(subnet)))
    return [
        ("DOCKER-USER", *source, *ESTABLISHED, "-j", "RETURN"),
        ("DOCKER-USER", *source, "-d", subnet, "-j", "RETURN"),
        *[("DOCKER-USER", *source, "-d", target, "-j", "DROP") for target in LAN_RANGES],
        # Traffic addressed to the Docker host does not pass through DOCKER-USER.
        ("INPUT", *source, *ESTABLISHED, "-j", "ACCEPT"),
        ("INPUT", *source, "-j", "DROP"),
    ]


def setup(config: Config, run=subprocess.run) -> None:
    settings = config.desktop
    network = settings.network
    subnet = str(ipaddress.IPv4Network(settings.subnet))
    inspect = run(["docker", "network", "inspect", network], capture_output=True, text=True)
    if inspect.returncode:
        run(["docker", "network", "create", "--driver", "bridge", "--ipv6=false",
             "--subnet", subnet, network], check=True)
    else:
        details, = json.loads(inspect.stdout)
        subnets = {item["Subnet"] for item in details["IPAM"]["Config"]}
        if details["Driver"] != "bridge" or details["EnableIPv6"] or subnets != {subnet}:
            raise ValueError(f"{network} must be an IPv4-only bridge with subnet {subnet}")

    positions = {"DOCKER-USER": 0, "INPUT": 0}
    for chain, *args in rules(subnet):
        positions[chain] += 1
        result = run(["iptables", "-C", chain, *args], capture_output=True)
        if result.returncode == 0:
            continue
        command = ["iptables", "-I", chain, str(positions[chain]), *args]
        run(command, check=True)
        print(shlex.join(command))


_PROBE = """
import socket
import sys

lan, port, public = sys.argv[1:]
try:
    with socket.create_connection((lan, int(port)), timeout=3):
        sys.exit("LAN probe reached the target")
except TimeoutError:
    print("LAN probe timed out")
except OSError as exc:
    sys.exit(f"LAN probe did not time out: {exc}")

try:
    with socket.create_connection((public, 443), timeout=5):
        print("Public probe answered")
except OSError as exc:
    sys.exit(f"Public probe failed: {exc}")
"""


def check(
    config: Config, lan_address: str, lan_port: int = 443, public_address: str = "1.1.1.1", *, run=subprocess.run,
) -> None:
    lan = ipaddress.IPv4Address(lan_address)
    public = ipaddress.IPv4Address(public_address)
    if not 1 <= lan_port <= 65535 or not public.is_global:
        raise ValueError("provide a valid LAN port and a public IPv4 address")
    settings = config.desktop
    run([
        "docker", "run", "--rm", "--pull=never", "--network", settings.network,
        "--entrypoint", "python3", settings.image, "-c", _PROBE, str(lan), str(lan_port), str(public),
    ], check=True)
