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
from terrapod.services import github_service, gitlab_service, vcs_rate_limit
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
# `warn` counts as well: a set may be advisory-only, and a file that produces
# warnings is a policy by any reading — the old filter dropped it silently.
_WARN_RULE_RE = re.compile(r"(?m)^\s*warn\s+(contains|:=|=)")

_POLICY_FILE_SUFFIXES = (".rego", ".yaml", ".yml", ".json")
# Per file. Data files are small by nature; the cap is here so one pathological
# file cannot be carried into every run's policy bundle.
_MAX_POLICY_FILE_BYTES = 1024 * 1024


def _parse_repo_url(conn: VCSConnection, repo_url: str) -> tuple[str, str] | None:
    return _provider_parse_repo_url(conn, repo_url)


async def _get_default_branch(conn: VCSConnection, owner: str, repo: str) -> str | None:
    return await _provider_get_default_branch(conn, owner, repo)


async def _get_branch_sha(conn: VCSConnection, owner: str, repo: str, branch: str) -> str | None:
    return await _provider_get_branch_sha(conn, owner, repo, branch)


def _extract_policy_files(
    archive_bytes: bytes, policy_path: str
) -> tuple[dict[str, str], list[str]]:
    """Extract a policy set's files from a tarball at the given path.

    Returns ({filename_with_extension: content}, [skipped descriptions]).

    The skipped list is returned rather than only logged because a skip can
    DELETE an enforced policy. The reconcile below removes any policy whose
    file is no longer extracted, and it cannot tell "the author deleted it"
    from "we declined to read it" — so a file that grows past the size cap, or
    a policy someone named `s3_bucket_test.rego`, silently lost its rule and
    the sync still reported clean. A mandatory set then passes on a policy
    that no longer exists. The caller surfaces these on `vcs_last_error`.

    The extension is kept — unlike the old rego-only extractor, which stripped
    it — because it is what distinguishes a policy from a data file, and what
    tells OPA how to load each one (#1842).

    `.rego`, `.yaml`, `.yml` and `.json` are taken; everything else is ignored,
    so a README or a CI config in the same directory costs nothing. Only direct
    children of policy_path are included (no recursive descent).

    `*_test.rego` is skipped. OPA test files define no `deny`, so they would
    land as support files and be loaded into a shared evaluation, where their
    fixtures become part of the data the real policies see.
    """
    files: dict[str, str] = {}
    skipped: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(_POLICY_FILE_SUFFIXES):
                continue
            if member.name.endswith("_test.rego"):
                skipped.append(f"{posixpath.basename(member.name)} (test fixture)")
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

            f = tar.extractfile(member)
            if f is None:
                continue
            raw = f.read()
            if len(raw) > _MAX_POLICY_FILE_BYTES:
                logger.warning(
                    "policy file skipped: too large",
                    file=remainder,
                    size=len(raw),
                    cap=_MAX_POLICY_FILE_BYTES,
                )
                skipped.append(f"{remainder} (over {_MAX_POLICY_FILE_BYTES // 1024} KiB)")
                continue
            try:
                files[remainder] = raw.decode("utf-8")
            except UnicodeDecodeError:
                # A binary file sharing the directory is not ours to carry.
                logger.warning("policy file skipped: not UTF-8", file=remainder)
                skipped.append(f"{remainder} (not UTF-8)")
    return files, skipped


def _classify(files: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split a set's files into policies and support files (#1842).

    A **policy** is a `.rego` file in `package terrapod` that produces a
    verdict — it defines `deny` or `warn`. Its key drops the extension, because
    that name is what the UI, the results and the API have always called a
    policy, and renaming them would be a gratuitous break.

    **Support files** are everything else the set carries: data (`.yaml`,
    `.yml`, `.json`) and `.rego` helpers that define no verdict. Their keys
    keep the extension, because OPA decides how to load a file by its suffix.

    The split is what lets a helper stop being invisible without becoming a
    policy: before this, a deny-less `.rego` was dropped at sync time, so a
    shared helper could not exist at all and every policy inlined its own copy
    of the same allowlist.
    """
    policies: dict[str, str] = {}
    support: dict[str, str] = {}
    for name, content in files.items():
        if name.endswith(".rego"):
            verdict = _DENY_RULE_RE.search(content) or _WARN_RULE_RE.search(content)
            if _PACKAGE_RE.search(content) and verdict:
                policies[os.path.splitext(name)[0]] = content
            else:
                support[name] = content
        else:
            support[name] = content
    return policies, support


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


async def _sync_policy_set(db: AsyncSession, ps: PolicySet, *, force: bool = False) -> None:
    """Sync a single VCS policy set.

    `force` re-reads the repository even when the branch head has not moved.
    The periodic poller never sets it -- an unchanged SHA means unchanged
    files, and re-downloading every archive every cycle would be waste.

    An explicit sync DOES set it, because #1842 gave the sync a second job.
    `support_files` is new, so every set that existed before the upgrade has
    `{}` and no commit to trigger a refill; the SHA check returned before the
    line that populates it. An operator who enabled `shared_evaluation` on
    such a set got an evaluation with no data files -- and a rule reading
    `data.approved_cidrs` is undefined rather than failing, so a mandatory set
    reported a clean pass. The UI's "no files found" warning blamed a wrong
    `policy-path`, and pushing an unrelated commit was the only real remedy.
    """
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

        if sha == ps.vcs_last_commit_sha and not force:
            return

        archive = await _download_archive(conn, owner, repo, sha)
        files, skipped_files = await asyncio.to_thread(
            _extract_policy_files, archive, ps.policy_path
        )
        rego_files, support_files = _classify(files)
        ps.support_files = support_files

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
        # A sync that declined to read a file is NOT a clean sync. Reporting
        # None here is what made a deleted-because-skipped policy invisible:
        # the set showed green while a rule it used to enforce was gone.
        ps.vcs_last_error = (
            (
                f"Synced, but {len(skipped_files)} file(s) were skipped and are not "
                f"part of this set: {', '.join(sorted(skipped_files))}"
            )[:500]
            if skipped_files
            else None
        )

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
    # The fan-out poller omits it; the explicit sync endpoint sets it.
    force = bool(payload.get("force", False))
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
        # Attribution has to live here, not only on the fan-out cycle: the cycle
        # enqueues a trigger and returns, so the provider calls happen in this
        # handler, on a different task with a fresh context. Labelling only the
        # cycle left every policy-set call recorded as `unknown` (#1339).
        with vcs_rate_limit.vcs_source("policy-sets", consumer=ps.name, kind="policy-set"):
            await _sync_policy_set(db, ps, force=force)
        await db.commit()


@vcs_rate_limit.attributed("policy-sets")
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
