"""Autodiscovery workspace lifecycle — rename / delete / orphan (#314).

Safe-by-default. Guarantees:
- Nothing here destroys infrastructure unless the rule explicitly opts
  in with `on_directory_delete == "destroy"`. The default ("flag")
  only marks the workspace `pending_deletion` and needs an explicit
  operator action.
- Open-PR handling is visibility-only: a PR comment + a *speculative*
  (`plan_only`, `is_destroy`) plan so reviewers see the blast radius.
  No state, lifecycle, or infra mutation happens until the change
  reaches the tracked branch.
- Before flagging/destroying on branch-advance we RE-VERIFY the
  directory is actually absent from the tracked-branch tree — we never
  act on a heuristic diff alone, and never on a truncated diff.
- A never-applied orphan (zero StateVersions) is the only thing
  auto-archived; anything with state is flagged for a human.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    AuditLog,
    AutodiscoveryRule,
    Run,
    StateVersion,
    VCSConnection,
    Workspace,
    generate_uuid7,
)
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_service
from terrapod.services.workspace_autodiscovery_service import (
    derive_root_directory,
    derive_workspace_name,
)

logger = get_logger(__name__)

LIFECYCLE_SOURCE = "autodiscovery-lifecycle"


def classify_dir_changes(
    file_changes: list[dict[str, str | None]],
) -> dict[str, Any]:
    """Pure: reduce per-file change records to directory-level intent.

    Returns ``{"deleted": set[str], "renamed": list[(old,new)],
    "ambiguous": set[str]}``:
    - deleted: dirs whose only changes are removals (no add/modify in
      the same dir, not a rename source).
    - renamed: a dir whose files were renamed into exactly one other dir.
    - ambiguous: a rename source whose files fanned out to >1 dir, or a
      dir that is both removed-from and added-to (split/merge) — never
      auto-acted on; surfaced for a human.
    """
    removed: dict[str, int] = {}
    present: set[str] = set()
    rename_map: dict[str, set[str]] = {}

    for fc in file_changes:
        status = fc.get("status")
        path = fc.get("path") or ""
        if status == "removed":
            removed[derive_root_directory(path)] = removed.get(derive_root_directory(path), 0) + 1
        elif status == "renamed":
            old_root = derive_root_directory(fc.get("old_path") or "")
            new_root = derive_root_directory(path)
            present.add(new_root)
            if old_root != new_root:
                rename_map.setdefault(old_root, set()).add(new_root)
            removed[old_root] = removed.get(old_root, 0)  # ensure key seen
        else:  # added / modified
            present.add(derive_root_directory(path))

    renamed: list[tuple[str, str]] = []
    ambiguous: set[str] = set()
    for old_root, new_roots in rename_map.items():
        if len(new_roots) == 1 and old_root not in present:
            renamed.append((old_root, next(iter(new_roots))))
        else:
            ambiguous.add(old_root)

    rename_sources = {o for o, _ in renamed} | ambiguous
    deleted = {
        d
        for d, n in removed.items()
        if n > 0 and d and d not in present and d not in rename_sources
    }
    return {"deleted": deleted, "renamed": renamed, "ambiguous": ambiguous}


async def _autodiscovered_ws(
    db: AsyncSession, rule: AutodiscoveryRule, root: str
) -> Workspace | None:
    """The active autodiscovered workspace this rule owns at `root`."""
    res = await db.execute(
        select(Workspace).where(
            Workspace.autodiscovery_rule_id == rule.id,
            Workspace.vcs_connection_id == rule.vcs_connection_id,
            Workspace.vcs_repo_url == rule.repo_url,
            Workspace.working_directory == root,
            Workspace.lifecycle_state == "active",
        )
    )
    return res.scalar_one_or_none()


async def _has_state(db: AsyncSession, ws_id) -> bool:  # noqa: ANN001
    n = await db.execute(
        select(func.count()).select_from(StateVersion).where(StateVersion.workspace_id == ws_id)
    )
    return (n.scalar() or 0) > 0


async def _post_comment(
    conn: VCSConnection, owner: str, repo: str, pr_number: int, body: str
) -> None:
    try:
        if conn.provider == "gitlab":
            await gitlab_service.create_mr_comment(conn, owner, repo, pr_number, body)
        else:
            await github_service.create_pr_comment(conn, owner, repo, pr_number, body)
    except Exception as e:  # comment failure must not break the poll cycle
        logger.warning("lifecycle PR comment failed", error=repr(e), pr=pr_number)


def _audit(db: AsyncSession, action: str, ws: Workspace, detail: str) -> None:
    db.add(
        AuditLog(
            id=generate_uuid7(),
            actor_email="system",
            actor_type="system",
            origin="system",
            action=action,
            resource_type="workspace",
            resource_id=f"ws-{ws.id}",
            status_code=200,
            detail=detail,
        )
    )


async def reconcile_open_pr(
    db: AsyncSession,
    rule: AutodiscoveryRule,
    conn: VCSConnection,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    file_changes: list[dict[str, str | None]] | None,
) -> None:
    """Visibility only — no mutation. Speculative destroy plan + PR
    comment for deletes; informational comment for renames/ambiguous.
    `file_changes is None` (truncated diff) → skip entirely.
    """
    if file_changes is None:
        return
    cls = classify_dir_changes(file_changes)

    for d in sorted(cls["deleted"]):
        ws = await _autodiscovered_ws(db, rule, d)
        if ws is None:
            continue
        # Dedupe: one speculative destroy plan per (workspace, head_sha).
        dup = await db.execute(
            select(Run.id).where(
                Run.workspace_id == ws.id,
                Run.vcs_commit_sha == head_sha,
                Run.is_destroy.is_(True),
                Run.plan_only.is_(True),
            )
        )
        if dup.scalar_one_or_none() is None:
            run = await run_service.create_run(
                db,
                workspace=ws,
                message=f"Speculative destroy plan: '{d}' removed in PR #{pr_number}",
                is_destroy=True,
                plan_only=True,
                source=LIFECYCLE_SOURCE,
                created_by="autodiscovery-lifecycle",
            )
            run.vcs_commit_sha = head_sha
            run.vcs_pull_request_number = pr_number
        await _post_comment(
            conn,
            owner,
            repo,
            pr_number,
            f"⚠️ Autodiscovery: directory `{d}` is removed in this PR. "
            f"Workspace `{ws.name}` maps to it — a **speculative destroy plan** "
            f"has been queued so you can review the blast radius. On merge, "
            f"this workspace will be "
            + (
                "**destroyed then archived** (rule opted in)."
                if rule.on_directory_delete == "destroy"
                else "marked *pending deletion* (requires an explicit operator action)."
            ),
        )

    for old, new in cls["renamed"]:
        ws = await _autodiscovered_ws(db, rule, old)
        if ws is None:
            continue
        await _post_comment(
            conn,
            owner,
            repo,
            pr_number,
            f"♻️ Autodiscovery: detected rename `{old}` → `{new}`. On merge, "
            f"workspace `{ws.name}` will be **moved in place** "
            f"(state & history preserved — no destroy).",
        )

    for amb in sorted(cls["ambiguous"]):
        ws = await _autodiscovered_ws(db, rule, amb)
        if ws is None:
            continue
        await _post_comment(
            conn,
            owner,
            repo,
            pr_number,
            f"❓ Autodiscovery: `{amb}` looks split/merged across multiple "
            f"directories — not treated as a clean rename. Workspace "
            f"`{ws.name}` will be left as *pending deletion* on merge for a "
            f"human to decide; new directories autodiscover normally.",
        )


async def _dir_absent_on_branch(
    conn: VCSConnection, owner: str, repo: str, branch: str, root: str
) -> bool:
    """Re-verify a directory really is gone from the tracked branch
    before any flag/destroy. Returns False (do NOT act) if the tree
    can't be listed or is truncated — fail safe.
    """
    try:
        if conn.provider == "gitlab":
            tree = await gitlab_service.list_repo_tree(conn, owner, repo, branch)
        else:
            tree = await github_service.list_repo_tree(conn, owner, repo, branch)
    except Exception:
        return False
    if tree is None:  # truncated — never act on incomplete data
        return False
    prefix = root.rstrip("/") + "/"
    return not any(p == root or p.startswith(prefix) for p in tree)


async def reconcile_branch_advance(
    db: AsyncSession,
    rule: AutodiscoveryRule,
    conn: VCSConnection,
    owner: str,
    repo: str,
    branch: str,
    file_changes: list[dict[str, str | None]] | None,
) -> None:
    """Mutating, but safe: applies renames in place and applies the
    rule's delete policy. Re-verifies every directory's absence against
    the tracked-branch tree first. Skips on truncated diff.
    """
    if file_changes is None:
        return
    cls = classify_dir_changes(file_changes)

    # Renames: move the existing workspace in place (state preserved).
    for old, new in cls["renamed"]:
        ws = await _autodiscovered_ws(db, rule, old)
        if ws is None:
            continue
        clash = await db.execute(
            select(Workspace.id).where(
                Workspace.vcs_connection_id == rule.vcs_connection_id,
                Workspace.vcs_repo_url == rule.repo_url,
                Workspace.working_directory == new,
                Workspace.id != ws.id,
            )
        )
        if clash.scalar_one_or_none() is not None:
            ws.lifecycle_state = "pending_deletion"
            ws.lifecycle_reason = (
                f"rename {old}->{new} but a workspace already owns {new}; "
                f"needs an operator decision"
            )
            _audit(db, "autodiscovery.rename_conflict", ws, ws.lifecycle_reason)
            continue
        ws.working_directory = new
        ws.trigger_prefixes = [new] if new else []
        if rule.name_template:
            ws.name = derive_workspace_name(rule, new)
        _audit(db, "autodiscovery.workspace_moved", ws, f"{old} -> {new}")
        logger.info(
            "Autodiscovery moved workspace on rename",
            workspace_id=str(ws.id),
            old=old,
            new=new,
        )

    # Deletes + ambiguous: only after re-verifying the dir is truly gone.
    for d in sorted(cls["deleted"] | cls["ambiguous"]):
        ws = await _autodiscovered_ws(db, rule, d)
        if ws is None:
            continue
        if not await _dir_absent_on_branch(conn, owner, repo, branch, d):
            continue  # still present (or unverifiable) — do nothing
        if d in cls["ambiguous"] or rule.on_directory_delete != "destroy":
            ws.lifecycle_state = "pending_deletion"
            ws.lifecycle_reason = f"directory '{d}' removed on '{branch}'"
            _audit(db, "autodiscovery.pending_deletion", ws, ws.lifecycle_reason)
            logger.info(
                "Autodiscovery flagged workspace pending_deletion",
                workspace_id=str(ws.id),
                directory=d,
            )
        else:  # rule explicitly opted in to destroy
            run = await run_service.create_run(
                db,
                workspace=ws,
                message=f"Autodiscovery: '{d}' removed — destroying (rule opt-in)",
                is_destroy=True,
                plan_only=False,
                auto_apply=True,
                source=LIFECYCLE_SOURCE,
                created_by="autodiscovery-lifecycle",
            )
            ws.lifecycle_reason = f"destroy queued (run {run.id}) — '{d}' removed"
            _audit(
                db,
                "autodiscovery.destroy_queued",
                ws,
                f"directory '{d}' removed; destroy run {run.id} queued",
            )
            logger.info(
                "Autodiscovery queued destroy run",
                workspace_id=str(ws.id),
                run_id=str(run.id),
                directory=d,
            )


async def reconcile_orphans(
    db: AsyncSession,
    rule: AutodiscoveryRule,
    conn: VCSConnection,
    owner: str,
    repo: str,
    branch: str,
    open_pr_numbers: set[int],
) -> None:
    """Autodiscovered workspaces whose origin PR is no longer open and
    whose directory is gone from the tracked branch are orphans:
    zero-state → archived; has-state → pending_deletion.
    """
    res = await db.execute(
        select(Workspace).where(
            Workspace.autodiscovery_rule_id == rule.id,
            Workspace.vcs_connection_id == rule.vcs_connection_id,
            Workspace.vcs_repo_url == rule.repo_url,
            Workspace.lifecycle_state == "active",
            Workspace.autodiscovery_pr_number.is_not(None),
        )
    )
    for ws in res.scalars().all():
        if ws.autodiscovery_pr_number in open_pr_numbers:
            continue  # PR still open — nothing to reconcile
        if not await _dir_absent_on_branch(conn, owner, repo, branch, ws.working_directory):
            continue  # dir present (PR merged / pushed) — legitimate
        if await _has_state(db, ws.id):
            ws.lifecycle_state = "pending_deletion"
            ws.lifecycle_reason = (
                f"origin PR #{ws.autodiscovery_pr_number} closed unmerged; "
                f"workspace has state — needs an explicit operator action"
            )
            _audit(db, "autodiscovery.pending_deletion", ws, ws.lifecycle_reason)
        else:
            ws.lifecycle_state = "archived"
            ws.lifecycle_reason = (
                f"origin PR #{ws.autodiscovery_pr_number} closed unmerged; "
                f"never applied — auto-archived"
            )
            _audit(db, "autodiscovery.archived", ws, ws.lifecycle_reason)
        logger.info(
            "Autodiscovery reconciled orphan",
            workspace_id=str(ws.id),
            state=ws.lifecycle_state,
            pr=ws.autodiscovery_pr_number,
        )
