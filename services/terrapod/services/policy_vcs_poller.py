"""Policy VCS poller — syncs .rego files from git repos into policy sets.

For each PolicySet with source=vcs, checks the tracked branch for new
commits. On a new commit, downloads the archive, extracts .rego files
from the configured policy_path, and upserts them into the policies
table. Deletes policies whose .rego files no longer exist in the repo.

Registered as a periodic task alongside vcs_poll and registry_vcs_poll.
"""

import asyncio
import io
import os
import posixpath
import re
import tarfile
import tempfile
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from terrapod.db.models import Policy, PolicySet, VCSConnection, now_utc
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service
from terrapod.services.scheduler import enqueue_trigger
from terrapod.services.vcs_provider import (
    get_branch_sha as _provider_get_branch_sha,
)
from terrapod.services.vcs_provider import (
    get_default_branch as _provider_get_default_branch,
)
from terrapod.services.vcs_provider import (
    parse_repo_url as _provider_parse_repo_url,
)

logger = get_logger(__name__)

# Max archive size (256 MB) — defence against pathological repos OOMing the worker.
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+terrapod\s*(#.*)?$")
_DENY_RULE_RE = re.compile(r"(?m)^\s*deny\s+(contains|:=|=)")


def _parse_repo_url(conn: VCSConnection, repo_url: str) -> tuple[str, str] | None:
    return _provider_parse_repo_url(conn, repo_url)


async def _get_default_branch(conn: VCSConnection, owner: str, repo: str) -> str | None:
    return await _provider_get_default_branch(conn, owner, repo)


async def _get_branch_sha(conn: VCSConnection, owner: str, repo: str, branch: str) -> str | None:
    return await _provider_get_branch_sha(conn, owner, repo, branch)


def _extract_rego_files(archive_bytes: bytes, policy_path: str) -> dict[str, str]:
    """Extract .rego files from a tarball at the given path.

    Returns {policy_name: rego_content} where policy_name is the filename
    without extension. Only direct children of policy_path are included
    (no recursive descent into subdirectories).
    """
    policies: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".rego"):
                continue

            # Reject path traversal: absolute paths or .. components.
            if member.name.startswith("/") or member.name.startswith(".."):
                continue
            normalized = posixpath.normpath(member.name)
            if normalized.startswith(".."):
                continue

            parts = member.name.split("/", 1)
            if len(parts) < 2:
                continue
            relative_path = parts[1]

            target_dir = policy_path.strip("/")
            if target_dir:
                if not relative_path.startswith(target_dir + "/"):
                    continue
                remainder = relative_path[len(target_dir) + 1 :]
            else:
                remainder = relative_path

            if "/" in remainder:
                continue

            name = os.path.splitext(remainder)[0]
            f = tar.extractfile(member)
            if f is not None:
                policies[name] = f.read().decode("utf-8")
    return policies


def _resolve_tmpdir() -> str | None:
    """Reuse the VCS tmpdir setting — same PVC, same sweep behaviour
    as vcs_archive_cache / cv_diff / provider_cache. On the API pod
    `/tmp` is tmpfs (RAM); the PVC mounted here is real disk so
    multi-hundred-MB downloads don't blow the memory budget."""
    from terrapod.config import settings

    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


