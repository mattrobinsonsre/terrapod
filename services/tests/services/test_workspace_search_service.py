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

    def test_all_true_without_exclude_matches_everything(self):
        """`all: true` with no exclusions returns the base query and never
        raises, even with no other dimension set."""
        q = build_workspace_query(WorkspaceFilter(all=True))
        # Base query: SELECT over workspaces, ordered by name, no WHERE.
        assert q.whereclause is None
        compiled = str(q)
        assert "workspaces" in compiled
        assert "ORDER BY" in compiled.upper()

    def test_all_true_ignores_other_dimensions(self):
        """`all` ignores the positive dimensions; only `exclude` narrows it."""
        q = build_workspace_query(WorkspaceFilter(all=True, name_prefix="prod-", locked=True))
        assert q.whereclause is None

    def test_all_true_still_applies_exclude(self):
        q = build_workspace_query(
            WorkspaceFilter(all=True, exclude=[WorkspaceFilter(name_glob="*-keep")])
        )
        assert q.whereclause is not None
        assert "NOT" in str(q.whereclause)

    def test_exclude_alone_is_not_a_selector(self):
        with pytest.raises(WorkspaceFilterError) as e:
            build_workspace_query(WorkspaceFilter(exclude=[WorkspaceFilter(name_glob="*-keep")]))
        assert "at least one selector" in str(e.value)

    def test_exclude_alone_via_parse_filter_raises(self):
        wf = parse_filter({"exclude": [{"name-glob": "*-keep"}]})
        with pytest.raises(WorkspaceFilterError) as e:
            build_workspace_query(wf)
        assert "at least one selector" in str(e.value)


# ── exclude ──────────────────────────────────────────────────────────────


class TestExclude:
    def test_exclude_adds_not_clause(self):
        q = build_workspace_query(
            WorkspaceFilter(
                labels={"team": "platform"}, exclude=[WorkspaceFilter(name_glob="*-keep")]
            )
        )
        compiled = str(q)
        assert "workspaces.labels" in compiled
        assert "NOT" in compiled

    def test_exclude_entries_are_ored_and_entry_dimensions_anded(self):
        q = build_workspace_query(
            WorkspaceFilter(
                labels={"team": "platform"},
                exclude=[
                    WorkspaceFilter(name_glob="a*", locked=True),
                    WorkspaceFilter(execution_backend="pulumi"),
                ],
            )
        )
        compiled = str(q.whereclause)
        assert " OR " in compiled
        assert "workspaces.locked" in compiled
        assert "workspaces.execution_backend" in compiled

    def test_exclude_is_null_safe_for_nullable_columns(self):
        """A NULL vcs_connection_id must not be dropped by `NOT (col = x)`."""
        cid = uuid.uuid4()
        q = build_workspace_query(
            WorkspaceFilter(locked=False, exclude=[WorkspaceFilter(vcs_connection_id=f"vcs-{cid}")])
        )
        assert "IS NOT true" in str(q.whereclause)

    def test_exclude_bad_uuid_raises(self):
        with pytest.raises(WorkspaceFilterError):
            build_workspace_query(
                WorkspaceFilter(
                    locked=True, exclude=[WorkspaceFilter(workspace_ids=["ws-not-a-uuid"])]
                )
            )

    def test_empty_entry_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            build_workspace_query(WorkspaceFilter(locked=True, exclude=[WorkspaceFilter()]))
        assert "exclude[0] sets no selector" in str(e.value)

    def test_nested_entry_raises(self):
        nested = WorkspaceFilter(name_glob="x*", exclude=[WorkspaceFilter(locked=True)])
        with pytest.raises(WorkspaceFilterError):
            build_workspace_query(WorkspaceFilter(locked=True, exclude=[nested]))
        with pytest.raises(WorkspaceFilterError):
            build_workspace_query(
                WorkspaceFilter(locked=True, exclude=[WorkspaceFilter(name_glob="x*", all=True)])
            )

    def test_explicit_dimensions_ignore_exclude(self):
        wf = WorkspaceFilter(exclude=[WorkspaceFilter(locked=True)])
        assert wf._explicit_dimensions() == []


class TestParseExclude:
    def test_parses_entries(self):
        wf = parse_filter(
            {
                "labels": {"team": "platform"},
                "exclude": [{"name-glob": "x*", "terraform_version": "1.5"}, {"locked": True}],
            }
        )
        assert [e.name_glob for e in wf.exclude] == ["x*", None]
        assert wf.exclude[0].engine_version == "1.5"
        assert wf.exclude[1].locked is True

    def test_default_is_empty(self):
        assert parse_filter({"locked": True}).exclude == []

    def test_empty_entry_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            parse_filter({"locked": True, "exclude": [{}]})
        assert "exclude[0] sets no selector" in str(e.value)

    def test_blank_name_prefix_entry_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            parse_filter({"locked": True, "exclude": [{"name_prefix": ""}]})
        assert "exclude[0] sets no selector" in str(e.value)

    def test_blank_owner_email_entry_is_valid(self):
        wf = parse_filter({"locked": True, "exclude": [{"owner_email": ""}]})
        assert wf.exclude[0].owner_email == ""

    def test_nested_exclude_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            parse_filter({"locked": True, "exclude": [{"name-glob": "x*", "exclude": [{}]}]})
        assert "exclude[0]" in str(e.value)

    def test_all_in_entry_raises(self):
        with pytest.raises(WorkspaceFilterError) as e:
            parse_filter({"locked": True, "exclude": [{"name-glob": "x*", "all": True}]})
        assert "exclude[0]" in str(e.value)

    def test_unknown_key_in_entry_names_it(self):
        with pytest.raises(WorkspaceFilterError) as e:
            parse_filter({"locked": True, "exclude": [{"bogus-key": "x"}]})
        assert "bogus_key" in str(e.value)
        assert "exclude[0]" in str(e.value)

    def test_non_list_exclude_raises(self):
        with pytest.raises(WorkspaceFilterError):
            parse_filter({"locked": True, "exclude": {"name-glob": "x*"}})

    @pytest.mark.parametrize("bad", ["", {}, 0, False])
    def test_falsy_non_list_exclude_raises(self, bad):
        with pytest.raises(WorkspaceFilterError, match="'exclude' must be a list"):
            parse_filter({"locked": True, "exclude": bad})

    @pytest.mark.parametrize("empty", [None, []])
    def test_null_or_empty_exclude_means_no_exclusion(self, empty):
        assert parse_filter({"locked": True, "exclude": empty}).exclude == []

    def test_non_dict_entry_raises(self):
        with pytest.raises(WorkspaceFilterError):
            parse_filter({"locked": True, "exclude": ["x*"]})

    def test_bad_type_in_entry_raises_filter_error(self):
        with pytest.raises(WorkspaceFilterError):
            parse_filter({"locked": True, "exclude": [{"locked": "definitely-not-a-bool"}]})


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

    def test_engine_version_adds_where(self):
        q = build_workspace_query(WorkspaceFilter(engine_version="1.12"))
        assert "workspaces.engine_version" in str(q)

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
        assert "workspace_agent_pools.agent_pool_id" in str(q)

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
