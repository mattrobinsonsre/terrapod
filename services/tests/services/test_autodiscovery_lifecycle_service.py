"""Tests for autodiscovery workspace lifecycle (#314).

Covers the pure `classify_dir_changes` reducer and the three async
reconcilers (`reconcile_open_pr`, `reconcile_branch_advance`,
`reconcile_orphans`). The reconcilers are safe-by-default: nothing
destroys infra unless a rule explicitly opts in, and no flag/destroy
happens until a directory's absence is re-verified against the
tracked-branch tree.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import autodiscovery_lifecycle_service as svc

# ── Helpers ──────────────────────────────────────────────────────────────


def _fc(status, path, old_path=None):
    """A single file-change record (matches the poller's diff shape)."""
    return {"status": status, "path": path, "old_path": old_path}


def _mock_rule(on_directory_delete="flag", name_template=""):
    r = MagicMock()
    r.id = uuid.uuid4()
    r.name = "monorepo"
    r.name_template = name_template
    r.vcs_connection_id = uuid.uuid4()
    r.repo_url = "https://github.com/example/repo"
    r.on_directory_delete = on_directory_delete
    return r


def _mock_conn(provider="github"):
    c = MagicMock()
    c.id = uuid.uuid4()
    c.provider = provider
    return c


def _mock_ws(working_directory="accounts/a", name="accounts-a", pr_number=None):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = name
    ws.working_directory = working_directory
    ws.trigger_prefixes = [working_directory] if working_directory else []
    ws.lifecycle_state = "active"
    ws.lifecycle_reason = ""
    ws.autodiscovery_pr_number = pr_number
    return ws


def _result(*, scalar_one_or_none=None, scalars_all=None, scalar=None):
    """A SQLAlchemy-result-like MagicMock."""
    res = MagicMock()
    res.scalar_one_or_none.return_value = scalar_one_or_none
    res.scalars.return_value.all.return_value = scalars_all or []
    res.scalar.return_value = scalar
    return res


# ── classify_dir_changes (pure) ──────────────────────────────────────────


class TestClassifyDirChanges:
    def test_clean_delete(self):
        """All files in a dir removed, dir not otherwise present → deleted."""
        cls = svc.classify_dir_changes(
            [
                _fc("removed", "accounts/a/main.tf"),
                _fc("removed", "accounts/a/vars.tf"),
            ]
        )
        assert cls["deleted"] == {"accounts/a"}
        assert cls["renamed"] == []
        assert cls["ambiguous"] == set()

    def test_clean_rename(self):
        """All files renamed old_root → exactly one new_root, old not
        present → renamed (not deleted)."""
        cls = svc.classify_dir_changes(
            [
                _fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf"),
                _fc("renamed", "accounts/b/vars.tf", old_path="accounts/a/vars.tf"),
            ]
        )
        assert cls["renamed"] == [("accounts/a", "accounts/b")]
        assert cls["deleted"] == set()
        assert cls["ambiguous"] == set()

    def test_split_is_ambiguous_not_deleted_or_renamed(self):
        """One old_root fanning out to two new_roots → ambiguous."""
        cls = svc.classify_dir_changes(
            [
                _fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf"),
                _fc("renamed", "accounts/c/vars.tf", old_path="accounts/a/vars.tf"),
            ]
        )
        assert cls["ambiguous"] == {"accounts/a"}
        assert cls["renamed"] == []
        assert "accounts/a" not in cls["deleted"]

    def test_modified_only_dir_classifies_as_nothing(self):
        cls = svc.classify_dir_changes([_fc("modified", "accounts/a/main.tf")])
        assert cls["deleted"] == set()
        assert cls["renamed"] == []
        assert cls["ambiguous"] == set()

    def test_mixed_removed_and_added_same_dir_not_deleted(self):
        """A dir that is both removed-from and added-to is still present
        — not a clean delete."""
        cls = svc.classify_dir_changes(
            [
                _fc("removed", "accounts/a/old.tf"),
                _fc("added", "accounts/a/new.tf"),
            ]
        )
        assert "accounts/a" not in cls["deleted"]
        assert cls["renamed"] == []
        assert cls["ambiguous"] == set()


# ── reconcile_open_pr (visibility only) ──────────────────────────────────


class TestReconcileOpenPr:
    @patch.object(svc, "_post_comment", new_callable=AsyncMock)
    @patch.object(svc, "run_service")
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_deleted_dir_creates_speculative_destroy_and_comments(
        self, m_ws, m_run_service, m_comment
    ):
        rule = _mock_rule()
        conn = _mock_conn()
        ws = _mock_ws()
        m_ws.return_value = ws
        run = MagicMock()
        m_run_service.create_run = AsyncMock(return_value=run)
        db = AsyncMock()
        # Dedup query: no existing run.
        db.execute.return_value = _result(scalar_one_or_none=None)

        await svc.reconcile_open_pr(
            db,
            rule,
            conn,
            "example",
            "repo",
            7,
            "abc123",
            [_fc("removed", "accounts/a/main.tf")],
        )

        m_run_service.create_run.assert_awaited_once()
        kwargs = m_run_service.create_run.await_args.kwargs
        assert kwargs["is_destroy"] is True
        assert kwargs["plan_only"] is True
        assert kwargs["source"] == svc.LIFECYCLE_SOURCE
        assert run.vcs_commit_sha == "abc123"
        assert run.vcs_pull_request_number == 7
        m_comment.assert_awaited_once()

    @patch.object(svc, "_post_comment", new_callable=AsyncMock)
    @patch.object(svc, "run_service")
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_dedupe_existing_run_no_second_create(self, m_ws, m_run_service, m_comment):
        rule = _mock_rule()
        conn = _mock_conn()
        m_ws.return_value = _mock_ws()
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()
        # Dedup query: a run already exists for this (ws, head_sha).
        db.execute.return_value = _result(scalar_one_or_none=uuid.uuid4())

        await svc.reconcile_open_pr(
            db,
            rule,
            conn,
            "example",
            "repo",
            7,
            "abc123",
            [_fc("removed", "accounts/a/main.tf")],
        )

        m_run_service.create_run.assert_not_awaited()
        # Comment is still posted (visibility) even when the run is deduped.
        m_comment.assert_awaited_once()

    @patch.object(svc, "_post_comment", new_callable=AsyncMock)
    @patch.object(svc, "run_service")
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_file_changes_none_is_noop(self, m_ws, m_run_service, m_comment):
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()

        await svc.reconcile_open_pr(
            db, _mock_rule(), _mock_conn(), "example", "repo", 7, "abc123", None
        )

        m_ws.assert_not_awaited()
        m_run_service.create_run.assert_not_awaited()
        m_comment.assert_not_awaited()
        db.execute.assert_not_awaited()


# ── reconcile_branch_advance (mutating, safe) ────────────────────────────


class TestReconcileBranchAdvance:
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_moves_workspace_in_place(self, m_ws, m_absent):
        rule = _mock_rule()
        ws = _mock_ws(working_directory="accounts/a", name="accounts-a")
        m_ws.return_value = ws
        db = AsyncMock()
        # Clash check: nothing already owns the new dir.
        db.execute.return_value = _result(scalar_one_or_none=None)

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )

        assert ws.working_directory == "accounts/b"
        assert ws.trigger_prefixes == ["accounts/b"]
        # name_template empty → name unchanged (in-place move, not re-derived).
        assert ws.name == "accounts-a"
        assert ws.lifecycle_state == "active"

    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_rederives_name_when_template_set(self, m_ws, m_absent):
        rule = _mock_rule(name_template="ws-{path}")
        ws = _mock_ws(working_directory="accounts/a", name="ws-accounts-a")
        m_ws.return_value = ws
        db = AsyncMock()
        db.execute.return_value = _result(scalar_one_or_none=None)

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )

        assert ws.working_directory == "accounts/b"
        assert ws.name == "ws-accounts-b"

    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_target_collision_sets_pending_not_moved(self, m_ws, m_absent):
        rule = _mock_rule()
        ws = _mock_ws(working_directory="accounts/a")
        m_ws.return_value = ws
        db = AsyncMock()
        # Clash check: a workspace already owns the new dir.
        db.execute.return_value = _result(scalar_one_or_none=uuid.uuid4())

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )

        assert ws.lifecycle_state == "pending_deletion"
        # Not moved.
        assert ws.working_directory == "accounts/a"

    @patch.object(svc, "run_service")
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_delete_flag_policy_flags_pending_no_destroy(self, m_ws, m_absent, m_run_service):
        rule = _mock_rule(on_directory_delete="flag")
        ws = _mock_ws(working_directory="accounts/a")
        m_ws.return_value = ws
        m_absent.return_value = True  # re-verified gone
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("removed", "accounts/a/main.tf")],
        )

        assert ws.lifecycle_state == "pending_deletion"
        m_run_service.create_run.assert_not_awaited()

    @patch.object(svc, "run_service")
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_delete_destroy_policy_queues_destroy_run(self, m_ws, m_absent, m_run_service):
        rule = _mock_rule(on_directory_delete="destroy")
        ws = _mock_ws(working_directory="accounts/a")
        m_ws.return_value = ws
        m_absent.return_value = True
        run = MagicMock()
        run.id = uuid.uuid4()
        m_run_service.create_run = AsyncMock(return_value=run)
        db = AsyncMock()

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("removed", "accounts/a/main.tf")],
        )

        m_run_service.create_run.assert_awaited_once()
        kwargs = m_run_service.create_run.await_args.kwargs
        assert kwargs["is_destroy"] is True
        assert kwargs["plan_only"] is False
        assert kwargs["auto_apply"] is True

    @patch.object(svc, "run_service")
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_dir_still_present_does_nothing(self, m_ws, m_absent, m_run_service):
        """Critical safety: if the dir is still present (or the tree is
        unverifiable), absolutely nothing happens — no flag, no destroy."""
        rule = _mock_rule(on_directory_delete="destroy")
        ws = _mock_ws(working_directory="accounts/a")
        m_ws.return_value = ws
        m_absent.return_value = False  # NOT verified absent
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("removed", "accounts/a/main.tf")],
        )

        assert ws.lifecycle_state == "active"
        assert ws.lifecycle_reason == ""
        m_run_service.create_run.assert_not_awaited()

    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_file_changes_none_is_noop(self, m_ws):
        db = AsyncMock()
        await svc.reconcile_branch_advance(
            db, _mock_rule(), _mock_conn(), "example", "repo", "main", None
        )
        m_ws.assert_not_awaited()
        db.execute.assert_not_awaited()


# ── reconcile_orphans ────────────────────────────────────────────────────


class TestReconcileOrphans:
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_closed_pr_dir_absent_no_state_archived(self, m_absent, m_state):
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_absent.return_value = True
        m_state.return_value = False  # never applied

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "archived"

    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_closed_pr_dir_absent_has_state_pending_deletion(self, m_absent, m_state):
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_absent.return_value = True
        m_state.return_value = True  # has applied state

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "pending_deletion"

    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_pr_still_open_untouched(self, m_absent, m_state):
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers={42}
        )

        assert ws.lifecycle_state == "active"
        m_absent.assert_not_awaited()
        m_state.assert_not_awaited()

    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_dir_still_present_untouched(self, m_absent, m_state):
        """PR closed but the dir is still on the branch (legitimately
        merged/pushed) — leave the workspace alone."""
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_absent.return_value = False  # dir present

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "active"
        m_state.assert_not_awaited()
