from sqlalchemy.orm import sessionmaker

from zk_zone_agent.audit import AuditLedgerWriter
from zk_zone_agent.db import AuditLedger, Base, create_sqlite_engine


def test_audit_ledger_hash_chains_rows(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'zone.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    writer = AuditLedgerWriter()

    with Session() as session:
        first = writer.append(session, "attendance", "one", {"value": 1})
        second = writer.append(session, "attendance", "two", {"value": 2})
        session.commit()

    with Session() as session:
        rows = session.query(AuditLedger).order_by(AuditLedger.id.asc()).all()
        assert rows[0].row_hash == first.row_hash
        assert rows[1].previous_hash == rows[0].row_hash
        assert rows[1].row_hash == second.row_hash
