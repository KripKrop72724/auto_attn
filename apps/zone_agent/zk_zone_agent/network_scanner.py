from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class ScanCandidate:
    ip: str
    port: int
    open: bool


class NetworkScanner:
    def discover_subnets(self) -> list[ipaddress.IPv4Network]:
        networks: set[ipaddress.IPv4Network] = set()
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if getattr(addr, "family", None) != socket.AF_INET:
                    continue
                ip = ipaddress.ip_address(addr.address)
                if ip.is_loopback or ip.is_link_local:
                    continue
                netmask = addr.netmask or "255.255.255.0"
                network = ipaddress.ip_network(f"{addr.address}/{netmask}", strict=False)
                networks.add(network)
        return sorted(networks, key=lambda n: str(n))

    def _is_open(self, ip: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    def scan(
        self,
        *,
        subnets: list[str] | None = None,
        port: int = 4370,
        timeout: float = 0.45,
        max_workers: int = 128,
    ) -> list[ScanCandidate]:
        networks = [ipaddress.ip_network(item, strict=False) for item in subnets] if subnets else self.discover_subnets()
        ips = [str(ip) for network in networks for ip in network.hosts()]
        results: list[ScanCandidate] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(self._is_open, ip, port, timeout): ip for ip in ips}
            for future in as_completed(future_map):
                ip = future_map[future]
                if future.result():
                    results.append(ScanCandidate(ip=ip, port=port, open=True))
        return sorted(results, key=lambda item: tuple(int(part) for part in item.ip.split(".")))


network_scanner = NetworkScanner()
