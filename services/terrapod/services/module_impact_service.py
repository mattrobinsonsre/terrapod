"""Module impact analysis — speculative plans for module PRs and triggered runs on publish.

Registered with the distributed scheduler as:
- Periodic task: polls VCS-connected modules with workspace links for open PRs
- Trigger handler: fires when a module-test run reaches a terminal state

When a PR is opened against a module's VCS repo, override tarballs are uploaded to
object storage and speculative plan-only runs are queued on all linked workspaces.
Wrapper consumers receive the PR tarball via the module download override;
self-contained catalog instances instead plan a speculative root config version
containing that tarball.

When a new module version is published (tag-based or manual upload), standard runs
are queued on all linked workspaces to apply the updated module.
"""

import asyncio
import json
import tarfile
import uuid
import zlib

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.upload_stream import file_chunks
from terrapod.db.models import (
    CatalogItem,
    ModuleWorkspaceLink,
    RegistryModule,
    Run,
    VCSConnection,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import (
    catalog_service,
    github_service,
    gitlab_service,
    run_links,
    run_service,
    variable_service,
    vcs_rate_limit,
    vcs_status_comment,
)
from terrapod.services.archive_utils import strip_archive_top_level_dir_async
from terrapod.services.module_subdirectory import scope_archive_to_subdirectory
from terrapod.services.vcs_provider import PullRequest
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key, module_override_key, module_tarball_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

# Distinguishes "the payload did not carry this field" (an older replica's
# trigger, mid-upgrade) from a genuine None. See _post_module_vcs_status.
_UNSET = object()


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
            from_fork=bool(pr["from_fork"]),
            base_ref=pr["base_ref"],
        )
        for pr in prs
    ]


async def _download_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    # GHSA-v8g7-pqrj-8mcm. This is one of the three clone dispatchers the allowlist
    # did NOT reach: the guard added in this release sits on `vcs_provider`'s
    # dispatcher, which these modules do not use — they call the provider services
    # directly. So a module, policy set or registry entry whose URL predates a
    # narrowing kept being cloned, and `docs/security-hardening.md` claimed otherwise.
    from terrapod.services.vcs_connection_rbac import (
        RepositoryNotAllowed,
        repository_pair_allowed,
    )

    if not repository_pair_allowed(conn, owner, repo):
        raise RepositoryNotAllowed(
            f"VCS connection vcs-{getattr(conn, 'id', None)} is restricted to specific "
            f"repositories and {owner}/{repo} is not one of them, so its credential will "
            "not be used to clone it. Widen `allowed-repositories` on the connection, or "
            "clear it to allow any repository the credential can reach."
        )

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


async def handle_module_impact_immediate_poll(payload: dict) -> None:
    """Webhook-triggered immediate module impact poll for a specific repo."""
    repo = payload.get("repo", "unknown")
    logger.info("Immediate module impact poll triggered by webhook", repo=repo)
    await module_impact_poll_cycle()


@vcs_rate_limit.attributed("module-impact")
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
                # Attribute this module's spend to the module itself (#1339), so
                # a breakdown can say "this module is a third of the budget"
                # rather than only "module-impact is".
                with vcs_rate_limit.vcs_target(
                    consumer=f"{module.namespace}/{module.name}/{module.provider}",
                    kind="module",
                    labels=module.labels,
                ):
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
            status_complete = await _create_module_test_runs(
                db, storage, module, conn, owner, repo, pr
            )
            if status_complete:
                pr_shas[pr_num_str] = pr.head_sha
            # Commit after each PR so runs + module_overrides are visible to
            # listeners immediately when the SSE event fires (flush alone keeps
            # data in the uncommitted transaction, invisible to other sessions).
            module.vcs_last_pr_shas = pr_shas
            await db.commit()
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
    """Cancel only module-test runs still in progress for a closed PR."""
    for link in module.workspace_links:
        result = await db.execute(
            select(Run).where(
                Run.workspace_id == link.workspace_id,
                Run.source == "module-test",
                Run.module_impact_module_id == module.id,
                Run.vcs_pull_request_number == pr_number,
                run_service.pr_closure_cancels_sql(Run),
            )
        )
        for stale_run in result.scalars().all():
            try:
                await run_service.cancel_run(
                    db,
                    stale_run,
                    force=True,
                    reason=run_service.pr_closed_cancel_reason(pr_number),
                )
                logger.info(
                    "Canceled stale module-test run",
                    run_id=str(stale_run.id),
                    pr_number=pr_number,
                    module=module.name,
                )
            except ValueError as e:
                logger.info(
                    "Left module-test run alone; it is no longer cancelable",
                    run_id=str(stale_run.id),
                    status=stale_run.status,
                    pr_number=pr_number,
                    reason=str(e),
                )
            except Exception as e:
                logger.warning(
                    "Failed to cancel stale module-test run",
                    run_id=str(stale_run.id),
                    error=str(e),
                )


