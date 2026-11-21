"""Operator commands for the Linux desktop network."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shlex
import socket
import subprocess
from collections.abc import Callable

from dom.config import Config, mdns_name
from dom.desktop.tools import offline

LAN_RANGES = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
    "0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15", "224.0.0.0/3",
)
CHAIN = "DOM-DESKTOP"
ESTABLISHED = ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED")
LOCAL = ("-m", "addrtype", "--dst-type", "LOCAL")


def rules(subnet: str) -> list[tuple[str, ...]]:
    """The DOM-DESKTOP chain. DOCKER-USER and INPUT both jump to it for the desktop subnet.

    Traffic addressed to the Docker host never passes through DOCKER-USER, so LOCAL destinations are
    only seen from INPUT; everything else is forwarded traffic.
    """
    return [
        (*LOCAL, *ESTABLISHED, "-j", "ACCEPT"),
        (*LOCAL, "-j", "DROP"),
        (*ESTABLISHED, "-j", "RETURN"),
        ("-d", str(ipaddress.IPv4Network(subnet)), "-j", "RETURN"),
        *[("-d", target, "-j", "DROP") for target in LAN_RANGES],
    ]


def jump(subnet: str) -> tuple[str, ...]:
    return ("-s", str(ipaddress.IPv4Network(subnet)), "-j", CHAIN)


def verify_network(details: dict, subnet: str) -> None:
    """The desktop network must be an IPv4-only bridge with exactly the configured subnet."""
    try:
        subnets = {item["Subnet"] for item in details["IPAM"]["Config"]}
        valid = details["Driver"] == "bridge" and not details["EnableIPv6"] and subnets == {subnet}
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f"the desktop network must be an IPv4-only bridge with subnet {subnet}")


def gateway(details: dict, subnet: str) -> str:
    configured = details["IPAM"]["Config"][0].get("Gateway")
    return str(ipaddress.IPv4Address(configured)) if configured else str(next(ipaddress.IPv4Network(subnet).hosts()))


def _query(run, *args: str) -> subprocess.CompletedProcess:
    """Run an iptables query: status 1 means "absent"; any other failure is an error to surface."""
    result = run(["iptables", "-w", *args], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result


def _change(run, *args: str) -> None:
    command = ["iptables", "-w", *args]
    run(command, check=True)
    print(shlex.join(command))


def _ensure_first(run, parent: str, entry: tuple[str, ...]) -> None:
    """Rule 1 of `parent` is the jump, exactly once. A moved jump is inserted anew before the old one is deleted."""
    listing = _query(run, "-S", parent)
    if listing.returncode:
        raise subprocess.CalledProcessError(listing.returncode, listing.args)
    entries = [tuple(shlex.split(line)[2:]) for line in listing.stdout.splitlines() if line.startswith(f"-A {parent} ")]
    if entries[:1] == [entry] and entry not in entries[1:]:
        return
    stale = [number for number, existing in enumerate(entries, 1) if existing == entry]
    _change(run, "-I", parent, "1", *entry)
    for number in reversed(stale):
        _change(run, "-D", parent, str(number + 1))


def setup(config: Config, run=subprocess.run) -> None:
    settings = config.desktop
    network = settings.network
    subnet = str(ipaddress.IPv4Network(settings.subnet))
    inspect = run(["docker", "network", "inspect", network], capture_output=True, text=True)
    if inspect.returncode:
        run(["docker", "network", "create", "--driver", "bridge", "--ipv6=false",
             "--subnet", subnet, network], check=True)
    else:
        verify_network(json.loads(inspect.stdout)[0], subnet)

    if _query(run, "-S", CHAIN).returncode:
        _change(run, "-N", CHAIN)
    for position, args in enumerate(rules(subnet), 1):
        if _query(run, "-C", CHAIN, *args).returncode:
            _change(run, "-I", CHAIN, str(position), *args)
    for parent in ("DOCKER-USER", "INPUT"):
        _ensure_first(run, parent, jump(subnet))


# Arguments: SECONDS CONTROL TARGET... Exit status 0: every target timed out and the control connected.
# 7: a target connected or failed fast (reachable). 8: the control did not connect, so a timeout proves nothing.
# Each target is judged on its own. `timeout` stops an nc that ignores -w.
PROBE_SECONDS = 3
PROBE_REACHABLE = 7
PROBE_NO_CONTROL = 8
PROBE_SCRIPT = """
w=$1; control=$2; shift 2
try() {
  host=${1%:*}; port=${1##*:}
  start=$(date +%s)
  timeout $((w + 2)) nc -z -w "$w" "$host" "$port" >/dev/null 2>&1 && return 0
  [ $(( $(date +%s) - start )) -ge "$w" ] && return 1
  return 2
}
try "$control" || exit 8
dir=$(mktemp -d)
n=0
for target in "$@"; do
  n=$((n + 1))
  ( try "$target"; echo $? > "$dir/$n" ) &
done
wait
for result in "$dir"/*; do
  [ "$(cat "$result")" = 1 ] || exit 7
done
"""


def resolve_ipv4(host: str) -> list[str]:
    """Every IPv4 address the host's resolver (mDNS included, through nss-mdns) gives for `host`."""
    try:
        answers = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({answer[4][0] for answer in answers}, key=ipaddress.IPv4Address)


def lan_targets(probes: list[str], resolve: Callable[[str], list[str]] | None = None) -> list[str]:
    """Probe targets as IPv4 literals. A `.local` name becomes one target per address it resolves to
    right now; the probe container cannot resolve mDNS itself. An unresolvable name fails closed."""
    resolve = resolve or resolve_ipv4
    targets = []
    for probe in probes:
        host, _, port = probe.rpartition(":")
        if not mdns_name(host):
            targets.append(probe)
            continue
        addresses = resolve(host)
        if not addresses:
            raise ValueError(f"could not resolve lan_probes host {host}")
        targets += [f"{address}:{port}" for address in addresses]
    return targets


def probe_command(config: Config, *targets: str) -> list[str]:
    """A throwaway container on the desktop network. It never pulls the image."""
    settings = config.desktop
    return [
        "docker", "run", "--rm", "--pull=never", "--network", settings.network, settings.probe_image,
        "sh", "-c", PROBE_SCRIPT, "probe", str(PROBE_SECONDS), settings.probe_control, *targets,
    ]


def check(
    config: Config, lan_address: str, lan_port: int = 443, public_address: str | None = None, *,
    run=subprocess.run, resolve: Callable[[str], list[str]] | None = None,
) -> None:
    """Prove the desktop network cannot reach a known LAN service, given by IPv4 address or `<name>.local`."""
    if not mdns_name(lan_address):
        lan_address = str(ipaddress.IPv4Address(lan_address))
    if not 1 <= lan_port <= 65535:
        raise ValueError("provide a valid LAN port")
    if public_address is not None:
        config = config.model_copy(update={"desktop": config.desktop.model_copy(update={
            "probe_control": f"{ipaddress.IPv4Address(public_address)}:443",
        })})
    targets = lan_targets([f"{lan_address}:{lan_port}"], resolve)
    code = run(probe_command(config, *targets)).returncode
    if code == PROBE_REACHABLE:
        raise ValueError(f"the desktop network can reach {lan_address}:{lan_port}; run dom desktop-setup")
    if code == PROBE_NO_CONTROL:
        raise ValueError("the probe could not reach its control address, so the result proves nothing")
    if code:
        raise ValueError("could not verify the desktop firewall (the probe container failed to run)")
    print(f"{lan_address}:{lan_port} timed out and the control connected: the sandbox cannot reach it.")


# The self-directed work guard (W13). It runs INSIDE the desktop container, through the sandbox's own shell,
# right after a fresh start and before any web page is loaded, so nothing the task meets can answer for it.
# Argument: JSON {seconds, control, targets, gateway_ports}. Prints JSON {results: {target: outcome}, gateway}.
# Outcomes: open (connected), refused (a host answered), timeout (no answer), unreachable (failed fast).
GUARD_SCRIPT = """
import json, socket, sys, threading, time
spec = json.loads(sys.argv[1])
targets = list(spec["targets"])
gateway = None
try:
    with open("/proc/net/route") as routes:
        for line in routes.read().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and int(fields[1], 16) == 0:
                gateway = socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
                break
except (OSError, ValueError):
    pass
if gateway:
    targets += [gateway + ":" + str(port) for port in spec["gateway_ports"]]
results = {}
def probe(target):
    host, _, port = target.rpartition(":")
    start = time.monotonic()
    try:
        socket.create_connection((host, int(port)), timeout=spec["seconds"]).close()
        outcome = "open"
    except socket.timeout:
        outcome = "timeout"
    except ConnectionRefusedError:
        outcome = "refused"
    except OSError:
        outcome = "timeout" if time.monotonic() - start >= spec["seconds"] else "unreachable"
    results[target] = outcome
threads = [threading.Thread(target=probe, args=(target,)) for target in [spec["control"], *targets]]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(json.dumps({"results": results, "gateway": gateway}))
"""
# Private addresses sampled on top of the known LAN services. Nothing need listen there, so only an answer
# (open or refused) counts against the guard; known services must time out, as the DOM-DESKTOP DROP rules make them.
# The first address of each private, shared and link-local range: where routers and gateways usually sit.
SAMPLED = tuple(f"{ipaddress.IPv4Network(block)[1]}:80" for block in LAN_RANGES[:5])
GUARD_PORTS = (22,)  # plus the dashboard port; the Docker host and the gateway are probed on these


def host_address(control: str) -> str | None:
    """This machine's own LAN IPv4 address: the source address it would use to reach `control`.
    Connecting a UDP socket sends nothing. None when there is no route or the address is not private."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((control.rpartition(":")[0], 9))
            address = probe.getsockname()[0]
    except OSError:
        return None
    ip = ipaddress.IPv4Address(address)
    return address if any(ip in ipaddress.IPv4Network(block) for block in LAN_RANGES[:5]) else None


async def host_answers(target: str, seconds: float = PROBE_SECONDS) -> bool:
    """Whether `target` answers a TCP connect from this host (connected or refused). A LAN probe that does not
    answer from here, such as a device that is switched off, proves nothing when it times out in the sandbox."""
    host, _, port = target.rpartition(":")
    try:
        async with asyncio.timeout(seconds):
            _, writer = await asyncio.open_connection(host, int(port))
    except ConnectionRefusedError:
        return True
    except (OSError, TimeoutError, ValueError):
        return False
    writer.close()
    return True


async def answering(targets: list[str]) -> set[str]:
    """The targets that answer from this host, checked in parallel."""
    answers = await asyncio.gather(*(host_answers(target) for target in targets))
    return {target for target, answered in zip(targets, answers) if answered}


def judge(
    results: object, control: str, lan: list[str], hosts: list[str], answered: set[str], sampled: tuple[str, ...] = SAMPLED,
) -> str | None:
    """Why the guard is not verified, or None. The public control must connect; no known target (`lan`, the LAN
    services, and `hosts`, the Docker host's own addresses) may answer in the sandbox; a timeout counts as proof
    only for a target that answers from the Docker host (`answered`), so every host address and at least one LAN
    service must prove the block; and no sampled private address may answer."""
    if not isinstance(results, dict):
        return "the network check inside the sandbox returned nothing usable"
    if results.get(control) != "open":
        return f"the sandbox could not reach the public test address {control}, so the check proves nothing"
    for target in (*lan, *hosts):
        if results.get(target) != "timeout":
            return f"the sandbox can reach {target} on the local network"
    for target in hosts:
        if target not in answered:
            return f"{target} does not answer from the Docker host either, so its timeout proves nothing"
    if not any(target in answered for target in lan):
        return "no lan_probes service answers from the Docker host, so the check proves nothing; probe one that is always on"
    for target in sampled:
        if results.get(target) not in ("timeout", "unreachable"):
            return f"the sandbox can reach {target} on the local network"
    return None


# Run under the exact offline wrapper the self-directed shell uses: it must not connect where a plain connect does.
OFFLINE_SCRIPT = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
    print("online")
except OSError:
    print("offline")
"""


async def verify_inside(client, config: Config, *, address: str | None = None) -> str | None:
    """Check from inside a freshly started desktop container that LAN and private addresses are unreachable
    while a public address is reachable, and that the offline wrapper has no network. Returns why the guard is
    not verified, or None when it is."""
    settings = config.desktop
    try:
        lan = await asyncio.to_thread(lan_targets, settings.lan_probes)
    except ValueError as exc:
        return f"could not resolve a LAN probe ({exc})"
    if not lan:
        return "set [desktop] lan_probes so the guard has a known LAN service to test"
    ports = (*GUARD_PORTS, config.web.port)
    host = address or await asyncio.to_thread(host_address, settings.probe_control)
    hosts = [f"{host}:{port}" for port in ports] if host else []
    control = settings.probe_control
    spec = {"seconds": PROBE_SECONDS, "control": control, "targets": [*lan, *hosts, *SAMPLED], "gateway_ports": ports}
    try:
        async with asyncio.timeout(60):
            result = await client.bash(f"/usr/bin/python3 -c {shlex.quote(GUARD_SCRIPT)} {shlex.quote(json.dumps(spec))}")
        report = json.loads(result["stdout"])
        results, gateway = report["results"], report["gateway"]
    except Exception:
        return "could not run the network check inside the sandbox"
    if not isinstance(gateway, str) or not gateway:
        return "could not find the sandbox's default gateway"
    hosts += [f"{gateway}:{port}" for port in ports]
    if reason := judge(results, control, lan, hosts, await answering([*lan, *hosts])):
        return reason
    control_host, _, control_port = control.rpartition(":")
    check = f"/usr/bin/python3 -c {shlex.quote(OFFLINE_SCRIPT)} {shlex.quote(control_host)} {shlex.quote(control_port)}"
    try:
        async with asyncio.timeout(30):
            result = await client.bash(offline(check))
        verdict = result["stdout"].strip() if isinstance(result, dict) and result.get("exit_code") == 0 else ""
    except Exception:
        verdict = ""
    if verdict != "offline":
        return "the offline shell wrapper " + ("has network" if verdict == "online" else "could not be verified")
    return None
