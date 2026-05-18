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

    def test_squash_merge_rename_inferred_from_add_remove_symmetry(self):
        """Squash merge loses provider `renamed` status: the move shows
        up as plain removed(old) + added(new) with identical basenames.
        We MUST infer the rename, not classify it as a delete — a
        `destroy`-opt-in rule would otherwise tear down a renamed
        workspace (the #314 Test-2 bug)."""
        cls = svc.classify_dir_changes(
            [
                _fc("removed", "accounts/a/main.tf"),
                _fc("removed", "accounts/a/vars.tf"),
                _fc("added", "accounts/b/main.tf"),
                _fc("added", "accounts/b/vars.tf"),
            ]
        )
        assert cls["renamed"] == [("accounts/a", "accounts/b")]
        assert cls["deleted"] == set()
        assert cls["ambiguous"] == set()

    def test_inferred_rename_basename_mismatch_stays_deleted(self):
        """No symmetry (different basenames) → the removed dir is a real
        delete, the added dir is just new. Never falsely 'rename'."""
        cls = svc.classify_dir_changes(
            [
                _fc("removed", "accounts/a/main.tf"),
                _fc("added", "accounts/b/totally-different.tf"),
            ]
        )
        assert cls["deleted"] == {"accounts/a"}
        assert cls["renamed"] == []

    def test_inferred_rename_ambiguous_when_two_added_dirs_match(self):
        """Removed dir's basename set matches >1 added dir → ambiguous,
        never auto-deleted or auto-renamed."""
        cls = svc.classify_dir_changes(
            [
                _fc("removed", "accounts/a/main.tf"),
                _fc("added", "accounts/b/main.tf"),
                _fc("added", "accounts/c/main.tf"),
            ]
        )
        assert cls["ambiguous"] == {"accounts/a"}
        assert cls["renamed"] == []
        assert "accounts/a" not in cls["deleted"]


# ── rename_target_dirs_to_suppress (#314 duplicate prevention) ───────────


