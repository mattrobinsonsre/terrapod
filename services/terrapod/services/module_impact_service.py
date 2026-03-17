"""Module impact analysis — speculative plans for module PRs and triggered runs on publish.

Registered with the distributed scheduler as:
- Periodic task: polls VCS-connected modules with workspace links for open PRs
- Trigger handler: fires when a module-test run reaches a terminal state

When a PR is opened against a module's VCS repo, override tarballs are uploaded to
object storage and speculative plan-only runs are queued on all linked workspaces.
The runner's terraform init transparently receives the PR branch tarball via the
module download endpoint override mechanism.

When a new module version is published (tag-based or manual upload), standard runs
are queued on all linked workspaces to apply the updated module.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import (
    ModuleWorkspaceLink,
    RegistryModule,
    Run,
    VCSConnection,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_service
from terrapod.services.vcs_provider import PullRequest
from terrapod.storage import get_storage
from terrapod.storage.keys import module_override_key

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# VCS dispatch helpers (shared with registry_vcs_poller / vcs_poller)
# ---------------------------------------------------------------------------


def _dispatch_parse_repo_url(provider: str):  # type: ignore[no-untyped-def]
    if provider == "github":
        return github_service.parse_repo_url
    elif provider == "gitlab":
        return gitlab_service.parse_repo_url
    raise ValueError(f"Unsupported VCS provider: {provider}")


async def _list_open_prs(
    conn: VCSConnection, owner: str, repo: str, base_branch: str
) -> list[PullRequest]:
    if conn.provider == "gitlab":
        return await gitlab_service.list_open_prs(conn, owner, repo, base_branch)
    prs = await github_service.list_open_pull_requests(conn, owner, repo, base_branch)
    return [
        PullRequest(
            number=pr["number"],
            head_sha=pr["head_sha"],
            head_ref=pr["head_ref"],
            title=pr["title"],
        )
        for pr in prs
    ]


async def _download_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    if conn.provider == "gitlab":
        return await gitlab_service.download_archive(conn, owner, repo, ref)
    return await github_service.download_repo_archive(conn, owner, repo, ref)


async def _get_default_branch(conn: VCSConnection, owner: str, repo: str) -> str | None:
    if conn.provider == "gitlab":
        return await gitlab_service.get_default_branch(conn, owner, repo)
    return await github_service.get_repo_default_branch(conn, owner, repo)


# ---------------------------------------------------------------------------
# Periodic task: poll module PRs for linked workspaces
# ---------------------------------------------------------------------------


async def module_impact_poll_cycle() -> None:
    """Poll VCS-connected modules (with workspace links) for open PRs."""
    async with get_db_session() as db:
        storage = get_storage()

        # Only poll modules that have at least one workspace link
        result = await db.execute(
            select(RegistryModule)
            .where(
                RegistryModule.source == "vcs",
                RegistryModule.vcs_connection_id.isnot(None),
                RegistryModule.vcs_repo_url != "",
            )
            .options(
                selectinload(RegistryModule.workspace_links).selectinload(
                    ModuleWorkspaceLink.workspace
                ),
            )
        )
        modules = [m for m in result.scalars().all() if m.workspace_links]

        if not modules:
            return

        logger.info("Module impact poll starting", module_count=len(modules))

        for module in modules:
            try:
                await _poll_module_prs(db, storage, module)
            except Exception:
                logger.warning(
                    "Module impact poll failed for module",
                    module_id=str(module.id),
                    module_name=module.name,
                    exc_info=True,
                )

        await db.commit()
        logger.info("Module impact poll complete")


async def _poll_module_prs(
    db: AsyncSession,
    storage,  # type: ignore[no-untyped-def]
    module: RegistryModule,
) -> None:
    """Poll a single module's VCS repo for open PRs and create speculative runs."""
    conn = await db.get(VCSConnection, module.vcs_connection_id)
    if conn is None or conn.status != "active":
        return

    parse_fn = _dispatch_parse_repo_url(conn.provider)
    parsed = parse_fn(module.vcs_repo_url)
    if parsed is None:
        logger.warning("Cannot parse module repo URL", url=module.vcs_repo_url)
        return

    owner, repo = parsed

    # Determine base branch (module's configured branch or repo default)
    base_branch = module.vcs_branch
    if not base_branch:
        base_branch = await _get_default_branch(conn, owner, repo) or "main"

    prs = await _list_open_prs(conn, owner, repo, base_branch)

    # Load existing PR SHA tracking
    pr_shas: dict[str, str] = dict(module.vcs_last_pr_shas or {})
    open_pr_numbers = {str(pr.number) for pr in prs}

    # Cancel runs for PRs that are no longer open (closed/merged)
    for pr_num_str in list(pr_shas.keys()):
        if pr_num_str not in open_pr_numbers:
            await _cancel_stale_module_runs(db, module, int(pr_num_str))
            del pr_shas[pr_num_str]

    for pr in prs:
        pr_num_str = str(pr.number)
        prev_sha = pr_shas.get(pr_num_str)

        if prev_sha == pr.head_sha:
            continue  # No new commits on this PR

        # New or updated PR — create speculative runs
        try:
            await _create_module_test_runs(db, storage, module, conn, owner, repo, pr)
            pr_shas[pr_num_str] = pr.head_sha
        except Exception:
            logger.warning(
                "Failed to create module test runs",
                module_name=module.name,
                pr_number=pr.number,
                exc_info=True,
            )

    module.vcs_last_pr_shas = pr_shas
    await db.flush()


