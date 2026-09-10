"""Where the `engine` discriminator lives, and where it must not (#1536).

Migration 697c42296453 first put `engine` on three tables for symmetry —
workspaces, configuration_versions and runs — and nothing ever read the
configuration-version copy. A configuration version is reachable only through
its workspace, which carries the engine, and engine is identity (replace-forcing,
never edited), so there is no mid-flight change a copy would snapshot against.

Pinned because the symmetry argument is attractive and will be made again. The
only copy that earns its place is the run's: the reconciler holds a Run and no
Workspace, every few seconds, over every in-flight run.
"""

from __future__ import annotations

import ast
from pathlib import Path

from terrapod.db.models import ConfigurationVersion, Run, Workspace


def _migration() -> Path:
    for base in (
        Path("/app/alembic/versions"),  # test image
        Path(__file__).resolve().parents[3] / "alembic" / "versions",  # repo
    ):
        candidate = base / "697c42296453_engine_discriminator.py"
        if candidate.exists():
            return candidate
    raise AssertionError("Could not locate the engine-discriminator migration")


def _migration_tables() -> tuple[str, ...]:
    tree = ast.parse(_migration().read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_TABLES" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("_TABLES not found in the engine-discriminator migration")


def test_configuration_versions_carry_no_engine() -> None:
    assert "engine" not in ConfigurationVersion.__table__.columns, (
        "configuration_versions.engine is back. Nothing reads it — read the engine "
        "from the workspace the configuration version belongs to (#1536)."
    )


def test_the_migration_does_not_add_it_either() -> None:
    """The model and the migration must agree, or a fresh install grows a column
    the ORM never maps — invisible until someone inserts without the server default."""
    assert "configuration_versions" not in _migration_tables()


def test_workspaces_and_runs_keep_theirs() -> None:
    """The other half of the acceptance: this was a removal from one table, not
    a general retreat from the discriminator."""
    assert "engine" in Workspace.__table__.columns
    assert "engine" in Run.__table__.columns
    assert set(_migration_tables()) == {"workspaces", "runs"}