async def _fetch_workspace_config(
    db: AsyncSession,
    ws: Workspace,
    storage,  # type: ignore[no-untyped-def]
    *,
    speculative: bool = False,
) -> uuid.UUID | None:
    """Fetch a workspace's VCS code and create a ConfigurationVersion.

    Returns the config version ID, or None if the workspace has no VCS
    connection or the download fails.
    """
    if not ws.vcs_connection_id or not ws.vcs_repo_url:
        # Module impact analysis is about the MODULE changing (PR or publish) —
        # the consuming workspace's own VCS status is irrelevant. A non-VCS
        # consumer (a CLI-driven workspace, or a Service Catalog instance whose
        # code is a server-generated wrapper, #535) has no VCS to re-fetch, so
        # reuse its latest uploaded config-version instead of skipping it. The
        # publish-triggered run re-inits that config (an unpinned module
        # reference floats to the new version); the PR-speculative run carries
        # `module_overrides` to swap the PR module tarball by coordinate.
        # Gating impact on the consumer's VCS silently excluded every non-VCS
        # consumer of a VCS-linked module — a pre-existing defect.
        latest = await run_service.get_latest_uploaded_cv(db, ws.id)
        return latest.id if latest else None

    ws_conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if ws_conn is None or ws_conn.status != "active":
        return None

    parse_fn = _dispatch_parse_repo_url(ws_conn.provider)
    parsed = parse_fn(ws.vcs_repo_url)
    if parsed is None:
        return None

    ws_owner, ws_repo = parsed
    branch = ws.vcs_branch
    if not branch:
        branch = await _get_default_branch(ws_conn, ws_owner, ws_repo) or "main"

    try:
        archive = await _download_archive(ws_conn, ws_owner, ws_repo, branch)
    except Exception:
        logger.warning(
            "Failed to download workspace archive for module-test",
            workspace=ws.name,
            branch=branch,
            exc_info=True,
        )
        return None

    archive = await strip_archive_top_level_dir_async(archive)

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source="module-test",
        auto_queue_runs=False,
        speculative=speculative,
        archive_provenance="unprotected",
    )
    await db.flush()

    key = config_version_key(str(ws.id), str(cv.id))
    await storage.put(key, archive, content_type="application/x-tar")

    cv = await run_service.mark_configuration_uploaded(db, cv)
    return cv.id


async def _create_root_pr_configuration(
    db: AsyncSession,
    storage: ObjectStore,
    ws: Workspace,
    override_key: str,
    staged_path: str | None,
    module_id: uuid.UUID,
) -> tuple[uuid.UUID, str]:
    """Validate the PR root once, then upload a speculative CV for this instance."""
    owns_path = staged_path is None
    if staged_path is None:
        staged_path, _ = await catalog_service._stage_root_archive(storage, override_key)
    try:
        cv = await run_service.create_configuration_version(
            db,
            workspace_id=ws.id,
            source="module-test",
            auto_queue_runs=False,
            speculative=True,
            archive_provenance="protected",
            archive_module_id=module_id,
        )
        await db.flush()
        await storage.put_stream(
            config_version_key(str(ws.id), str(cv.id)),
            file_chunks(staged_path),
            content_type="application/gzip",
        )
        await run_service.mark_configuration_uploaded(db, cv)
        return cv.id, staged_path
    except BaseException:
        if owns_path:
            await catalog_service._remove_root_archive(staged_path)
        raise


