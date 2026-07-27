"""Shape of the architecture-critique tables (#1036 Part 2 / #963).

Source/metadata-level assertions (no DB) that lock the table + column names
the serializers, service, and migration all depend on, so a rename can't
silently drift them apart. The row-level round-trip lives in the integration
tier alongside the service.
"""

from terrapod.db.models import ArchitectureCritique, ArchitectureCritiqueMessage


class TestArchitectureCritiqueModel:
    def test_tablename_and_columns(self):
        assert ArchitectureCritique.__tablename__ == "architecture_critiques"
        cols = set(ArchitectureCritique.__table__.columns.keys())
        assert {
            "id",
            "workspace_id",
            "state_version_id",
            "state_serial",
            "status",
            "architecture",
            "risk_level",
            "findings",
            "deferred",
            "model",
            "input_tokens",
            "output_tokens",
            "error_message",
            "created_at",
            "updated_at",
        } <= cols

    def test_one_critique_per_state_version(self):
        # Regenerating over the same state version upserts, not duplicates.
        uniques = {
            tuple(c.name for c in con.columns)
            for con in ArchitectureCritique.__table__.constraints
            if con.__class__.__name__ == "UniqueConstraint"
        }
        assert ("state_version_id",) in uniques

    def test_state_version_fk_cascades(self):
        fks = {
            fk.column.table.name: fk.ondelete for fk in ArchitectureCritique.__table__.foreign_keys
        }
        # Old critiques CASCADE away with their state version when pruned.
        assert fks.get("state_versions") == "CASCADE"
        assert fks.get("workspaces") == "CASCADE"


class TestArchitectureCritiqueMessageModel:
    def test_tablename_and_role_constraint(self):
        assert ArchitectureCritiqueMessage.__tablename__ == "architecture_critique_messages"
        checks = [
            str(con.sqltext)
            for con in ArchitectureCritiqueMessage.__table__.constraints
            if con.__class__.__name__ == "CheckConstraint"
        ]
        assert any("role" in c for c in checks)

    def test_cascades_from_critique(self):
        fks = {
            fk.column.table.name: fk.ondelete
            for fk in ArchitectureCritiqueMessage.__table__.foreign_keys
        }
        assert fks.get("architecture_critiques") == "CASCADE"
