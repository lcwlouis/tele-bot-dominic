"""find_devices: list LAN devices from mDNS/DNS-SD and the neighbour table. Read-only, no scanning.

Each configured LAN host (a model base_url host or a desktop LAN probe) is matched against what is
seen. An IPv4 host is remembered by the MAC addresses and mDNS host names seen at its address; when
that same device shows up at another address, Dom proposes the address change and the owner
approves it through the normal proposals flow. A `.local` name follows the device by itself and
needs no change.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dom.config import host_places, mdns_name
from dom.desktop.network import resolve_ipv4
from dom.tools import Tool, TurnCtx

if TYPE_CHECKING:
    from dom.telegram.handlers import App

log = logging.getLogger(__name__)

KNOWN_KEY = "devices:known"
SOURCE = "find_devices"
BROWSE = ["avahi-browse", "--all", "--resolve", "--terminate", "--parsable"]
NEIGHBOURS = ["ip", "-j", "neigh", "show"]
TIMEOUT = 15
MAX_DEVICES = 100
SKIPPED_INTERFACES = ("docker", "br-", "veth")  # container bridges, not the LAN
DEAD = {"FAILED", "INCOMPLETE"}
_ESCAPE = re.compile(r"\\(\d{3})")


@dataclass
class Device:
    address: str
    names: set[str] = field(default_factory=set)
    macs: set[str] = field(default_factory=set)


@dataclass
class Discovery:
    devices: list[Device]
    notes: list[str]


def _ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _command(run, args: list[str], missing: str) -> str | None:
    try:
        # Device names come from the network: undecodable bytes are replaced, never fatal.
        result = run(args, capture_output=True, text=True, errors="replace", timeout=TIMEOUT)
    except FileNotFoundError:
        raise LookupError(missing) from None
    except subprocess.TimeoutExpired:
        raise LookupError(f"{args[0]} timed out") from None
    return result.stdout if result.returncode == 0 else None


def collect(run=subprocess.run) -> Discovery:
    """IPv4 devices with their mDNS names and MAC addresses, from avahi-browse and `ip neigh`."""
    found: dict[str, Device] = {}
    notes = []
    try:
        browse = _command(run, BROWSE, "mDNS unavailable: avahi-browse is not installed (sudo apt install avahi-utils)")
        for line in (browse or "").splitlines():
            parts = line.split(";")
            if len(parts) < 9 or parts[0] != "=" or parts[2] != "IPv4" or not _ipv4(parts[7]):
                continue
            device = found.setdefault(parts[7], Device(parts[7]))
            service = _ESCAPE.sub(lambda match: chr(int(match[1])), parts[3])
            device.names.update(name for name in (parts[6], service) if name)
    except LookupError as note:
        notes.append(str(note))
    try:
        listing = _command(run, NEIGHBOURS, "neighbour table unavailable: the ip command is missing")
        for entry in json.loads(listing or "[]"):
            address, mac, interface = entry.get("dst", ""), entry.get("lladdr"), entry.get("dev", "")
            if not _ipv4(address) or not mac or DEAD & set(entry.get("state") or []):
                continue
            if interface.startswith(SKIPPED_INTERFACES):
                continue
            found.setdefault(address, Device(address)).macs.add(mac.lower())
    except (LookupError, ValueError, AttributeError) as note:
        notes.append(str(note) if isinstance(note, LookupError) else "could not read the neighbour table")
    ordered = sorted(found.values(), key=lambda device: ipaddress.IPv4Address(device.address))
    return Discovery(ordered, notes)


def _configured(app: App) -> dict[str, list[str]]:
    """Configured LAN hosts from config.toml as it is now (an approved change may be newer than app.config)."""
    try:
        raw = tomllib.loads((app.home.root / "config.toml").read_text())
    except (OSError, ValueError):
        raw = app.config.model_dump()
    hosts = {}
    for host, places in host_places(raw).items():
        if mdns_name(host) or (_ipv4(host) and ipaddress.IPv4Address(host).is_private
                               and not ipaddress.IPv4Address(host).is_loopback):
            hosts[host] = places
    return hosts


def _same(device: Device, known: dict) -> bool:
    names = {name for name in device.names if mdns_name(name)}
    return bool(device.macs & set(known.get("macs", [])) or names & set(known.get("names", [])))


def _identity(device: Device) -> dict:
    return {"macs": sorted(device.macs), "names": sorted(name for name in device.names if mdns_name(name))}


async def _propose(app: App, old: str, device: Device, places: list[str]) -> str:
    for row in await app.proposals.pending():
        body = json.loads(row["body_json"])
        if row["kind"] == "lan_address" and body.get("old") == old and body.get("new") == device.address:
            return f"moved to {device.address}; proposal #{row['id']} is waiting for the owner's approval"
    name = next((name for name in sorted(device.names) if mdns_name(name)), "")
    proposal_id = await app.proposals.create(
        "lan_address", f"LAN address change: {old} → {device.address}",
        {"old": old, "new": device.address, "places": places, "name": name, "macs": sorted(device.macs)}, SOURCE,
    )
    return f"moved to {device.address}; proposed the config change as #{proposal_id} for the owner's approval"


async def find(app: App) -> str:
    discovery = await asyncio.to_thread(collect)
    at = {device.address: device for device in discovery.devices}
    known: dict[str, dict] = json.loads(await app.store.kv_get(KNOWN_KEY) or "{}")
    lines = ["Devices on the LAN (mDNS and the neighbour table; nothing was scanned):"]
    for device in discovery.devices[:MAX_DEVICES]:
        names = ", ".join(sorted(device.names)) or "no name"
        macs = f" [{', '.join(sorted(device.macs))}]" if device.macs else ""
        lines.append(f"- {device.address}: {names}{macs}")
    if len(discovery.devices) > MAX_DEVICES:
        lines.append(f"[{len(discovery.devices) - MAX_DEVICES} more not shown]")
    if not discovery.devices:
        lines.append("- none found")

    lines.append("Configured hosts:")
    for host, places in _configured(app).items():
        where = ", ".join(places)
        if mdns_name(host):
            addresses = await asyncio.to_thread(resolve_ipv4, host)
            status = f"resolves to {', '.join(addresses)}" if addresses else "does not resolve right now"
        else:
            here, before = at.get(host), known.get(host)
            if here is not None and (before is None or _same(here, before)):
                if here.macs or here.names:
                    known[host] = _identity(here)
                status = "seen at this address"
            else:
                moved = next((device for device in discovery.devices
                              if before and device.address != host and _same(device, before)), None)
                if moved is not None:
                    status = await _propose(app, host, moved, places)
                elif here is not None:
                    status = "answered by a different device than before; nothing was changed"
                else:
                    status = "not seen" + ("" if before else " yet, so Dom cannot tell if it moved")
        lines.append(f"- {host} ({where}): {status}")
    await app.store.kv_set(KNOWN_KEY, json.dumps(known, sort_keys=True))
    lines += [f"Note: {note}" for note in discovery.notes]
    return "\n".join(lines)


async def _find_devices(args: dict, ctx: TurnCtx) -> str:
    if ctx.app is None or not ctx.is_owner_dm():
        return "error: find_devices is available only in the owner's DM"
    return await find(ctx.app)


FIND_DEVICES_TOOL = Tool(
    name="find_devices",
    description=(
        "List devices on the home network with their names and addresses, from mDNS and the neighbour table "
        "(read-only; nothing is scanned). Also checks the hosts in Dom's config: if one moved to a new address, "
        "a config change is proposed for the owner to approve."
    ),
    schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_find_devices,
    scopes={"owner_dm"},
)