class TestRenameTargetDirsToSuppress:
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_with_existing_ws_suppresses_new_dir(self, m_ws):
        """Old side has a rule-owned active workspace → the new dir must
        be suppressed so open-PR autodiscovery doesn't create a
        duplicate that the merge-time rename would then clash with."""
        m_ws.return_value = _mock_ws()
        out = await svc.rename_target_dirs_to_suppress(
            AsyncMock(),
            [_mock_rule()],
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )
        assert out == {"accounts/b"}

    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_without_existing_ws_suppresses_nothing(self, m_ws):
        """No existing workspace on the old side → it's not really a
        'move of a tracked workspace'; let normal autodiscovery run."""
        m_ws.return_value = None
        out = await svc.rename_target_dirs_to_suppress(
            AsyncMock(),
            [_mock_rule()],
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )
        assert out == set()

    async def test_none_file_changes_suppresses_nothing(self):
        out = await svc.rename_target_dirs_to_suppress(AsyncMock(), [_mock_rule()], None)
        assert out == set()


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
        # 1) run-dedup query → no existing run; 2) notify-seen query →
        # not yet notified.
        db.execute.side_effect = [
            _result(scalar_one_or_none=None),
            _result(scalar_one_or_none=None),
        ]

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
    async def test_dedupe_existing_run_still_comments_first_time(
        self, m_ws, m_run_service, m_comment
    ):
        """Run already exists (no second plan) but this is the first
        comment for this head_sha → comment IS posted."""
        rule = _mock_rule()
        conn = _mock_conn()
        m_ws.return_value = _mock_ws()
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()
        db.execute.side_effect = [
            _result(scalar_one_or_none=uuid.uuid4()),  # run exists
            _result(scalar_one_or_none=None),  # not yet notified
        ]

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
        m_comment.assert_awaited_once()

    @patch.object(svc, "_post_comment", new_callable=AsyncMock)
    @patch.object(svc, "run_service")
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_comment_deduped_when_already_notified(self, m_ws, m_run_service, m_comment):
        """The cyclic poller must NOT re-post the same comment: once a
        notify marker exists for (ws, head_sha) the comment is
        suppressed even though reconcile runs again."""
        rule = _mock_rule()
        conn = _mock_conn()
        m_ws.return_value = _mock_ws()
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()
        db.execute.side_effect = [
            _result(scalar_one_or_none=uuid.uuid4()),  # run exists
            _result(scalar_one_or_none=uuid.uuid4()),  # already notified
        ]

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
        m_comment.assert_not_awaited()

    @patch.object(svc, "_post_comment", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_comment_deduped_when_already_notified(self, m_ws, m_comment):
        """Rename comment is also deduped per (ws, head_sha)."""
        m_ws.return_value = _mock_ws()
        db = AsyncMock()
        db.execute.return_value = _result(scalar_one_or_none=uuid.uuid4())  # notified

        await svc.reconcile_open_pr(
            db,
            _mock_rule(),
            _mock_conn(),
            "example",
            "repo",
            7,
            "abc123",
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )

        m_comment.assert_not_awaited()

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

    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_absorbs_speculative_duplicate_and_moves_original(
        self, m_ws, m_absent, m_has_state
    ):
        """The common PR-driven rename: open-PR autodiscovery already
        created a never-applied speculative workspace at the new dir.
        That is NOT a real conflict — it must be absorbed (archived,
        name/dir freed) and the original (state/history-bearing)
        workspace moved in place. This is the #314 Test-2 bug."""
        rule = _mock_rule()
        original = _mock_ws(working_directory="accounts/a", name="accounts-a")
        m_ws.return_value = original
        dup = _mock_ws(working_directory="accounts/b", name="accounts-b", pr_number=8)
        dup.autodiscovery_rule_id = rule.id  # rule-owned
        m_has_state.return_value = False  # never applied
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[dup])

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )

        # Original moved in place, still active.
        assert original.working_directory == "accounts/b"
        assert original.lifecycle_state == "active"
        # Speculative duplicate archived + name/dir freed (no clash).
        assert dup.lifecycle_state == "archived"
        assert dup.name.startswith("accounts-b-superseded-")
        assert dup.working_directory.startswith("accounts/b#superseded-")

    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    @patch.object(svc, "_autodiscovered_ws", new_callable=AsyncMock)
    async def test_rename_genuine_conflict_not_absorbed(self, m_ws, m_absent, m_has_state):
        """A real workspace already owns the new dir (not a rule-owned
        zero-state speculative dup) → must NOT be absorbed; the original
        is flagged pending_deletion for an operator to decide."""
        rule = _mock_rule()
        original = _mock_ws(working_directory="accounts/a")
        m_ws.return_value = original
        other = _mock_ws(working_directory="accounts/b", name="real-b")
        other.autodiscovery_rule_id = uuid.uuid4()  # different rule → not absorbable
        db = AsyncMock()
        db.execute.side_effect = [
            _result(scalars_all=[other]),  # _absorbable_speculative_dup → None
            _result(scalar_one_or_none=uuid.uuid4()),  # clash check → conflict
        ]

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("renamed", "accounts/b/main.tf", old_path="accounts/a/main.tf")],
        )

        assert original.lifecycle_state == "pending_deletion"
        assert original.working_directory == "accounts/a"  # not moved
        assert other.lifecycle_state == "active"  # untouched

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
        db.execute.return_value = _result(scalar_one_or_none=None)  # no destroy in flight

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
    async def test_destroy_deduped_when_already_queued(self, m_ws, m_absent, m_run_service):
        """Idempotency: every branch advance of any workspace sharing
        the rule/repo re-runs reconcile. The destroy path must NOT queue
        a second destroy while one is already in flight/done (otherwise
        concurrent `terraform destroy` jobs for one workspace)."""
        rule = _mock_rule(on_directory_delete="destroy")
        ws = _mock_ws(working_directory="accounts/a")
        m_ws.return_value = ws
        m_absent.return_value = True
        m_run_service.create_run = AsyncMock()
        db = AsyncMock()
        db.execute.return_value = _result(scalar_one_or_none=uuid.uuid4())  # already queued

        await svc.reconcile_branch_advance(
            db,
            rule,
            _mock_conn(),
            "example",
            "repo",
            "main",
            [_fc("removed", "accounts/a/main.tf")],
        )

        m_run_service.create_run.assert_not_awaited()
        assert ws.lifecycle_reason == ""  # not re-stamped

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


