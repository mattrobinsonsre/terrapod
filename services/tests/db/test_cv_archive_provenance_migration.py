"""Historical archive backfill only admits provably ordinary sources."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

from terrapod.db.models import ConfigurationVersion


def test_archive_provenance_migration_backfill_and_immutability():
    migration = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/a9a7825cce45_cv_archive_provenance.py"
    )
    if not migration.exists():
        migration = Path("/app/alembic/versions/a9a7825cce45_cv_archive_provenance.py")
    spec = importlib.util.spec_from_file_location("cv_archive_provenance_migration", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module, "op", MagicMock()) as op:
        module.upgrade()
    statements = [args.args[0] for args in op.execute.call_args_list]
    assert not any("UPDATE runs" in statement for statement in statements)
    assert not any("UPDATE configuration_versions" in statement for statement in statements)
    immutability = next(statement for statement in statements if "CREATE FUNCTION" in statement)
    assert "IS DISTINCT FROM OLD.archive_provenance" in immutability
    assert "IS DISTINCT FROM OLD.archive_module_id" in immutability
    constraint = op.create_check_constraint.call_args.args[2]
    assert "archive_module_id IS NOT NULL" in constraint
    assert "'admin-only'" in constraint
    assert ConfigurationVersion.__table__.c.archive_module_id.foreign_keys == set()
