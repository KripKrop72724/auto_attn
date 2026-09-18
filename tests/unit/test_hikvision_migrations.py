"""Exercise both historical create_all bootstrap and existing-install upgrades."""
import importlib.util
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from zk_add.db import Base
import zk_add.hikvision_evidence  # noqa: F401
import zk_add.hikvision_reconciliation  # noqa: F401
import zk_add.hikvision_profiles  # noqa: F401


@pytest.mark.parametrize("existing_install", [False, True])
def test_hikvision_migrations_handle_bootstrap_and_existing_install(existing_install):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        if existing_install:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name.startswith("add_hikvision_"):
                    table.drop(connection)
            for column in ("firmware_family", "terminal_vendor", "terminal_protocol"):
                connection.exec_driver_sql(f"ALTER TABLE add_connectors DROP COLUMN {column}")
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        migrations = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions"
        with Operations.context(context):
            for path in sorted(migrations.glob("20260917_*.py")):
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
        columns = {c["name"] for c in inspect(connection).get_columns("add_connectors")}
        assert {"firmware_family", "terminal_vendor", "terminal_protocol"} <= columns
    engine.dispose()
