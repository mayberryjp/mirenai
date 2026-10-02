"""Trusted-source-network filtering for inbound DNS queries.

The DNS server answers only clients whose source address falls inside one of the
configured trusted subnets; queries from anywhere else are dropped without a
reply. An empty subnet set trusts every client, so the feature is opt-in and a
fresh install keeps answering everyone until subnets are added.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from mirenai.logging import get_logger

log = get_logger("dns.network")


@dataclass(frozen=True)
class NetworkFilter:
    """Immutable set of trusted source subnets with a membership test."""

    networks: tuple[IPv4Network | IPv6Network, ...] = ()

    @classmethod
    def from_cidrs(cls, cidrs: Iterable[str]) -> NetworkFilter:
        """Build a filter from CIDR strings, skipping any that don't parse."""
        networks: list[IPv4Network | IPv6Network] = []
        for cidr in cidrs:
            try:
                networks.append(ip_network(cidr, strict=False))
            except ValueError:
                log.warning("ignoring invalid trusted subnet %r", cidr)
        return cls(tuple(networks))

    def is_trusted(self, client_ip: str) -> bool:
        """Return whether ``client_ip`` is allowed to query the resolver.

        With no subnets configured every client is trusted; otherwise the address
        must fall inside one of them. An unparseable address is never trusted.
        """
        if not self.networks:
            return True
        try:
            addr = ip_address(client_ip)
        except ValueError:
            return False
        return any(addr in net for net in self.networks)