async def _validate_root_instance_inputs(
    db: AsyncSession, item: CatalogItem, ws: Workspace, interface: dict
) -> None:
    """Check stored instance inputs against the candidate root before it can run."""
    inputs = interface["inputs"]
    fields = catalog_service.derive_form(item, inputs, [])
    names = {inp["name"] for inp in inputs if inp.get("name")}
    variables = {
        var.key: var
        for var in await variable_service.list_variables(db, ws.id)
        if var.category in ("native", "terraform")
    }
    removed = variables.keys() - names
    if removed:
        raise catalog_service.CatalogError(f"Removed input(s): {', '.join(sorted(removed))}")
    overrides = catalog_service._option_overrides(item)
    for inp in inputs:
        name = inp.get("name")
        var = variables.get(name)
        if name and overrides.get(name, {}).get("sensitive") and not inp.get("sensitive"):
            raise catalog_service.CatalogError(
                f"Root input {name} must declare sensitive = true in its module archive"
            )
        if var is not None and bool(var.sensitive) != bool(
            inp.get("sensitive") or overrides.get(name, {}).get("sensitive")
        ):
            raise catalog_service.CatalogError(f"Input {name} must be reconfigured for sensitivity")
        override = overrides.get(name, {})
        if not override.get("hidden") or override.get("default") is None:
            continue
        expected, structured = catalog_service._input_var_repr(override["default"])
        if var is None or var.structured != structured or var.value != expected:
            raise catalog_service.CatalogError(f"Hidden input {name} must be reconfigured")
    input_values = {}
    for field in fields:
        var = variables.get(field["name"])
        if var is None:
            continue
        try:
            input_values[field["name"]] = json.loads(var.value) if var.structured else var.value
        except (ValueError, TypeError) as e:
            raise catalog_service.CatalogError(f"Input {field['name']} must be reconfigured") from e
    catalog_service._resolve_inputs(item, inputs, [], input_values)
    required = {
        inp["name"]
        for inp in inputs
        if inp.get("name") and inp.get("required") and inp.get("default") is None
    }
    missing = required - variables.keys()
    if missing:
        raise catalog_service.CatalogError(
            f"Missing required input(s): {', '.join(sorted(missing))}"
        )


async def _post_unusable_module_pr_status(
    conn: VCSConnection,
    owner: str,
    repo: str,
    pr: PullRequest,
    ws: Workspace,
    description: str,
) -> bool:
    """Report an unusable candidate; retry transient failures on the next poll."""
    try:
        if conn.provider == "gitlab":
            await gitlab_service.create_commit_status(
                conn,
                owner,
                repo,
                pr.head_sha,
                state="failed",
                description=description,
                context=f"terrapod/{ws.name}",
            )
        else:
            await github_service.create_commit_status(
                conn,
                owner,
                repo,
                pr.head_sha,
                state="failure",
                description=description,
                context=f"terrapod/{ws.name}",
            )
    except httpx.HTTPStatusError as e:
        logger.warning("Failed to post invalid module PR status", exc_info=True)
        return 400 <= e.response.status_code < 500  # A definitive 4xx cannot be retried.
    except Exception:
        logger.warning("Failed to post invalid module PR status", exc_info=True)
        return False
    return True


async def _queue_module_pr_run(
    db: AsyncSession,
    module: RegistryModule,
    ws: Workspace,
    pr: PullRequest,
    cv_id: uuid.UUID,
    overrides: dict[str, str],
) -> Run | None:
    """Queue a plan-only run and retain its module PR status attribution."""
    try:
        run = await run_service.create_run(
            db,
            workspace=ws,
            message=f"Module impact: PR #{pr.number} on {module.name}/{module.provider} — {pr.title}",
            source="module-test",
            plan_only=True,
            configuration_version_id=cv_id,
            created_by="module-impact-analysis",
        )
    except run_service.PlanApplyNotEnabled:
        return None
    # Overrides can be cleared by blob retention; attribution must survive them.
    run.module_overrides = overrides
    run.module_impact_module_id = module.id
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
    return run


