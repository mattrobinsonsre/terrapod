"""Fetching a workspace's code from its VCS connection.

Extracted from the runs router (#1396) so the run-trigger path can use it too.
It was a private helper there, which is why the trigger path grew its own,
worse answer — reuse whichever configuration version happened to be newest —
rather than sharing this one.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import VCSConnection, Workspace
from terrapod.logging_config import get_logger
from terrapod.services import run_service

logger = get_logger(__name__)


class VCSConfigError(Exception):
    """The workspace's code cannot be fetched — a property of the workspace or
    its connection, not a transient provider failure."""


async def fetch_config_version(
    db: AsyncSession, ws: Workspace, *, ref_override: str = ""
) -> tuple[uuid.UUID, str, str]:
    """Download code from VCS and create a ConfigurationVersion.

    The one place code is fetched for a run that did not bring its own.
    Resolve branch -> get HEAD SHA -> download tarball -> create CV ->
    upload -> mark uploaded, which is the flow the VCS poller uses.

    Two callers, for the same reason — the workspace is VCS-connected, so
    its code comes from VCS:

      - a UI-queued run with no uploaded configuration version;
      - a run trigger firing on a VCS-connected destination (#1396). That
        one used to reuse whichever CV was newest, which could be a stale
        drift artifact or an unmerged pull request.

    Raises VCSConfigError for a workspace we cannot fetch for (inactive
    connection, unparseable URL, no resolvable branch). Provider transport
    failures propagate as httpx errors so callers can tell "your workspace is
    misconfigured" from "GitHub is having a bad afternoon" — the API maps the
    latter to 502/504 (#1358); the run-trigger path fails the trigger.

    When ref_override is set, fetches code from that branch/tag/SHA instead
    of the workspace's tracked branch.

    Returns (cv_id, commit_sha, ref_name).
    """
    from terrapod.services.vcs_archive_cache import VCSArchiveCache
    from terrapod.services.vcs_poller import (
        _get_branch_sha,
        _list_tags,
        _parse_repo_url,
        _resolve_branch,
        _stream_cv_upload_from_cache,
    )

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if not conn or conn.status != "active":
        raise VCSConfigError("VCS connection is not active")

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
        raise VCSConfigError("Cannot parse VCS repo URL")
    owner, repo = parsed

    if ref_override:
        # Try as branch first, then tag, then treat as raw SHA
        sha = await _get_branch_sha(conn, owner, repo, ref_override, meta=None)
        ref_name = ref_override
        if not sha:
            tags = await _list_tags(conn, owner, repo)
            tag_match = next((t for t in tags if t["name"] == ref_override), None)
            if tag_match:
                sha = tag_match["sha"]
            else:
                # Treat as raw SHA — providers accept any git ref
                sha = ref_override
    else:
        # The provider is reached here, so a provider outage lands here too.
        # These calls RAISE on an error status rather than returning None, so
        # the `if not sha` guard below never sees one. The httpx error is
        # deliberately NOT caught here: the caller decides what a provider
        # outage means. The API turns it into 502/504 rather than a bare 500
        # (#1358); the run-trigger path fails the trigger rather than reaching
        # for some other configuration version (#1396).
        ref_name = await _resolve_branch(conn, ws, owner, repo) or ""
        if not ref_name:
            raise VCSConfigError("Cannot determine VCS branch")
        sha = await _get_branch_sha(conn, owner, repo, ref_name, meta=None)
        if not sha:
            raise VCSConfigError("Cannot get branch HEAD SHA")

    # Streaming + storage cache: tarball never lands in process memory, and
    # subsequent UI fetches at the same SHA hit the cache (storage `head()`)
    # instead of re-downloading from the provider. Single-shot cache instance
    # is fine here — the in-process single-flight only matters when multiple
    # workspaces poll the same SHA concurrently.
    #
    # Path narrowing: pass this workspace's own `working_directory ∪
    # trigger_prefixes`. Cross-workspace coalescing isn't useful here
    # (one request, one workspace), so we don't bother computing a wider
    # union. If another caller fetched the same SHA with a different path
    # set, that's a different cache entry — content-addressed by paths_hash.
    #
    # If the workspace's terraform crosses directory boundaries via relative
    # module sources (`module "auth0" { source = "../auth0" }`), the operator
    # MUST declare the referenced paths in `trigger_prefixes` — sparse-checkout
    # cone mode includes parent directories but not siblings, so without an
    # explicit prefix the runner would `lstat ../foo: no such file`. This
    # contract is consistent with the VCS-poll path's `_compute_paths_unions`,
    # so a workspace that works under VCS poll also works here. See #478 /
    # #480 for the history (v0.35.3 briefly forced whole-repo here as a
    # blanket fix; v0.35.4 reverted that and put the responsibility back on
    # the workspace declaration).
    fetch_paths: list[str] = []
    if ws.working_directory:
        fetch_paths.append(ws.working_directory.strip("/ "))
    if ws.trigger_prefixes:
        fetch_paths.extend(p.strip("/ ") for p in ws.trigger_prefixes if p)
    fetch_paths = [p for p in fetch_paths if p]

    cache = VCSArchiveCache()
    cache_storage_key = await cache.get_or_fetch(conn, owner, repo, sha, paths=fetch_paths or None)

    cv = await run_service.create_configuration_version(
        db, workspace_id=ws.id, source="vcs", auto_queue_runs=False
    )
    await db.flush()

    await _stream_cv_upload_from_cache(cache_storage_key, ws.id, cv.id)

    cv = await run_service.mark_configuration_uploaded(db, cv)

    return cv.id, sha, ref_name
