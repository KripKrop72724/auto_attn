from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from zk_add.settings import settings


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str | None = None) -> Engine:
    value = database_url or settings.resolved_database_url
    is_sqlite = value.startswith("sqlite")
    if value.startswith("sqlite:///"):
        Path(value.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    if is_sqlite:
        connect_args = {"check_same_thread": False}
        pool_options = {}
    else:
        connect_args = {
            "connect_timeout": settings.database_connect_timeout_seconds,
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms} "
                f"-c lock_timeout={settings.database_lock_timeout_ms} "
                "-c idle_in_transaction_session_timeout=60000"
            ),
        }
        pool_options = {
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_timeout": settings.database_pool_timeout_seconds,
            "pool_recycle": settings.database_pool_recycle_seconds,
            "pool_use_lifo": True,
        }
    engine = create_engine(
        value,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
        **pool_options,
    )
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


engine = create_database_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def init_db(bind: Engine | None = None) -> None:
    from zk_add import models  # noqa: F401

    Base.metadata.create_all(bind or engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
