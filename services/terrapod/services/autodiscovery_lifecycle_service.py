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
    derive_pulumi_workspace_name,
    derive_root_directory,
    derive_workspace_name,
)

logger = get_logger(__name__)

LIFECYCLE_SOURCE = "autodiscovery-lifecycle"


def classify_for_rule(
    rule: AutodiscoveryRule, file_changes: list[dict[str, str | None]]
) -> dict[str, Any]:
    """Change intent in this rule's own units (#1570).

    Normalises BOTH engines to `(root, stack | None)` so every reconciler below
    works on one shape. Terraform's unit is the directory, carried as
    `(root, None)` -- which is also exactly what `_autodiscovered_ws` wants for
    a workspace whose `stack` is NULL, so the two agree without a special case
    at each call site.
    """
    if (getattr(rule, "engine", "") or "terraform") == "pulumi":
        return classify_stack_changes(file_changes)
    cls = classify_dir_changes(file_changes)
    return {
        "deleted": {(d, None) for d in cls["deleted"]},
        "renamed": [((o, None), (n, None)) for o, n in cls["renamed"]],
        "ambiguous": {(a, None) for a in cls["ambiguous"]},
    }


def classify_stack_changes(
    file_changes: list[dict[str, str | None]],
) -> dict[str, Any]:
    """Pure: the same reduction as `classify_dir_changes`, over (dir, stack).

    A Pulumi workspace is one stack, so the intent that matters is per stack
    file, not per directory: removing `Pulumi.dev.yaml` while `Pulumi.prod.yaml`
    stays is one workspace gone and the directory very much alive. Feeding that
    to the directory classifier answers "nothing was deleted", so the flag never
    fires -- and, in the other direction, a directory whose stacks were ALL
    removed would read as one deletion rather than several.

    Returns the same shape, keyed by `(root, stack)` instead of `root`:
    ``{"deleted": set[(root, stack)], "renamed": list[((root,stack),(root,stack))],
    "ambiguous": set[(root, stack)]}``.

    **Deliberately stricter than the directory classifier.** That one infers a
    rename from add/remove symmetry across basenames, because a squash merge
    loses per-file `renamed` status. A stack has exactly one file, so there is
    no set to compare and no symmetry to lean on -- a removed stack and an
    added one in the same push are indistinguishable from a delete plus an
    unrelated create. So a rename is recognised ONLY from an explicit
    `renamed` status, and a removal that coincides with any addition in the
    same directory is **ambiguous, never deleted**. Ambiguous is surfaced for a
    human and never auto-acted on (#314), which is the safe direction: the cost
    of a missed rename is a flagged workspace, the cost of a missed ambiguity
    is a destroyed one.
    """
    from terrapod.services.workspace_autodiscovery_service import (
        derive_root_directory,
        pulumi_stack_of,
    )

    def unit(path: str | None) -> tuple[str, str] | None:
        if not path:
            return None
        stack = pulumi_stack_of(path)
        return (derive_root_directory(path), stack) if stack else None

    removed: set[tuple[str, str]] = set()
    added: set[tuple[str, str]] = set()
    renamed: list[tuple[tuple[str, str], tuple[str, str]]] = []

    for ch in file_changes:
        status = (ch.get("status") or "").lower()
        path = ch.get("path") or ch.get("filename")
        previous = ch.get("previous_path") or ch.get("previous_filename")
        if status == "renamed":
            src, dst = unit(previous), unit(path)
            if src and dst and src != dst:
                renamed.append((src, dst))
            elif dst:
                added.add(dst)
            elif src:
                removed.add(src)
            continue
        u = unit(path)
        if u is None:
            continue
        if status in ("removed", "deleted"):
            removed.add(u)
        else:
            added.add(u)

    renamed_sources = {src for src, _ in renamed}
    renamed_targets = {dst for _, dst in renamed}

    # A removal in a directory that also gained a stack is not a clean delete:
    # it could be the rename a squash merge flattened. Never auto-acted on.
    added_dirs = {root for root, _ in added | renamed_targets}
    deleted: set[tuple[str, str]] = set()
    ambiguous: set[tuple[str, str]] = set()
    for u in removed - renamed_sources:
        (ambiguous if u[0] in added_dirs else deleted).add(u)

    return {"deleted": deleted, "renamed": renamed, "ambiguous": ambiguous}


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

    Renames are detected two ways, because a provider's per-file
    `renamed` status is NOT reliable across a **squash merge** (GitHub's
    compare(old..new) for a squashed PR reports the moves as plain
    removed+added). Relying on `renamed` only would mis-classify a
    merged rename as a delete — which, on a `destroy`-opt-in rule,
    would wrongly tear down a renamed workspace. So we ALSO infer a
    rename from add/remove symmetry: a removed dir A whose set of
    file basenames exactly equals the added set of exactly one new dir
    B (and nothing else) is A→B. Anything less clean is ambiguous, not
    deleted — never auto-destroyed.
    """
    from pathlib import PurePosixPath

    removed_files: dict[str, set[str]] = {}
    added_files: dict[str, set[str]] = {}
    present: set[str] = set()
    rename_map: dict[str, set[str]] = {}

    def _base(p: str) -> str:
        return PurePosixPath(p).name

    for fc in file_changes:
        status = fc.get("status")
        path = fc.get("path") or ""
        if status == "removed":
            removed_files.setdefault(derive_root_directory(path), set()).add(_base(path))
        elif status == "renamed":
            old_root = derive_root_directory(fc.get("old_path") or "")
            new_root = derive_root_directory(path)
            present.add(new_root)
            added_files.setdefault(new_root, set()).add(_base(path))
            if old_root != new_root:
                rename_map.setdefault(old_root, set()).add(new_root)
                removed_files.setdefault(old_root, set()).add(_base(fc.get("old_path") or ""))
        elif status == "added":
            present.add(derive_root_directory(path))
            added_files.setdefault(derive_root_directory(path), set()).add(_base(path))
        else:  # modified
            present.add(derive_root_directory(path))

    renamed: list[tuple[str, str]] = []
    ambiguous: set[str] = set()

    # 1) Explicit provider-reported renames.
    for old_root, new_roots in rename_map.items():
        if len(new_roots) == 1 and old_root not in present:
            renamed.append((old_root, next(iter(new_roots))))
        else:
            ambiguous.add(old_root)

    handled = {o for o, _ in renamed} | ambiguous

    # 2) Inferred renames from exact add/remove basename symmetry — the
    # squash-merge-safe path. A removed dir's basename set must match
    # exactly one added dir's basename set, and that dir must not itself
    # be a rename target already.
    used_targets = {n for _, n in renamed}
    for a, a_files in removed_files.items():
        if a in handled or not a or not a_files or a in present:
            continue
        matches = [
            b
            for b, b_files in added_files.items()
            if b != a and b not in used_targets and b_files == a_files
        ]
        if len(matches) == 1:
            renamed.append((a, matches[0]))
            used_targets.add(matches[0])
            handled.add(a)
        elif matches:
            ambiguous.add(a)
            handled.add(a)

    rename_sources = {o for o, _ in renamed} | ambiguous
    deleted = {
        d
        for d, files in removed_files.items()
        if files and d and d not in present and d not in rename_sources
    }
    return {"deleted": deleted, "renamed": renamed, "ambiguous": ambiguous}


async def _autodiscovered_ws(
    db: AsyncSession, rule: AutodiscoveryRule, root: str, stack: str | None = None
) -> Workspace | None:
    """The active autodiscovered workspace this rule owns at `root`.

    `stack` completes the address for a Pulumi rule (#1570). A Pulumi directory
    normally holds several stacks, so filtering on the directory alone matches
    every sibling and `scalar_one_or_none` RAISES rather than answering -- and
    this is the lookup every flag and destroy decision is made from. `None`
    means "not stack-scoped", which is every Terraform workspace.
    """
    stack_filter = Workspace.stack.is_(None) if stack is None else Workspace.stack == stack
    res = await db.execute(
        select(Workspace).where(
            Workspace.autodiscovery_rule_id == rule.id,
            Workspace.vcs_connection_id == rule.vcs_connection_id,
            Workspace.vcs_repo_url == rule.repo_url,
            Workspace.working_directory == root,
            stack_filter,
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


async def _get_pull_request(conn: VCSConnection, owner: str, repo: str, pr_number: int):
    """Fetch a single PR/MR. Returns the PullRequest (with `.merged`)
    or None if it can't be determined (404 / transient error). The
    orphan reconciler treats None as 'unknown' and does nothing —
    fail safe, never archive on uncertainty.
    """
    try:
        if conn.provider == "gitlab":
            return await gitlab_service.get_pull_request(conn, owner, repo, pr_number)
        return await github_service.get_pull_request(conn, owner, repo, pr_number)
    except Exception as e:
        logger.warning("lifecycle PR fetch failed", error=repr(e), pr=pr_number)
        return None


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


async def rename_target_dirs_to_suppress(
    db: AsyncSession,
    rules: list[AutodiscoveryRule],
    file_changes: list[dict[str, str | None]] | None,
) -> set[str]:
    """Directory roots that are the *new* side of a detected rename
    whose *old* side already has an active rule-owned autodiscovered
    workspace.

    These must NOT get a fresh speculative workspace while the PR is
    open: the rename is reconciled by moving the existing workspace in
    place on merge. Creating a duplicate here would make the merge-time
    rename collide with it (clash → original wrongly flagged
    pending_deletion). `None`/truncated diff → suppress nothing (safe:
    the merge path also re-classifies and absorbs duplicates).
    """
    if file_changes is None:
        return set()
    out: set[str] = set()
    for rule in rules:
        cls = classify_for_rule(rule, file_changes)
        for (old, old_stack), (new, _new_stack) in cls["renamed"]:
            if await _autodiscovered_ws(db, rule, old, old_stack) is not None:
                # Deliberately the DIRECTORY, not the unit: the caller uses this
                # as `skip_roots`, which suppresses speculative creation by
                # directory. For a Pulumi rename that suppresses the renamed
                # stack's siblings too -- broader than necessary, and in the
                # safe direction, since the merge path re-classifies and moves
                # the workspace in place rather than creating a duplicate.
                out.add(new)
    return out


async def _absorbable_speculative_dup(
    db: AsyncSession, rule: AutodiscoveryRule, new: str, keep_id
) -> Workspace | None:  # noqa: ANN001
    """A workspace at `new` that is just a never-applied speculative
    duplicate the open-PR autodiscovery created for this rename target:
    same rule, PR-originated, zero state, not already terminal. Safe to
    archive out of the way so the real (state-bearing) workspace can be
    moved in place. Anything else (manual ws, has state, different rule)
    is a genuine conflict and is NOT returned.
    """
    res = await db.execute(
        select(Workspace).where(
            Workspace.vcs_connection_id == rule.vcs_connection_id,
            Workspace.vcs_repo_url == rule.repo_url,
            Workspace.working_directory == new,
            Workspace.id != keep_id,
            Workspace.lifecycle_state != "archived",
        )
    )
    others = res.scalars().all()
    if len(others) != 1:
        return None  # 0 → no clash; >1 → ambiguous, let caller flag
    dup = others[0]
    if (
        dup.autodiscovery_rule_id == rule.id
        and dup.autodiscovery_pr_number is not None
        and not await _has_state(db, dup.id)
    ):
        return dup
    return None


async def _notify_once(
    db: AsyncSession,
    conn: VCSConnection,
    owner: str,
    repo: str,
    pr_number: int,
    ws: Workspace,
    head_sha: str,
    kind: str,
    body: str,
) -> None:
    """Post a PR comment at most once per (workspace, head_sha, kind).

    The poller calls reconcile_open_pr every cycle while a PR is open;
    without this the same rename/delete comment is re-posted every
    ~60s. We use an AuditLog row as a durable idempotency ledger
    (keyed by ws + head_sha + kind) so a new push (new head_sha)
    re-notifies but repeated cycles on the same commit do not.
    """
    seen = await db.execute(
        select(AuditLog.id).where(
            AuditLog.action == "autodiscovery.notified",
            AuditLog.resource_id == f"ws-{ws.id}",
            AuditLog.detail == f"{kind}:{head_sha}",
        )
    )
    if seen.scalar_one_or_none() is not None:
        return
    await _post_comment(conn, owner, repo, pr_number, body)
    db.add(
        AuditLog(
            id=generate_uuid7(),
            actor_email="system",
            actor_type="system",
            origin="system",
            action="autodiscovery.notified",
            resource_type="workspace",
            resource_id=f"ws-{ws.id}",
            status_code=200,
            detail=f"{kind}:{head_sha}",
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
    Comments are deduped per (workspace, head_sha) so the cyclic poller
    doesn't spam the PR. `file_changes is None` (truncated diff) → skip.
    """
    if file_changes is None:
        return
    cls = classify_for_rule(rule, file_changes)

    for d, d_stack in sorted(cls["deleted"]):
        ws = await _autodiscovered_ws(db, rule, d, d_stack)
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
        await _notify_once(
            db,
            conn,
            owner,
            repo,
            pr_number,
            ws,
            head_sha,
            "deleted",
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

    for (old, old_stack), (new, _new_stack) in cls["renamed"]:
        ws = await _autodiscovered_ws(db, rule, old, old_stack)
        if ws is None:
            continue
        await _notify_once(
            db,
            conn,
            owner,
            repo,
            pr_number,
            ws,
            head_sha,
            "renamed",
            f"♻️ Autodiscovery: detected rename `{old}` → `{new}`. On merge, "
            f"workspace `{ws.name}` will be **moved in place** "
            f"(state & history preserved — no destroy).",
        )

    for amb, amb_stack in sorted(cls["ambiguous"]):
        ws = await _autodiscovered_ws(db, rule, amb, amb_stack)
        if ws is None:
            continue
        await _notify_once(
            db,
            conn,
            owner,
            repo,
            pr_number,
            ws,
            head_sha,
            "ambiguous",
            f"❓ Autodiscovery: `{amb}` looks split/merged across multiple "
            f"directories — not treated as a clean rename. Workspace "
            f"`{ws.name}` will be left as *pending deletion* on merge for a "
            f"human to decide; new directories autodiscover normally.",
        )


async def _dir_absent_on_branch(
    conn: VCSConnection, owner: str, repo: str, branch: str, root: str, stack: str | None = None
) -> bool:
    """Re-verify the unit really is gone from the tracked branch before any
    flag/destroy. Returns False (do NOT act) if the tree can't be listed or is
    truncated — fail safe.

    For a Pulumi workspace the unit is the STACK FILE, not the directory
    (#1570): its siblings keep the directory alive, so a directory check would
    answer "still there" for a stack that has genuinely gone.
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
    if stack is not None:
        # A Pulumi workspace is one stack, so what has to be gone is that
        # stack's own file -- not the directory, which its siblings still
        # occupy. Checking the directory here would answer "still present" for
        # a genuinely removed stack, and the flag would never fire.
        candidates = {
            f"{root.rstrip('/')}/Pulumi.{stack}.yaml" if root else f"Pulumi.{stack}.yaml",
            f"{root.rstrip('/')}/Pulumi.{stack}.yml" if root else f"Pulumi.{stack}.yml",
        }
        return not any(p in candidates for p in tree)
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
    cls = classify_for_rule(rule, file_changes)

    # Renames: move the existing workspace in place (state preserved).
    for (old, old_stack), (new, new_stack) in cls["renamed"]:
        ws = await _autodiscovered_ws(db, rule, old, old_stack)
        if ws is None:
            continue
        # The open-PR autodiscovery will already have created a
        # speculative, never-applied workspace at `new` (PR-originated,
        # zero state). That is not a real conflict — absorb it so the
        # real (state/history-bearing) workspace can move in place.
        dup = await _absorbable_speculative_dup(db, rule, new, ws.id)
        if dup is not None:
            dup.lifecycle_state = "archived"
            dup.lifecycle_reason = (
                f"superseded by in-place rename move of '{old}' -> '{new}' "
                f"(never-applied speculative duplicate)"
            )
            # Free its name + directory before the moved workspace
            # adopts the derived name (workspaces.name is unique).
            dup.name = f"{dup.name}-superseded-{str(dup.id)[:8]}"[:90]
            dup.working_directory = f"{new}#superseded-{str(dup.id)[:8]}"
            dup.trigger_prefixes = []
            _audit(
                db,
                "autodiscovery.speculative_dup_absorbed",
                dup,
                f"absorbed into rename move {old} -> {new}",
            )
            await db.flush()
        else:
            clash = await db.execute(
                select(Workspace.id).where(
                    Workspace.vcs_connection_id == rule.vcs_connection_id,
                    Workspace.vcs_repo_url == rule.repo_url,
                    Workspace.working_directory == new,
                    Workspace.id != ws.id,
                    Workspace.lifecycle_state != "archived",
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
        if new_stack is not None:
            # The stack moved with it, and the name follows the stack rather
            # than the rule's template -- a Pulumi workspace IS `project::stack`
            # and a stale half would address different state.
            ws.stack = new_stack
            ws.name = derive_pulumi_workspace_name(new, new_stack)
        elif rule.name_template:
            ws.name = derive_workspace_name(rule, new)
        _audit(db, "autodiscovery.workspace_moved", ws, f"{old} -> {new}")
        logger.info(
            "Autodiscovery moved workspace on rename",
            workspace_id=str(ws.id),
            old=old,
            new=new,
        )

    # Deletes + ambiguous: only after re-verifying the unit is truly gone.
    for d, d_stack in sorted(cls["deleted"] | cls["ambiguous"]):
        ws = await _autodiscovered_ws(db, rule, d, d_stack)
        if ws is None:
            continue
        if not await _dir_absent_on_branch(conn, owner, repo, branch, d, d_stack):
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
            # Idempotency: the flag path self-dedupes (it flips
            # lifecycle_state so _autodiscovered_ws stops returning the
            # ws), but the destroy path leaves the ws `active` until the
            # run applies. Without this guard every branch advance of
            # any workspace sharing this rule/repo would queue ANOTHER
            # destroy → concurrent `terraform destroy` jobs for one
            # workspace. Only re-queue if no destroy is in flight/done
            # (a prior errored/canceled/discarded one may be retried).
            existing = await db.execute(
                select(Run.id).where(
                    Run.workspace_id == ws.id,
                    Run.is_destroy.is_(True),
                    Run.plan_only.is_(False),
                    Run.source == LIFECYCLE_SOURCE,
                    Run.status.notin_(["errored", "canceled", "discarded"]),
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue  # a destroy is already queued/running/done
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
    """Reconcile autodiscovered workspaces whose origin PR is no longer
    open.

    The decisive signal is **whether the origin PR merged**, NOT
    whether its directory is currently on the branch:

    - Origin PR **merged** → the workspace graduated to a normal tracked
      workspace. It is NOT an orphan. Shed the speculative PR link so we
      never reconsider it; any *later* directory disappearance is a
      delete/rename handled by `reconcile_branch_advance`. (Without this
      a long-lived workspace whose dir is later renamed would be
      wrongly archived here before the rename move runs.)
    - Origin PR **closed unmerged** → genuine orphan. Re-verify the dir
      really is absent, then: zero-state → archived; has-state →
      pending_deletion.
    - PR state **unknown** (deleted / transient fetch failure) → do
      nothing. Fail safe — never archive on uncertainty; retry next
      cycle.
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

        pr = await _get_pull_request(conn, owner, repo, ws.autodiscovery_pr_number)
        if pr is None:
            continue  # unknown — fail safe, retry next cycle
        if pr.merged:
            # Graduated: a real, merged workspace. Drop the speculative
            # link so the orphan reconciler never touches it again;
            # rename/delete of its dir is reconcile_branch_advance's job.
            _audit(
                db,
                "autodiscovery.graduated",
                ws,
                f"origin PR #{ws.autodiscovery_pr_number} merged — no longer speculative",
            )
            logger.info(
                "Autodiscovery graduated workspace (origin PR merged)",
                workspace_id=str(ws.id),
                pr=ws.autodiscovery_pr_number,
            )
            ws.autodiscovery_pr_number = None
            continue
        if pr.state == "open" or (pr.state or "").lower() in ("open", "opened"):
            continue  # reopened since we listed PRs — leave it alone

        # Origin PR closed WITHOUT merging → genuine orphan.
        if not await _dir_absent_on_branch(
            conn, owner, repo, branch, ws.working_directory, ws.stack
        ):
            continue  # dir somehow present — legitimate, do nothing
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