async def _cancel_stale_module_runs(
    db: AsyncSession,
    module: RegistryModule,
    pr_number: int,
) -> None:
    """Cancel active module-test runs for a closed/merged PR."""
    for link in module.workspace_links:
        result = await db.execute(
            select(Run).where(
                Run.workspace_id == link.workspace_id,
                Run.source == "module-test",
                Run.vcs_pull_request_number == pr_number,
                Run.status.notin_(run_service.TERMINAL_STATES),
            )
        )
        for stale_run in result.scalars().all():
            try:
                await run_service.cancel_run(db, stale_run)
                logger.info(
                    "Canceled stale module-test run",
                    run_id=str(stale_run.id),
                    pr_number=pr_number,
                    module=module.name,
                )
            except Exception as e:
                logger.warning(
                    "Failed to cancel stale module-test run",
                    run_id=str(stale_run.id),
                    error=str(e),
                )


async def _create_module_test_runs(
    db: AsyncSession,
    storage,  # type: ignore[no-untyped-def]
    module: RegistryModule,
    conn: VCSConnection,
    owner: str,
    repo: str,
    pr: PullRequest,
) -> None:
    """Download PR archive, upload override tarball, and create speculative runs."""
    # Download archive from PR head
    try:
        archive_bytes = await _download_archive(conn, owner, repo, pr.head_sha)
    except Exception:
        logger.warning(
            "Failed to download archive for module PR",
            module=module.name,
            pr_number=pr.number,
            exc_info=True,
        )
        return

    # Upload override tarball (keyed by commit SHA for reuse across workspaces/retries)
    override_key = module_override_key(pr.head_sha, module.namespace, module.name, module.provider)
    await storage.put(override_key, archive_bytes, "application/gzip")

    # Build overrides dict for this module
    module_coord = f"{module.namespace}/{module.name}/{module.provider}"
    overrides = {module_coord: override_key}

    # Cancel existing non-terminal module-test runs for this PR (superseded)
    for link in module.workspace_links:
        result = await db.execute(
            select(Run).where(
                Run.workspace_id == link.workspace_id,
                Run.source == "module-test",
                Run.vcs_pull_request_number == pr.number,
                Run.status.notin_(run_service.TERMINAL_STATES),
            )
        )
        for stale_run in result.scalars().all():
            try:
                await run_service.cancel_run(db, stale_run)
            except Exception:
                pass

    # Create speculative plan-only runs on each linked workspace
    for link in module.workspace_links:
        ws = link.workspace
        if ws is None:
            continue

        run = await run_service.create_run(
            db,
            workspace=ws,
            message=f"Module impact: PR #{pr.number} on {module.name}/{module.provider} — {pr.title}",
            source="module-test",
            plan_only=True,
            created_by="module-impact-analysis",
        )
        run.module_overrides = overrides
        run.vcs_commit_sha = pr.head_sha
        run.vcs_branch = pr.head_ref
        run.vcs_pull_request_number = pr.number
        await db.flush()

        run = await run_service.queue_run(db, run)

        logger.info(
            "Module-test run created",
            run_id=str(run.id),
            module=f"{module.name}/{module.provider}",
            workspace=ws.name,
            pr_number=pr.number,
            head_sha=pr.head_sha[:8],
        )

        # Enqueue VCS commit status for the module repo
        await _enqueue_module_vcs_status(run, module, "pending")


# ---------------------------------------------------------------------------
# Triggered runs on module version publish
# ---------------------------------------------------------------------------


