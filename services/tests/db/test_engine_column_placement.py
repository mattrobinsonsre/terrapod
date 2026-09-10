"""The `engine` discriminator lives on workspaces and nowhere else (#1536).

Migration 697c42296453 first put `engine` on three tables for symmetry —
workspaces, configuration_versions and runs. The configuration-version copy was
never read; the run's copy was read, but only because it was there, and it is
how #1523 happened: it defaulted to terraform and sent a Pulumi workspace's runs
down the Terraform path, reporting success.

Engine is identity — replace-forcing, never edited — so there is nothing to
snapshot. One row owns the fact and everything else joins to it. Pinned because
"store a copy next to where it is read" is attractive and will be proposed
again.
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
        "configuration_versions.engine is back. Read the engine from the workspace "
        "the configuration version belongs to (#1536)."
    )


def test_runs_carry_no_engine() -> None:
    assert "engine" not in Run.__table__.columns, (
        "runs.engine is back. A run's engine is its workspace's: join to it, or take "
        "it from a workspace already loaded. A stored copy is what caused #1523."
    )


def test_the_workspace_is_the_one_place_it_is_stored() -> None:
    assert "engine" in Workspace.__table__.columns


def test_the_migration_agrees_with_the_models() -> None:
    """Or a fresh install grows columns the ORM never maps — invisible until
    something inserts without the server default."""
    assert _migration_tables() == ("workspaces",)
