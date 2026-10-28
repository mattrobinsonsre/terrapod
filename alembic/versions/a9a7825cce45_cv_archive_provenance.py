"""Classify configuration archive provenance independently of caller source.

Revision ID: a9a7825cce45
Revises: 9f5022744cbf
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a9a7825cce45"
down_revision: str | None = "9f5022744cbf"


def upgrade() -> None:
    op.add_column(
        "configuration_versions",
        sa.Column("archive_provenance", sa.String(20), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "configuration_versions",
        sa.Column("archive_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("runs", sa.Column("module_impact_module_id", postgresql.UUID(as_uuid=True)))
    # Old overrides contain a coordinate, not the original UUID. After a module
    # is deleted the same coordinate can name another module; leave those runs
    # unattributed rather than post their details to the replacement repository.
    # Avoid a full-table rewrite while these new columns hold migration DDL
    # locks. Human reads recognize only legacy ordinary sources on non-catalog
    # workspaces; ambiguous catalog/module-test rows remain unknown and denied.
    op.create_check_constraint(
        "ck_configuration_versions_archive_provenance",
        "configuration_versions",
        "archive_provenance IN ('unknown', 'unprotected', 'protected', 'admin-only') AND "
        "(archive_module_id IS NOT NULL) = (archive_provenance = 'protected')",
    )
    op.execute("""
        CREATE FUNCTION prevent_cv_archive_provenance_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.archive_provenance IS DISTINCT FROM OLD.archive_provenance
               OR NEW.archive_module_id IS DISTINCT FROM OLD.archive_module_id THEN
                RAISE EXCEPTION 'configuration archive provenance is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_cv_archive_provenance_immutable
        BEFORE UPDATE ON configuration_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_cv_archive_provenance_change()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_cv_archive_provenance_immutable ON configuration_versions")
    op.execute("DROP FUNCTION prevent_cv_archive_provenance_change()")
    op.drop_constraint("ck_configuration_versions_archive_provenance", "configuration_versions")
    op.drop_column("configuration_versions", "archive_module_id")
    op.drop_column("configuration_versions", "archive_provenance")
    op.drop_column("runs", "module_impact_module_id")