async def trigger_linked_workspace_runs(
    db: AsyncSession,
    module: RegistryModule,
    version: str,
    commit_sha: str = "",
) -> list[Run]:
    """Queue standard runs on all linked workspaces when a module version is published.

    Called from registry_module_service (manual upload) and registry_vcs_poller
    (tag-based auto-publish).
    """
    result = await db.execute(
        select(ModuleWorkspaceLink)
        .where(ModuleWorkspaceLink.module_id == module.id)
        .options(selectinload(ModuleWorkspaceLink.workspace))
    )
    links = list(result.scalars().all())

    if not links:
        return []

    runs: list[Run] = []
    for link in links:
        ws = link.workspace
        if ws is None:
            continue

        run = await run_service.create_run(
            db,
            workspace=ws,
            message=f"Module {module.name}/{module.provider} v{version} published",
            source="module-publish",
            plan_only=False,
            created_by="module-impact-analysis",
        )
        if commit_sha:
            run.vcs_commit_sha = commit_sha
        await db.flush()

        run = await run_service.queue_run(db, run)
        runs.append(run)

        logger.info(
            "Module publish run created",
            run_id=str(run.id),
            module=f"{module.name}/{module.provider}",
            version=version,
            workspace=ws.name,
        )

    return runs


# ---------------------------------------------------------------------------
# Trigger handler: module-test run completed
# ---------------------------------------------------------------------------


async def handle_module_test_completed(payload: dict) -> None:
    """Post VCS commit status to the module's repo when a module-test run finishes."""
    run_id_str = payload.get("run_id", "")
    target_status = payload.get("target_status", "")

    if not run_id_str or not target_status:
        return

    async with get_db_session() as db:
        run = await db.get(Run, uuid.UUID(run_id_str))
        if run is None or run.source not in ("module-test",):
            return

        if not run.module_overrides or not run.vcs_commit_sha:
            return

        # Find the module from the override keys
        module = await _resolve_module_from_overrides(db, run.module_overrides)
        if module is None:
            return

        await _post_module_vcs_status(db, run, module, target_status)


async def _resolve_module_from_overrides(
    db: AsyncSession,
    overrides: dict,
) -> RegistryModule | None:
    """Resolve the RegistryModule from a module_overrides dict."""
    for coord in overrides:
        parts = coord.split("/")
        if len(parts) == 3:
            namespace, name, provider = parts
            result = await db.execute(
                select(RegistryModule).where(
                    RegistryModule.namespace == namespace,
                    RegistryModule.name == name,
                    RegistryModule.provider == provider,
                )
            )
            module = result.scalars().first()
            if module:
                return module
    return None


async def _enqueue_module_vcs_status(
    run: Run,
    module: RegistryModule,
    target_status: str,
) -> None:
    """Enqueue a VCS commit status trigger for a module-test run."""
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "module_test_completed",
            {
                "run_id": str(run.id),
                "target_status": target_status,
            },
            dedup_key=f"modtest:{run.id}:{target_status}",
            dedup_ttl=60,
        )
    except Exception as e:
        logger.warning("Failed to enqueue module test status", error=str(e))


async def _post_module_vcs_status(
    db: AsyncSession,
    run: Run,
    module: RegistryModule,
    target_status: str,
) -> None:
    """Post commit status and PR comment to the module's VCS repo."""
    from terrapod.config import settings
    from terrapod.services.vcs_status_dispatcher import (
        _build_comment_body,
        _find_or_create_comment,
        _resolve_status,
    )

    if not module.vcs_connection_id:
        return

    conn = await db.get(VCSConnection, module.vcs_connection_id)
    if conn is None or conn.status != "active":
        return

    parse_fn = _dispatch_parse_repo_url(conn.provider)
    parsed = parse_fn(module.vcs_repo_url)
    if parsed is None:
        return

    owner, repo = parsed

    # Resolve workspace name for the comment
    ws = await db.get(Workspace, run.workspace_id)
    ws_name = ws.name if ws else str(run.workspace_id)

    # Build target URL
    target_url = ""
    if settings.external_url:
        target_url = (
            f"{settings.external_url.rstrip('/')}/workspaces/{run.workspace_id}/runs/{run.id}"
        )

    # Post commit status
    github_state, gitlab_state, description = _resolve_status(target_status, run.plan_only)
    context = f"terrapod/{ws_name}"

    try:
        if conn.provider == "gitlab":
            await gitlab_service.create_commit_status(
                conn,
                owner,
                repo,
                run.vcs_commit_sha,
                state=gitlab_state,
                description=description,
                target_url=target_url,
            )
        else:
            await github_service.create_commit_status(
                conn,
                owner,
                repo,
                run.vcs_commit_sha,
                state=github_state,
                description=description,
                target_url=target_url,
                context=context,
            )
    except Exception as e:
        logger.warning("Failed to post module VCS commit status", error=str(e))

    # Post PR comment
    if run.vcs_pull_request_number:
        run_url = target_url or f"run-{run.id}"
        body = _build_comment_body(
            workspace_name=ws_name,
            workspace_id=str(run.workspace_id),
            run_id=f"run-{run.id}",
            run_status=target_status,
            plan_only=run.plan_only,
            has_changes=run.has_changes,
            run_url=run_url,
        )
        await _find_or_create_comment(
            conn, owner, repo, run.vcs_pull_request_number, str(run.workspace_id), body
        )
