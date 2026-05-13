from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


import psutil

from zk_zone_agent.settings import settings


SocketConnector = Callable[[tuple[str, int], float], object]


@dataclass(frozen=True)
class DiscoveredSubnet:
    network: ipaddress.IPv4Network
    interface_name: str
    address: str


@dataclass(frozen=True)
class ScanCandidate:
    ip: str
    port: int
    open: bool
    subnet: str | None = None
    interface_name: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class NetworkScanner:
    excluded_interface_keywords = (
        "docker",
        "vbox",
        "virtualbox",
        "vmware",
        "hyper-v",
        "hyperv",
        "tailscale",
        "zerotier",
        "utun",
        "llw",
        "awdl",
    )

    def __init__(self, connector: SocketConnector | None = None) -> None:
        self._connector = connector or socket.create_connection

    def discover_interfaces(
        self,
        *,
        include_public: bool | None = None,
        max_hosts_per_subnet: int | None = None,
    ) -> list[DiscoveredSubnet]:
        include_public = settings.scan_include_public_subnets if include_public is None else include_public
        max_hosts_per_subnet = max_hosts_per_subnet or settings.scan_max_hosts_per_subnet
        networks: dict[tuple[str, str], DiscoveredSubnet] = {}
        stats = psutil.net_if_stats()
        for interface_name, addrs in psutil.net_if_addrs().items():
            if self._excluded_interface(interface_name):
                continue
            stat = stats.get(interface_name)
            if stat is not None and not stat.isup:
                continue
            for addr in addrs:
                if getattr(addr, "family", None) != socket.AF_INET:
                    continue
                ip = ipaddress.ip_address(addr.address)
                if self._excluded_address(ip, include_public=include_public):
                    continue
                netmask = addr.netmask or "255.255.255.0"
                network = ipaddress.ip_network(f"{addr.address}/{netmask}", strict=False)
                network = self._cap_network_to_host_subnet(network, ip, max_hosts_per_subnet)
                key = (str(network), interface_name)
                networks[key] = DiscoveredSubnet(
                    network=network,
                    interface_name=interface_name,
                    address=str(ip),
                )
        return sorted(networks.values(), key=lambda item: (str(item.network), item.interface_name))

    def discover_subnets(self) -> list[ipaddress.IPv4Network]:
        return [item.network for item in self.discover_interfaces()]

    def _excluded_interface(self, interface_name: str) -> bool:
        lowered = interface_name.lower()
        return any(keyword in lowered for keyword in self.excluded_interface_keywords)

    def _excluded_address(self, ip: ipaddress.IPv4Address, *, include_public: bool) -> bool:
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return True
        if not include_public and not ip.is_private:
            return True
        return False

    def _cap_network_to_host_subnet(
        self,
        network: ipaddress.IPv4Network,
        ip: ipaddress.IPv4Address,
        max_hosts_per_subnet: int,
    ) -> ipaddress.IPv4Network:
        usable_hosts = max(network.num_addresses - 2, 0)
        if usable_hosts <= max_hosts_per_subnet:
            return network
        return ipaddress.ip_network(f"{ip}/24", strict=False)

    def _hosts_for_network(
        self,
        network: ipaddress.IPv4Network,
        *,
        max_hosts_per_subnet: int,
    ) -> list[str]:
        hosts = network.hosts()
        return [str(ip) for _, ip in zip(range(max_hosts_per_subnet), hosts)]

    def _probe(self, ip: str, port: int, timeout: float) -> ScanCandidate:
        started = time.perf_counter()
        try:
            conn = self._connector((ip, port), timeout)
            close = getattr(conn, "close", None)
            if callable(close):
                close()
            return ScanCandidate(
                ip=ip,
                port=port,
                open=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except OSError as exc:
            return ScanCandidate(ip=ip, port=port, open=False, error=str(exc))

    def scan(
        self,
        *,
        subnets: list[str] | None = None,
        port: int | None = None,
        timeout: float | None = None,
        max_workers: int | None = None,
        max_hosts_per_subnet: int | None = None,
    ) -> list[ScanCandidate]:
        port = port or settings.scan_port
        timeout = timeout or settings.scan_timeout_seconds
        max_workers = max_workers or settings.scan_concurrency
        max_hosts_per_subnet = max_hosts_per_subnet or settings.scan_max_hosts_per_subnet
        if subnets:
            subnet_items = [
                DiscoveredSubnet(
                    network=ipaddress.ip_network(item, strict=False),
                    interface_name="manual",
                    address="",
                )
                for item in subnets
            ]
        else:
            subnet_items = self.discover_interfaces(max_hosts_per_subnet=max_hosts_per_subnet)

        targets: dict[str, DiscoveredSubnet] = {}
        for subnet in subnet_items:
            for ip in self._hosts_for_network(subnet.network, max_hosts_per_subnet=max_hosts_per_subnet):
                targets.setdefault(ip, subnet)

        results: list[ScanCandidate] = []
        if not targets:
            return results
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            future_map = {pool.submit(self._probe, ip, port, timeout): ip for ip in targets}
            for future in as_completed(future_map):
                candidate = future.result()
                if candidate.open:
                    subnet = targets[candidate.ip]
                    results.append(
                        ScanCandidate(
                            ip=candidate.ip,
                            port=candidate.port,
                            open=True,
                            subnet=str(subnet.network),
                            interface_name=subnet.interface_name,
                            latency_ms=candidate.latency_ms,
                        )
                    )
        return sorted(results, key=lambda item: tuple(int(part) for part in item.ip.split(".")))


network_scanner = NetworkScanner()
