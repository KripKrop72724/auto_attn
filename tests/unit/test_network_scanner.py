import socket
from types import SimpleNamespace

from zk_zone_agent.network_scanner import NetworkScanner


class _DummyConnection:
    def close(self):
        return None


def test_discover_interfaces_filters_and_caps_large_lan(monkeypatch):
    import zk_zone_agent.network_scanner as scanner_module

    monkeypatch.setattr(
        scanner_module.psutil,
        "net_if_stats",
        lambda: {
            "Ethernet": SimpleNamespace(isup=True),
            "Loopback": SimpleNamespace(isup=True),
            "Docker": SimpleNamespace(isup=True),
        },
    )
    monkeypatch.setattr(
        scanner_module.psutil,
        "net_if_addrs",
        lambda: {
            "Ethernet": [
                SimpleNamespace(family=socket.AF_INET, address="192.168.10.44", netmask="255.255.0.0")
            ],
            "Loopback": [
                SimpleNamespace(family=socket.AF_INET, address="127.0.0.1", netmask="255.0.0.0")
            ],
            "Docker": [
                SimpleNamespace(family=socket.AF_INET, address="172.17.0.1", netmask="255.255.0.0")
            ],
        },
    )

    subnets = NetworkScanner().discover_interfaces(max_hosts_per_subnet=254)

    assert [str(item.network) for item in subnets] == ["192.168.10.0/24"]
    assert subnets[0].interface_name == "Ethernet"


def test_scan_returns_open_port_4370_candidates():
    def connector(address, _timeout):
        ip, port = address
        if ip == "192.168.44.2" and port == 4370:
            return _DummyConnection()
        raise OSError("closed")

    scanner = NetworkScanner(connector=connector)
    results = scanner.scan(
        subnets=["192.168.44.0/30"],
        port=4370,
        timeout=0.01,
        max_workers=4,
        max_hosts_per_subnet=2,
    )

    assert [item.ip for item in results] == ["192.168.44.2"]
    assert results[0].port == 4370
    assert results[0].subnet == "192.168.44.0/30"


def test_scan_ignores_closed_ports():
    scanner = NetworkScanner(connector=lambda _address, _timeout: (_ for _ in ()).throw(OSError("closed")))

    assert scanner.scan(subnets=["192.168.45.0/30"], max_hosts_per_subnet=2) == []
