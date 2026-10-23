"""add-api starts with `alembic upgrade head`; a second head stops it from starting."""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]


def test_add_migrations_have_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "apps/add_backend/alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Alembic heads {sorted(heads)}: set the newest migration's down_revision to the current head")