async def _create_module_test_runs(
    db: AsyncSession,
    storage,  # type: ignore[no-untyped-def]
    module: RegistryModule,
    conn: VCSConnection,
    owner: str,
    repo: str,
    pr: PullRequest,
) -> bool:
    """Download PR archive, upload override tarball, and create speculative runs."""
    # A module PR from a fork reaches further than a workspace PR does: it
    # creates a plan on every workspace that consumes the module, each with its
    # own credentials. Each of those workspaces decides for itself (the per-
    # workspace gate is in the run loop below); this early return just avoids
    # downloading and storing an override tarball nobody will use.
    if pr.from_fork and not any(
        link.workspace is not None and link.workspace.allow_fork_pr_plans
        for link in module.workspace_links
    ):
        logger.info(
            "module_impact.fork_pr_skipped",
            module=module.name,
            pr_number=pr.number,
            head_sha=pr.head_sha,
        )
        return True

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
        return False

    # Strip top-level directory wrapper from VCS archive before storing
    try:
        archive_bytes = await strip_archive_top_level_dir_async(archive_bytes)

        # A submodule (#1583) is tested as it is published: its own subdirectory,
        # re-rooted — not the whole repository.
        if module.subdirectory:
            scoped = await asyncio.to_thread(
                scope_archive_to_subdirectory, archive_bytes, module.subdirectory
            )
            if scoped is None:
                logger.info(
                    "Module PR has nothing under the module's subdirectory; no impact runs",
                    module=module.name,
                    pr_number=pr.number,
                    subdirectory=module.subdirectory,
                )
                return True
            archive_bytes = scoped
    except (tarfile.TarError, EOFError, OSError, zlib.error):
        logger.warning("Cannot repack module PR archive", module=module.name, exc_info=True)
        posted = True
        for link in module.workspace_links:
            ws = link.workspace
            if ws is not None and (not pr.from_fork or ws.allow_fork_pr_plans):
                posted &= await _post_unusable_module_pr_status(
                    conn, owner, repo, pr, ws, "Module PR archive cannot be tested"
                )
        return posted

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
                Run.module_impact_module_id == module.id,
                Run.vcs_pull_request_number == pr.number,
                Run.status.notin_(run_service.TERMINAL_STATES),
                Run.vcs_commit_sha.is_distinct_from(pr.head_sha),
            )
        )
        for stale_run in result.scalars().all():
            try:
                await run_service.cancel_run(db, stale_run, force=True)
            except Exception:
                pass

    root_path = None
    root_interface = None
    root_error = False
    queued_any = False
    status_complete = True
    try:
        # Create speculative plan-only runs on each linked workspace.
        for link in module.workspace_links:
            ws = link.workspace
            if ws is None:
                continue

            # Per-workspace, because the setting is per-workspace: one consumer
            # opting in to fork PRs does not volunteer the others' credentials.
            if pr.from_fork and not ws.allow_fork_pr_plans:
                logger.info(
                    "module_impact.fork_pr_skipped_for_workspace",
                    workspace=ws.name,
                    module=module.name,
                    pr_number=pr.number,
                )
                continue

            existing = await db.execute(
                select(Run.id)
                .where(
                    Run.workspace_id == ws.id,
                    Run.source == "module-test",
                    Run.status != "canceled",
                    Run.module_impact_module_id == module.id,
                    Run.vcs_pull_request_number == pr.number,
                    Run.vcs_commit_sha == pr.head_sha,
                )
                .limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                continue

            item = await db.get(CatalogItem, ws.catalog_item_id) if ws.catalog_item_id else None
            if item is not None and not catalog_service.runtime_matches_item(item, ws):
                logger.info(
                    "Skipping module-test run — catalog runtime is no longer allowed",
                    workspace=ws.name,
                )
                continue
            if item is not None and item.self_contained and item.module_id == module.id:
                if not root_error:
                    try:
                        if root_path is None:
                            root_path, root_interface = await catalog_service._stage_root_archive(
                                storage, override_key
                            )
                        await _validate_root_instance_inputs(db, item, ws, root_interface)
                        cv_id, root_path = await _create_root_pr_configuration(
                            db, storage, ws, override_key, root_path, module.id
                        )
                    except catalog_service.CatalogError:
                        if root_path is None:
                            root_error = True
                        logger.warning(
                            "Skipping self-contained module PR plan: invalid candidate or inputs",
                            module=module.name,
                            pr_number=pr.number,
                        )
                        status_complete &= await _post_unusable_module_pr_status(
                            conn, owner, repo, pr, ws, "Module PR candidate cannot be tested"
                        )
                        continue
                else:
                    status_complete &= await _post_unusable_module_pr_status(
                        conn, owner, repo, pr, ws, "Module PR candidate cannot be tested"
                    )
                    continue
            else:
                # Wrapper consumers still use their own configuration plus
                # the registry module override at init.
                cv_id = await _fetch_workspace_config(db, ws, storage, speculative=True)
            if cv_id is None:
                logger.warning(
                    "Skipping module-test run — could not fetch workspace config",
                    workspace=ws.name,
                    module=module.name,
                )
                continue

            run = await _queue_module_pr_run(db, module, ws, pr, cv_id, overrides)
            if run is None:
                logger.info(
                    "Skipping module-test run — workspace declares no plan/apply operation",
                    workspace=ws.name,
                )
                continue
            queued_any = True
            await _enqueue_module_vcs_status(run, module, "pending")
    finally:
        if root_path is not None:
            await catalog_service._remove_root_archive(root_path)
        if not queued_any:
            # Override keys are shared by SHA. Do not delete one another PR's
            # run still uses, but leave no orphan when validation refused every run.
            try:
                referenced = await db.execute(
                    select(Run.id)
                    .where(Run.module_overrides[module_coord].astext == override_key)
                    .limit(1)
                )
                if referenced.scalar_one_or_none() is None:
                    await storage.delete(override_key)
            except Exception:
                logger.warning("Failed to clean up unused module PR archive", exc_info=True)
    return status_complete


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

    storage = get_storage()
    runs: list[Run] = []
    for link in links:
        ws = link.workspace
        if ws is None:
            continue

        run_version = version
        item = await db.get(CatalogItem, ws.catalog_item_id) if ws.catalog_item_id else None
        if item is not None and not catalog_service.runtime_matches_item(item, ws):
            ws.catalog_update_block = {
                "version": ws.catalog_version_pin or version,
                "reason": "runtime",
            }
            logger.info(
                "Skipping module-publish run — catalog runtime is no longer allowed",
                workspace=ws.name,
            )
            continue
        if (
            item is not None
            and item.self_contained
            and item.module_id == module.id
            and ws.catalog_version_pin is None
        ):
            path = None
            try:
                selected = await catalog_service._resolve_module_version(db, module.id, None)
                if selected is None:
                    raise catalog_service.CatalogError("No uploaded module version is available")
                run_version = selected.version
                path, interface = await catalog_service._stage_root_archive(
                    storage,
                    module_tarball_key(
                        module.namespace, module.name, module.provider, selected.version
                    ),
                    item.ansible_directory if item.configure_enabled else None,
                )
                await _validate_root_instance_inputs(db, item, ws, interface)

                cv = await run_service.create_configuration_version(
                    db,
                    workspace_id=ws.id,
                    source="module-publish",
                    auto_queue_runs=False,
                    archive_provenance="protected",
                    archive_module_id=module.id,
                )
                await db.flush()
                await storage.put_stream(
                    config_version_key(str(ws.id), str(cv.id)),
                    file_chunks(path),
                    content_type="application/gzip",
                )
                await run_service.mark_configuration_uploaded(db, cv)
                cv_id = cv.id
            except catalog_service.CatalogError as e:
                ws.catalog_update_block = {
                    "version": run_version,
                    "reason": "inputs" if path is not None else "archive",
                }
                logger.warning(
                    "Skipping self-contained catalog publish run; reconfigure instance",
                    workspace=ws.name,
                    version=version,
                    reason=str(e),
                )
                continue
            finally:
                if path is not None:
                    await catalog_service._remove_root_archive(path)
        else:
            # Wrapper and pinned instances retain their existing publish behavior.
            cv_id = await _fetch_workspace_config(db, ws, storage)
        if cv_id is None:
            logger.warning(
                "Skipping module-publish run — could not fetch workspace config",
                workspace=ws.name,
                module=f"{module.name}/{module.provider}",
            )
            continue

        try:
            run = await run_service.create_run(
                db,
                workspace=ws,
                message=f"Module {module.name}/{module.provider} v{run_version} published",
                source="module-publish",
                plan_only=False,
                configuration_version_id=cv_id,
                created_by="module-impact-analysis",
            )
        except run_service.PlanApplyNotEnabled:
            logger.info(
                "Skipping module-publish run — workspace declares no plan/apply operation",
                workspace=ws.name,
            )
            continue
        if commit_sha:
            run.vcs_commit_sha = commit_sha
        await db.flush()

        run = await run_service.queue_run(db, run)
        if item is not None and (
            (item.self_contained and ws.catalog_version_pin is None)
            or (ws.catalog_update_block or {}).get("reason") == "runtime"
        ):
            ws.catalog_update_block = None
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
    # `_UNSET` rather than None: None is a meaningful value here ("the plan did
    # not record it"), so it has to be distinguishable from "the payload predates
    # this field", which is the case for a trigger enqueued by an older replica
    # mid-upgrade.
    payload_has_changes = payload.get("has_changes", _UNSET)

    if not run_id_str or not target_status:
        return

    async with get_db_session() as db:
        run = await db.get(Run, uuid.UUID(run_id_str))
        if run is None or run.source not in ("module-test",):
            return

        if not run.vcs_commit_sha:
            return

        # A legacy override records only a coordinate; after deletion, that
        # coordinate can belong to another module and must not route a status.
        if not run.module_impact_module_id:
            return
        module = await db.get(RegistryModule, run.module_impact_module_id)
        if module is None:
            return

        await _post_module_vcs_status(
            db, run, module, target_status, payload_has_changes=payload_has_changes
        )


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
    payload_has_changes: object = _UNSET,
) -> None:
    """Post commit status and PR comment to the module's VCS repo.

    `payload_has_changes` is the value snapshotted at the transition. Prefer it
    over the row: this handler can run before the transaction that set the
    status has committed, and reading the row then yields the PREVIOUS value —
    which rendered a completed plan as "Plan finished" rather than "No changes"
    (#1378). Falls back to the row when the payload predates the field.
    """
    from terrapod.services.vcs_status_dispatcher import _resolve_status

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

    # Build target URL. "" rather than None: the commit-status clients take a
    # plain string.
    target_url = run_links.run_url(run.workspace_id, run.id) or ""

    # Post commit status
    has_changes = (
        payload_has_changes  # type: ignore[assignment]
        if payload_has_changes is not _UNSET
        else run.has_changes
    )
    github_state, gitlab_state, description = _resolve_status(
        target_status, run.plan_only, has_changes
    )
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
                context=context,
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

    # One comment on the module's PR, listing every consuming workspace
    # (#1940).
    #
    # This used to post one comment per consumer, keyed on the workspace id,
    # so a module with ten linked workspaces put ten comments on a single PR
    # — and each was reposted on every push, because the commit SHA was part
    # of the identity too. Now every consumer is a row of the same table a
    # workspace PR gets, in one comment, edited in place.
    #
    # Called once per run rather than once per PR, so several consumers'
    # transitions can arrive together; `_post_or_update` holds a per-PR lock
    # and every call renders the whole table from current rows, so they
    # converge on one comment instead of racing to create several.
    if run.vcs_pull_request_number:
        link_rows = await db.execute(
            select(ModuleWorkspaceLink.workspace_id).where(
                ModuleWorkspaceLink.module_id == module.id
            )
        )
        await vcs_status_comment.refresh_module_pr_comment(
            db,
            conn,
            f"{owner}/{repo}",
            run.vcs_pull_request_number,
            list(link_rows.scalars().all()),
            module.id,
            run.vcs_commit_sha,
        )
