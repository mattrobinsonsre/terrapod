"""Tests for the server-side workspace search/selection engine (#318).

Pure-logic — no DB. We construct `WorkspaceFilter` directly and inspect
the generated SQLAlchemy `Select` (compiled string / whereclause) rather
than executing it.
"""

import uuid

import pytest

from terrapod.services.workspace_search_service import (
    WorkspaceFilter,
    WorkspaceFilterError,
    build_workspace_query,
    parse_filter,
)

# ── parse_filter ─────────────────────────────────────────────────────────


class TestParseFilter:
    def test_none_raises(self):
        with pytest.raises(WorkspaceFilterError):
            parse_filter(None)

    def test_empty_dict_raises(self):
        with pytest.raises(WorkspaceFilterError):
            parse_filter({})

    def test_non_dict_raises(self):
        with pytest.raises(WorkspaceFilterError):
            parse_filter(["not", "a", "dict"])

    def test_unknown_key_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            parse_filter({"bogus-selector": "x"})
        assert "unknown filter key" in str(e.value)
        assert "bogus_selector" in str(e.value)

    def test_hyphenated_keys_normalised(self):
        """JSON:API-ish hyphenated keys map onto the snake_case model."""
        wf = parse_filter(
            {
                "name-prefix": "prod-",
                "execution-backend": "tofu",
                "agent-pool-id": "apool-1",
                "has-vcs": True,
            }
        )
        assert wf.name_prefix == "prod-"
        assert wf.execution_backend == "tofu"
        assert wf.agent_pool_id == "apool-1"
        assert wf.has_vcs is True

    def test_all_true_parses(self):
        wf = parse_filter({"all": True})
        assert wf.all is True

    def test_bad_type_translated_to_filter_error(self):
        """A pydantic validation failure surfaces as WorkspaceFilterError,
        not a raw pydantic ValidationError."""
        with pytest.raises(WorkspaceFilterError):
            parse_filter({"locked": "definitely-not-a-bool"})


# ── build_workspace_query — blast-radius guard ───────────────────────────


class TestBuildQueryGuard:
    def test_no_dimensions_not_all_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            build_workspace_query(WorkspaceFilter())
        assert "at least one selector" in str(e.value)

    def test_empty_collections_count_as_no_dimensions(self):
        """`labels: {}` / `workspace_ids: []` are not real selectors —
        they must not slip past the empty-filter guard."""
        with pytest.raises(WorkspaceFilterError):
            build_workspace_query(WorkspaceFilter(labels={}, workspace_ids=[]))

    def test_all_true_short_circuits_no_error(self):
        """`all: true` returns the base query and never raises, even with
        no other dimension set."""
        q = build_workspace_query(WorkspaceFilter(all=True))
        # Base query: SELECT over workspaces, ordered by name, no WHERE.
        assert q.whereclause is None
        compiled = str(q)
        assert "workspaces" in compiled
        assert "ORDER BY" in compiled.upper()

    def test_all_true_ignores_other_dimensions(self):
        """`all` short-circuits before any dimension is appended."""
        q = build_workspace_query(WorkspaceFilter(all=True, name_prefix="prod-", locked=True))
        assert q.whereclause is None


# ── build_workspace_query — per-dimension WHERE ──────────────────────────


class TestBuildQueryDimensions:
    def test_name_prefix_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(name_prefix="prod-"))
        assert q.whereclause is not None
        assert "lower(workspaces.name) LIKE lower" in str(q)

    def test_name_glob_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(name_glob="*-staging"))
        assert q.whereclause is not None
        assert "workspaces.name" in str(q)

    def test_execution_backend_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(execution_backend="tofu"))
        assert "workspaces.execution_backend" in str(q)

    def test_execution_mode_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(execution_mode="agent"))
        assert "workspaces.execution_mode" in str(q)

    def test_terraform_version_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(terraform_version="1.12"))
        assert "workspaces.terraform_version" in str(q)

    def test_owner_email_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(owner_email="a@example.com"))
        assert "workspaces.owner_email" in str(q)

    def test_drift_status_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(drift_status="drifted"))
        assert "workspaces.drift_status" in str(q)

    def test_locked_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(locked=True))
        assert "workspaces.locked" in str(q)

    def test_labels_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(labels={"team": "platform"}))
        assert q.whereclause is not None
        assert "workspaces.labels" in str(q)

    def test_workspace_ids_adds_where(self):
        wid = uuid.uuid4()
        q = build_workspace_query(WorkspaceFilter(workspace_ids=[f"ws-{wid}"]))
        assert q.whereclause is not None
        assert "workspaces.id IN" in str(q)

    def test_workspace_ids_bad_uuid_raises(self):
        with pytest.raises(WorkspaceFilterError):
            build_workspace_query(WorkspaceFilter(workspace_ids=["ws-not-a-uuid"]))

    def test_agent_pool_id_adds_where(self):
        pid = uuid.uuid4()
        q = build_workspace_query(WorkspaceFilter(agent_pool_id=f"apool-{pid}"))
        assert "workspaces.agent_pool_id" in str(q)

    def test_agent_pool_id_bad_uuid_raises(self):
        with pytest.raises(WorkspaceFilterError):
            build_workspace_query(WorkspaceFilter(agent_pool_id="apool-nope"))

    def test_vcs_connection_id_adds_where(self):
        cid = uuid.uuid4()
        q = build_workspace_query(WorkspaceFilter(vcs_connection_id=f"vcs-{cid}"))
        assert "workspaces.vcs_connection_id" in str(q)

    def test_has_vcs_true_adds_is_not_null(self):
        q = build_workspace_query(WorkspaceFilter(has_vcs=True))
        assert "workspaces.vcs_connection_id IS NOT NULL" in str(q)

    def test_has_vcs_false_adds_is_null(self):
        q = build_workspace_query(WorkspaceFilter(has_vcs=False))
        assert "workspaces.vcs_connection_id IS NULL" in str(q)

    def test_multiple_dimensions_are_anded(self):
        """Two dimensions → both columns appear in the WHERE (AND-combined)."""
        q = build_workspace_query(WorkspaceFilter(execution_backend="tofu", locked=True))
        compiled = str(q)
        assert "workspaces.execution_backend" in compiled
        assert "workspaces.locked" in compiled
        assert " AND " in compiled
