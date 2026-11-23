"""Evaluate real socket peers. Forwarding headers never participate."""

import asyncio
from ipaddress import ip_address, ip_network, IPv6Address
import socket

# Fixed proxy/bridge defaults plus independently configured site-specific ranges.
ALWAYS_DENY = tuple(map(ip_network, ("172.16.0.0/12", "192.168.65.0/24")))


def address(value):
    result = ip_address(value.split("%", 1)[0])
    if isinstance(result, IPv6Address) and result.ipv4_mapped is not None:
        return result.ipv4_mapped
    return result


class Sources:
    def __init__(self, config, local_addresses=()):
        self.static = []
        self.names = []
        for source in config.allowed_sources:
            if source.lower().rstrip(".").endswith(".local"):
                self.names.append(source.rstrip("."))
            else:
                self.static.append(ip_network(source, strict=False))
        self.resolved = set()
        self.local_addresses = {address(value) for value in (*local_addresses, config.bind_host)}
        self.denied = (*ALWAYS_DENY, *(ip_network(v, strict=False) for v in config.denied_sources))

    async def refresh(self):
        async def resolve(name):
            try:
                return await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                    name, None, type=socket.SOCK_STREAM
                ), 5)
            except (OSError, TimeoutError):
                return ()

        results = await asyncio.gather(*(resolve(name) for name in self.names))
        self.resolved = {address(entry[4][0]) for result in results for entry in result}

    def permits(self, peer):
        if not peer:
            return False
        try:
            ip = address(peer[0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip in self.local_addresses
                or any(ip in net for net in self.denied)):
            return False
        return ip in self.resolved or any(ip in net for net in self.static)
