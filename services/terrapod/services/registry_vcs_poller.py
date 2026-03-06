"""Registry VCS poller — auto-publish module versions from VCS tags.

Registered as a periodic task with the distributed scheduler. Each cycle:
1. Queries all modules with source='vcs' and a configured VCS connection
2. Lists tags from the VCS provider
3. For each new tag matching the module's tag pattern, downloads the archive
   and creates a new module version
"""

import fnmatch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import RegistryModule, RegistryModuleVersion
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service
from terrapod.storage import get_storage
from terrapod.storage.keys import module_tarball_key

logger = get_logger(__name__)


def _extract_version(tag_name: str, pattern: str) -> str:
    """Extract a semver string from a tag name based on the glob pattern.

    For pattern 'v*', tag 'v1.2.3' → '1.2.3'
    For pattern 'release-*', tag 'release-1.0.0' → '1.0.0'
    For pattern '*', tag '1.0.0' → '1.0.0'
    """
    # Find the prefix before the first wildcard
    star_pos = pattern.find("*")
    if star_pos < 0:
        # No wildcard — the tag name IS the version
        return tag_name

    prefix = pattern[:star_pos]
    if tag_name.startswith(prefix):
        return tag_name[len(prefix) :]

    return tag_name


def _dispatch_list_tags(provider: str):  # type: ignore[no-untyped-def]
    """Get the list_tags function for a VCS provider."""
    if provider == "github":
        return github_service.list_repo_tags
    elif provider == "gitlab":
        return gitlab_service.list_tags
    else:
        raise ValueError(f"Unsupported VCS provider: {provider}")


def _dispatch_download_archive(provider: str):  # type: ignore[no-untyped-def]
    """Get the download_archive function for a VCS provider."""
    if provider == "github":
        return github_service.download_repo_archive
    elif provider == "gitlab":
        return gitlab_service.download_archive
    else:
        raise ValueError(f"Unsupported VCS provider: {provider}")


def _dispatch_parse_repo_url(provider: str):  # type: ignore[no-untyped-def]
    """Get the parse_repo_url function for a VCS provider."""
    if provider == "github":
        return github_service.parse_repo_url
    elif provider == "gitlab":
        return gitlab_service.parse_repo_url
    else:
        raise ValueError(f"Unsupported VCS provider: {provider}")


async def registry_vcs_poll_cycle() -> None:
    """Poll VCS providers for new tags and auto-publish module versions."""
    async with get_db_session() as db:
        storage = get_storage()

        # Get all VCS-sourced modules with their connections
        result = await db.execute(
            select(RegistryModule)
            .where(
                RegistryModule.source == "vcs",
                RegistryModule.vcs_connection_id.isnot(None),
                RegistryModule.vcs_repo_url != "",
            )
            .options(selectinload(RegistryModule.versions))
        )
        modules = list(result.scalars().all())

        if not modules:
            return

        logger.info("Registry VCS poll starting", module_count=len(modules))

        for module in modules:
            try:
                await _poll_module(db, storage, module)
            except Exception:
                logger.warning(
                    "Registry VCS poll failed for module",
                    module_id=str(module.id),
                    module_name=module.name,
                    exc_info=True,
                )

        await db.commit()
        logger.info("Registry VCS poll complete")


async def _poll_module(db: AsyncSession, storage, module: RegistryModule) -> None:  # type: ignore[no-untyped-def]
    """Poll a single module's VCS repo for new tags."""
    from terrapod.db.models import VCSConnection

    # Load the VCS connection
    conn_result = await db.execute(
        select(VCSConnection).where(VCSConnection.id == module.vcs_connection_id)
    )
    conn = conn_result.scalars().first()
    if conn is None:
        logger.warning("VCS connection not found", module_id=str(module.id))
        return

    # Parse repo URL
    parse_fn = _dispatch_parse_repo_url(conn.provider)
    parsed = parse_fn(module.vcs_repo_url)
    if parsed is None:
        logger.warning(
            "Cannot parse repo URL",
            module_id=str(module.id),
            repo_url=module.vcs_repo_url,
        )
        return

    owner, repo = parsed

    # List tags from VCS
    list_tags_fn = _dispatch_list_tags(conn.provider)
    tags = await list_tags_fn(conn, owner, repo)

    # Build lookup of existing versions by version string
    existing_versions: dict[str, RegistryModuleVersion] = {v.version: v for v in module.versions}
    pattern = module.vcs_tag_pattern or "v*"

    changed_count = 0
    latest_tag = module.vcs_last_tag

    for tag in tags:
        tag_name = tag["name"]
        tag_sha = tag.get("sha", "")

        # Check if tag matches pattern
        if not fnmatch.fnmatch(tag_name, pattern):
            continue

        # Extract version from tag
        version_str = _extract_version(tag_name, pattern)
        if not version_str:
            continue

        existing = existing_versions.get(version_str)

        # Skip if version exists and SHA matches (no change)
        if existing and existing.vcs_commit_sha == tag_sha and tag_sha:
            continue

        # Download archive at this tag (new version or SHA mismatch)
        download_fn = _dispatch_download_archive(conn.provider)
        try:
            archive_bytes = await download_fn(conn, owner, repo, tag_name)
        except Exception:
            logger.warning(
                "Failed to download archive for tag",
                module_id=str(module.id),
                tag=tag_name,
                exc_info=True,
            )
            continue

        # Store tarball (overwrites existing if tag was moved)
        key = module_tarball_key(module.namespace, module.name, module.provider, version_str)
        await storage.put(key, archive_bytes, "application/gzip")

        if existing:
            # Tag moved to a different commit — update existing record
            existing.vcs_commit_sha = tag_sha
            existing.vcs_tag = tag_name
            logger.info(
                "Module version updated (tag moved)",
                module_name=module.name,
                provider=module.provider,
                version=version_str,
                tag=tag_name,
                sha=tag_sha[:12] if tag_sha else "",
            )
        else:
            # New version
            mod_version = RegistryModuleVersion(
                module_id=module.id,
                version=version_str,
                upload_status="uploaded",
                vcs_commit_sha=tag_sha,
                vcs_tag=tag_name,
            )
            db.add(mod_version)
            existing_versions[version_str] = mod_version
            logger.info(
                "Module version created from VCS tag",
                module_name=module.name,
                provider=module.provider,
                version=version_str,
                tag=tag_name,
                sha=tag_sha[:12] if tag_sha else "",
            )

        changed_count += 1
        latest_tag = tag_name

    if changed_count > 0:
        module.status = "setup_complete"
        if latest_tag:
            module.vcs_last_tag = latest_tag
        await db.flush()

        logger.info(
            "Module VCS poll complete",
            module_name=module.name,
            versions_changed=changed_count,
        )