def _pr(*, merged=False, state="closed"):
    """A PR-like object exposing just what reconcile_orphans reads."""
    return MagicMock(merged=merged, state=state)


class TestReconcileOrphans:
    @patch.object(svc, "_get_pull_request", new_callable=AsyncMock)
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_closed_unmerged_dir_absent_no_state_archived(self, m_absent, m_state, m_get_pr):
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_get_pr.return_value = _pr(merged=False, state="closed")
        m_absent.return_value = True
        m_state.return_value = False  # never applied

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "archived"

    @patch.object(svc, "_get_pull_request", new_callable=AsyncMock)
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_closed_unmerged_dir_absent_has_state_pending_deletion(
        self, m_absent, m_state, m_get_pr
    ):
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_get_pr.return_value = _pr(merged=False, state="closed")
        m_absent.return_value = True
        m_state.return_value = True  # has applied state

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "pending_deletion"

    @patch.object(svc, "_get_pull_request", new_callable=AsyncMock)
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_origin_pr_merged_graduates_and_is_never_orphaned(
        self, m_absent, m_state, m_get_pr
    ):
        """The #314 Test-2 regression: a workspace whose origin PR
        *merged* is a real graduated workspace, NOT an orphan. It must
        shed its speculative PR link and be left active so a later
        rename of its dir is moved in place (not archived here)."""
        rule = _mock_rule()
        ws = _mock_ws(pr_number=7)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_get_pr.return_value = _pr(merged=True, state="closed")

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "active"
        assert ws.autodiscovery_pr_number is None  # graduated
        m_absent.assert_not_awaited()  # never even checks the tree
        m_state.assert_not_awaited()

    @patch.object(svc, "_get_pull_request", new_callable=AsyncMock)
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_unknown_pr_state_fail_safe_no_action(self, m_absent, m_state, m_get_pr):
        """PR fetch returns None (deleted/transient) → never archive on
        uncertainty; leave untouched and retry next cycle."""
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_get_pr.return_value = None

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "active"
        assert ws.autodiscovery_pr_number == 42
        m_absent.assert_not_awaited()
        m_state.assert_not_awaited()

    @patch.object(svc, "_get_pull_request", new_callable=AsyncMock)
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_pr_still_open_untouched(self, m_absent, m_state, m_get_pr):
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers={42}
        )

        assert ws.lifecycle_state == "active"
        m_get_pr.assert_not_awaited()
        m_absent.assert_not_awaited()
        m_state.assert_not_awaited()

    @patch.object(svc, "_get_pull_request", new_callable=AsyncMock)
    @patch.object(svc, "_has_state", new_callable=AsyncMock)
    @patch.object(svc, "_dir_absent_on_branch", new_callable=AsyncMock)
    async def test_closed_unmerged_dir_still_present_untouched(self, m_absent, m_state, m_get_pr):
        """Origin PR closed unmerged but the dir is somehow on the
        branch — leave the workspace alone."""
        rule = _mock_rule()
        ws = _mock_ws(pr_number=42)
        db = AsyncMock()
        db.execute.return_value = _result(scalars_all=[ws])
        m_get_pr.return_value = _pr(merged=False, state="closed")
        m_absent.return_value = False  # dir present

        await svc.reconcile_orphans(
            db, rule, _mock_conn(), "example", "repo", "main", open_pr_numbers=set()
        )

        assert ws.lifecycle_state == "active"
        m_state.assert_not_awaited()