async def _download_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    """Download archive via streaming with a size cap enforced before memory load.

    Uses the provider's stream-to-file path (chunked writes to disk, ~1 MB
    in memory at any time) so an adversarial multi-hundred-MB repo cannot
    OOM the API replica. After the streamed download completes, the total
    byte count is checked BEFORE reading into memory. Only archives under
    the cap are loaded for tarfile extraction.

    Tempfile lands on the CSP-attached PVC (`settings.vcs.tmpdir`) rather
    than node tmpfs — see `_resolve_tmpdir`.
    """
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False, dir=_resolve_tmpdir()) as tmp:
        tmp_path = tmp.name

    try:
        if conn.provider == "gitlab":
            written = await gitlab_service.download_archive_to_file(
                conn, owner, repo, ref, tmp_path
            )
        else:
            written = await github_service.download_repo_archive_to_file(
                conn, owner, repo, ref, tmp_path
            )

        if written > _MAX_ARCHIVE_BYTES:
            raise ValueError(
                f"Archive exceeds {_MAX_ARCHIVE_BYTES // (1024 * 1024)} MB limit ({written} bytes)"
            )

        return await asyncio.to_thread(_read_file, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _sync_policy_set(db: AsyncSession, ps: PolicySet) -> None:
    """Sync a single VCS policy set."""
    conn = ps.vcs_connection
    if conn is None:
        ps.vcs_last_error = "VCS connection deleted"
        return

    parsed = _parse_repo_url(conn, ps.vcs_repo_url)
    if parsed is None:
        ps.vcs_last_error = f"Cannot parse repo URL: {ps.vcs_repo_url}"
        return

    owner, repo = parsed
    branch = ps.vcs_branch

    try:
        if not branch:
            branch = await _get_default_branch(conn, owner, repo) or "main"

        sha = await _get_branch_sha(conn, owner, repo, branch)
        if sha is None:
            ps.vcs_last_error = f"Branch '{branch}' not found"
            return

        if sha == ps.vcs_last_commit_sha:
            return

        archive = await _download_archive(conn, owner, repo, sha)
        rego_files = await asyncio.to_thread(_extract_rego_files, archive, ps.policy_path)
        rego_files = {
            name: rego
            for name, rego in rego_files.items()
            if _PACKAGE_RE.search(rego) and _DENY_RULE_RE.search(rego)
        }

        existing = {p.name: p for p in ps.policies}

        for name, rego in rego_files.items():
            if name in existing:
                if existing[name].rego != rego:
                    existing[name].rego = rego
                    existing[name].updated_at = now_utc()
            else:
                db.add(
                    Policy(
                        policy_set_id=ps.id,
                        name=name,
                        rego=rego,
                    )
                )

        for name, policy in existing.items():
            if name not in rego_files:
                await db.delete(policy)

        ps.vcs_last_commit_sha = sha
        ps.vcs_last_synced_at = now_utc()
        ps.vcs_last_error = None

        logger.info(
            "Policy set synced from VCS",
            policy_set=ps.name,
            commit=sha[:8],
            policies_count=len(rego_files),
        )

    except Exception as e:
        ps.vcs_last_error = str(e)[:500]
        logger.warning(
            "Policy VCS sync failed",
            policy_set=ps.name,
            error=str(e),
        )


async def handle_policy_vcs_sync(payload: dict) -> None:
    """Triggered handler: sync a single VCS policy set by ID.

    Enqueued by the POST /policy-sets/{id}/actions/sync endpoint and by
    policy_vcs_poll_cycle (fan-out).
    """
    ps_id = uuid.UUID(payload["policy_set_id"])
    async with get_db_session() as db:
        ps = (
            await db.execute(
                select(PolicySet)
                .where(PolicySet.id == ps_id)
                .options(selectinload(PolicySet.policies), joinedload(PolicySet.vcs_connection))
            )
        ).scalar_one_or_none()
        if ps is None or ps.source != "vcs":
            return
        await _sync_policy_set(db, ps)
        await db.commit()


async def policy_vcs_poll_cycle() -> None:
    """Fan-out: enumerate VCS policy sets and enqueue one sync trigger per set.

    Each set syncs independently via handle_policy_vcs_sync — a slow repo
    cannot stall other sets.
    """
    async with get_db_session() as db:
        result = await db.execute(
            select(PolicySet.id).where(PolicySet.source == "vcs", PolicySet.enabled.is_(True))
        )
        ps_ids = result.scalars().all()

    for ps_id in ps_ids:
        await enqueue_trigger(
            "policy_vcs_sync",
            payload={"policy_set_id": str(ps_id)},
            dedup_key=f"policy_vcs_sync:{ps_id}",
            dedup_ttl=30,
        )
