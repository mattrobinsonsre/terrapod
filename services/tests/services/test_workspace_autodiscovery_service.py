"""Tests for workspace autodiscovery service (terrapod #283).

The pure-logic functions (`rule_claims_path`, `derive_root_directory`,
`derive_workspace_name`, `_glob_to_regex`) are exercised here without
hitting the DB. The find-or-autocreate path is covered by the API +
integration tests that run against a real session.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.workspace_autodiscovery_service import (
    _is_terraform_file,
    _match_glob,
    derive_root_directory,
    derive_workspace_name,
    preview_for_paths,
    rule_claims_path,
)


def _rule(
    pattern: str = "accounts/*/**/*.tf",
    ignore_patterns: list[str] | None = None,
    name: str = "monorepo",
    name_template: str = "",
    enabled: bool = True,
):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.pattern = pattern
    r.ignore_patterns = ignore_patterns or []
    r.name = name
    r.name_template = name_template
    r.enabled = enabled
    return r


# ── Glob translation ─────────────────────────────────────────────────────


class TestGlobMatching:
    def test_single_star_within_segment(self):
        assert _match_glob("foo/bar.tf", "foo/*.tf")
        assert not _match_glob("foo/sub/bar.tf", "foo/*.tf")  # `*` not crossing /

    def test_double_star_crosses_segments(self):
        assert _match_glob("a/b/c/main.tf", "a/**/*.tf")
        assert _match_glob("a/main.tf", "a/**/*.tf")  # ** matches zero segments
        assert _match_glob("modules/vpc/inner/foo.tf", "modules/**")

    def test_question_mark_single_char(self):
        assert _match_glob("a/b.tf", "a/?.tf")
        assert not _match_glob("a/bb.tf", "a/?.tf")

    def test_character_class(self):
        assert _match_glob("v1/main.tf", "v[123]/main.tf")
        assert not _match_glob("v9/main.tf", "v[123]/main.tf")

    def test_full_path_anchored(self):
        # Patterns are anchored to the whole path.
        assert _match_glob("accounts/alpha/main.tf", "accounts/*/main.tf")
        assert not _match_glob("subdir/accounts/alpha/main.tf", "accounts/*/main.tf")

    def test_double_star_at_end(self):
        assert _match_glob("modules/vpc/main.tf", "modules/**")
        assert _match_glob("modules/vpc/sub/foo.tf", "modules/**")

    def test_literal_segments_escaped(self):
        # Dots inside path segments are literal — not regex metacharacters.
        assert _match_glob("a.b/c.tf", "a.b/*.tf")
        assert not _match_glob("aXb/c.tf", "a.b/*.tf")


# ── Terraform file detection ─────────────────────────────────────────────


class TestIsTerraformFile:
    def test_tf_files_recognised(self):
        assert _is_terraform_file("main.tf")
        assert _is_terraform_file("a/b/main.tf")
        assert _is_terraform_file("variables.tf")
        assert _is_terraform_file("a/b/c.tfvars")
        assert _is_terraform_file("override.tf.json")

    def test_non_tf_files_rejected(self):
        assert not _is_terraform_file("README.md")
        assert not _is_terraform_file("Makefile")
        assert not _is_terraform_file(".github/workflows/ci.yml")
        assert not _is_terraform_file("scripts/main.py")


# ── rule_claims_path ─────────────────────────────────────────────────────


class TestRuleClaimsPath:
    def test_matches_pattern(self):
        rule = _rule(pattern="accounts/*/**/*.tf")
        assert rule_claims_path(rule, "accounts/alpha/network/main.tf")

    def test_pattern_miss(self):
        rule = _rule(pattern="accounts/*/**/*.tf")
        assert not rule_claims_path(rule, "infrastructure/network/main.tf")

    def test_ignore_pattern_excludes(self):
        rule = _rule(
            pattern="**/*.tf",
            ignore_patterns=["modules/**"],
        )
        assert rule_claims_path(rule, "accounts/alpha/main.tf")
        assert not rule_claims_path(rule, "modules/vpc/main.tf")

    def test_non_terraform_file_rejected(self):
        rule = _rule(pattern="**/*")  # very permissive
        assert not rule_claims_path(rule, "accounts/alpha/README.md")

    def test_multiple_ignore_patterns(self):
        rule = _rule(
            pattern="**/*.tf",
            ignore_patterns=["modules/**", "deprecated/**", "**/_archive/**"],
        )
        assert rule_claims_path(rule, "accounts/alpha/main.tf")
        assert not rule_claims_path(rule, "modules/vpc/main.tf")
        assert not rule_claims_path(rule, "deprecated/old/main.tf")
        assert not rule_claims_path(rule, "accounts/alpha/_archive/main.tf")


# ── derive_root_directory ────────────────────────────────────────────────


class TestDeriveRootDirectory:
    def test_nested_file(self):
        assert derive_root_directory("accounts/alpha/network/main.tf") == "accounts/alpha/network"

    def test_top_level_file(self):
        # main.tf at repo root → empty working_directory (matches the
        # workspace model's default — repo root is "").
        assert derive_root_directory("main.tf") == ""

    def test_two_levels(self):
        assert derive_root_directory("a/b/c.tf") == "a/b"


# ── derive_workspace_name ────────────────────────────────────────────────


class TestDeriveWorkspaceName:
    def test_default_template_dashes_path(self):
        rule = _rule(name="monorepo")
        assert derive_workspace_name(rule, "accounts/alpha/network") == "accounts-alpha-network"

    def test_root_directory_empty_falls_back_to_rule_name(self):
        rule = _rule(name="monorepo-root")
        assert derive_workspace_name(rule, "") == "monorepo-root"

    def test_explicit_template_with_path_placeholder(self):
        rule = _rule(name="monorepo", name_template="ws-{path}")
        assert derive_workspace_name(rule, "accounts/alpha/network") == "ws-accounts-alpha-network"

    def test_explicit_template_with_root_placeholder(self):
        # {root} preserves the slashes; sanitiser maps them to dashes.
        rule = _rule(name="monorepo", name_template="prefix.{root}")
        assert derive_workspace_name(rule, "accounts/alpha") == "prefix-accounts-alpha"

    def test_sanitisation_drops_invalid_chars(self):
        rule = _rule(name="monorepo", name_template="ws/{path}@latest")
        # `/` and `@` are not allowed in workspace names; collapse to dashes.
        result = derive_workspace_name(rule, "a/b")
        assert result == "ws-a-b-latest"

    def test_truncation_to_90_chars(self):
        # Workspace names are capped at 90 chars (matches the schema).
        long_path = "a/" + "verylongsegment/" * 10  # well over 90 chars when dashed
        rule = _rule(name="monorepo")
        result = derive_workspace_name(rule, long_path.rstrip("/"))
        assert len(result) <= 90


# ── Preview (#311) ───────────────────────────────────────────────────────


def _preview_db(
    dir_bound: dict[str, uuid.UUID | None] | None = None,
    name_taken: set[str] | None = None,
) -> AsyncMock:
    """Build a mock AsyncSession for `preview_for_paths`, which issues
    two queries in order, mirroring `find_or_autocreate_workspace`:

    1. Stage 1 — workspaces already bound to the rule's
       (connection, repo, working_directory). `dir_bound` maps
       working_directory → autodiscovery_rule_id (or None for a
       manually-created workspace at that directory).
    2. Stage 2 — names already taken by any workspace. `name_taken`
       is the set of colliding names.
    """
    dir_bound = dir_bound or {}
    name_taken = name_taken or set()
    db = AsyncMock()
    r1 = MagicMock()
    r1.all = MagicMock(return_value=list(dir_bound.items()))
    r2 = MagicMock()
    r2.all = MagicMock(return_value=[(n,) for n in name_taken])
    db.execute = AsyncMock(side_effect=[r1, r2])
    return db


class TestPreviewForPaths:
    @pytest.mark.asyncio
    async def test_groups_files_by_directory(self):
        rule = _rule(pattern="accounts/*/**/*.tf")
        files = [
            "accounts/alpha/network/main.tf",
            "accounts/alpha/network/variables.tf",  # same directory; should collapse
            "accounts/beta/compute/main.tf",
        ]
        db = _preview_db({})
        preview = await preview_for_paths(db, rule, files)
        names = sorted(e["working_directory"] for e in preview)
        assert names == ["accounts/alpha/network", "accounts/beta/compute"]

    @pytest.mark.asyncio
    async def test_flags_collision_with_user_workspace(self):
        """The derived name is taken by an unrelated workspace (not bound
        to this directory). `find_or_autocreate_workspace` would raise
        AutodiscoveryNameCollision, so the row is a no-op: colliding but
        NOT existing_autodiscovered.
        """
        rule = _rule(pattern="accounts/*/**/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        db = _preview_db(name_taken={"accounts-alpha-network"})
        preview = await preview_for_paths(db, rule, files)
        assert len(preview) == 1
        assert preview[0]["collision"] is True
        assert preview[0]["existing_autodiscovered"] is False

    @pytest.mark.asyncio
    async def test_flags_existing_autodiscovered(self):
        """The common already-backfilled case — this rule already
        materialised a workspace for this directory. Mirrors Lookup #1
        (reuse-by-directory), so the preview must read the
        directory-bound query, not a name match.
        """
        rule = _rule(pattern="accounts/*/**/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        db = _preview_db(dir_bound={"accounts/alpha/network": rule.id})
        preview = await preview_for_paths(db, rule, files)
        assert preview[0]["collision"] is True
        assert preview[0]["existing_autodiscovered"] is True

    @pytest.mark.asyncio
    async def test_directory_bound_under_different_name_is_noop(self):
        """Regression (#312): a workspace already bound to this
        (connection, repo, directory) under a DIFFERENT name. The scan
        reuses it (creates nothing), so preview must report collision —
        not a phantom "will create". Before the fix, preview only
        checked by name and mispredicted this as creatable, breaking the
        "Provision N workspaces" promise.
        """
        rule = _rule(pattern="accounts/*/**/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        # Directory bound (to this rule) but the derived name is free.
        db = _preview_db(dir_bound={"accounts/alpha/network": rule.id}, name_taken=set())
        preview = await preview_for_paths(db, rule, files)
        assert len(preview) == 1
        assert preview[0]["collision"] is True
        assert preview[0]["existing_autodiscovered"] is True

    @pytest.mark.asyncio
    async def test_directory_bound_to_other_rule_is_noop_not_ours(self):
        """Directory already claimed by a *different* rule (or a manual
        workspace with no rule). Scan reuses it; preview must show a
        no-op that is NOT flagged as discovered by this rule.
        """
        rule = _rule(pattern="accounts/*/**/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        db = _preview_db(dir_bound={"accounts/alpha/network": uuid.uuid4()})
        preview = await preview_for_paths(db, rule, files)
        assert preview[0]["collision"] is True
        assert preview[0]["existing_autodiscovered"] is False

    @pytest.mark.asyncio
    async def test_clean_row_would_be_created(self):
        """Neither directory-bound nor name-taken → scan creates it.
        This is the row the 'Provision N' count must include."""
        rule = _rule(pattern="accounts/*/**/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        db = _preview_db()
        preview = await preview_for_paths(db, rule, files)
        assert preview[0]["collision"] is False
        assert preview[0]["existing_autodiscovered"] is False

    @pytest.mark.asyncio
    async def test_disabled_rule_returns_nothing(self):
        """A disabled rule has nothing to preview — short-circuit the
        whole walk. The /scan endpoint force-enables for explicit user
        action, but /preview should reflect the rule's actual state.
        """
        rule = _rule(enabled=False, pattern="accounts/*/**/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        db = _preview_db({})
        preview = await preview_for_paths(db, rule, files)
        assert preview == []

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty_without_db_query(self):
        """If the matcher rejects everything we shouldn't even query the
        DB for collisions. Saves a round trip on a misconfigured rule
        against a huge repo."""
        rule = _rule(pattern="this-prefix-matches-nothing/*.tf")
        files = ["accounts/alpha/network/main.tf"]
        db = _preview_db({})
        preview = await preview_for_paths(db, rule, files)
        assert preview == []
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignore_pattern_filters_out_matches(self):
        rule = _rule(
            pattern="accounts/*/**/*.tf",
            ignore_patterns=["accounts/*/modules/**"],
        )
        files = [
            "accounts/alpha/network/main.tf",
            "accounts/alpha/modules/vpc/main.tf",  # ignored
        ]
        db = _preview_db({})
        preview = await preview_for_paths(db, rule, files)
        assert len(preview) == 1
        assert preview[0]["working_directory"] == "accounts/alpha/network"
