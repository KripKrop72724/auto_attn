from sqlalchemy.orm import sessionmaker

from zk_zone_agent.db import Base, DeviceDiscoveryResult, create_sqlite_engine
from zk_zone_agent.discovery import DiscoveryService
from zk_zone_agent.network_scanner import ScanCandidate


class _FakeScanner:
    def __init__(self):
        self.results = [
            ScanCandidate(
                ip="192.168.110.137",
                port=4370,
                open=True,
                subnet="192.168.110.0/24",
                interface_name="Ethernet",
            )
        ]

    def scan(self, **_kwargs):
        return self.results

    def discover_subnets(self):
        return []


def test_discovery_persists_candidates_and_marks_unreachable(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    scanner = _FakeScanner()
    service = DiscoveryService(session_factory=session_factory, scanner=scanner)

    service.run_scan(source="AUTO", subnets=["192.168.110.0/24"])
    with session_factory() as session:
        candidate = session.query(DeviceDiscoveryResult).one()
        assert candidate.ip == "192.168.110.137"
        assert candidate.status == "NEEDS_COMM_KEY"

    scanner.results = []
    service.run_scan(source="AUTO", subnets=["192.168.110.0/24"])

    with session_factory() as session:
        candidate = session.query(DeviceDiscoveryResult).one()
        assert candidate.status == "UNREACHABLE"
        assert candidate.consecutive_failures == 1
