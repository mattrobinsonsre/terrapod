"""Which Ansible execution environments a deployment can run, and which one a
workspace resolves to (#2154).

Two kinds, and the difference is who vouches for the image:

* **built-in** -- one tag per ansible-core line in `ansible.ee_repository`,
  selected by a workspace's `ansible_version`. Terrapod builds, smokes, scans
  and republishes these, so the set is discovered from the registry rather than
  hand-kept: the high end of the range floats as upstream publishes new lines,
  and a hand-kept list would mean a Terrapod release per ansible release.
* **operator-supplied** -- a label in `ansible.execution_environments` mapping
  to an image URI, selected by a workspace's `ansible_execution_environment`,
  which overrides `ansible_version` when set. Trusted code, used as given.

**"I cannot tell" is a first-class answer here and must not collapse into
"empty".** The built-in set comes from a registry over the network, and the
write-time check refuses a workspace naming a line that is not in it -- so an
unreachable registry would otherwise refuse ordinary workspace edits. That is
the pull-through's own long-standing position, stated in
`_revalidate_tag`'s docstring: a cache that stops answering when it cannot
reach the internet has failed at the one thing it exists for.

There is deliberately **no EOL flag**. Marking a line end-of-life needs a
sourced support-cycle date, and ansible-core publishes that as prose rather than
as anything machine-readable -- so the only way to ship the flag today would be
a constant encoding what someone believed at the time, which is the kind of
"fact" this project does not put in code. The warning #2154 D6 asks for needs a
real source first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import DEFAULT_ANSIBLE_LINE
from terrapod.services.oci import pullthrough_service, registry_service

logger = structlog.get_logger("ansible.ee")

#: A built-in EE tag names a LINE, not a patch: `2.21`, never `2.21.5`. The
#: immutable `X.Y.Z` tags exist alongside it for auditing a run after the fact,
#: but a workspace selects a line and the line's tag moves as that line is
#: rebuilt for upstream patches.
EE_LINE = re.compile(r"^\d+\.\d+$")

KIND_BUILTIN = "built-in"
KIND_OPERATOR = "operator"

_CACHE_KEY = "tp:ansible_ee_lines"


class UnknownExecutionEnvironment(ValueError):
    """A workspace named an operator EE label that is not configured.

    Its own exception type so a router can answer 422 naming the field, rather
    than every caller re-deriving which of several `ValueError`s this was.
    """


class UnpublishedAnsibleLine(ValueError):
    """A workspace named an ansible-core line with no published EE image.

    Raised **only when the available set is known** and the line is absent from
    it. When the set cannot be determined the write is accepted -- see the
    module docstring.
    """


@dataclass(frozen=True)
class Availability:
    """What this deployment can run.

    `lines is None` means the built-in set could not be determined, which is
    different from `lines == []` (the registry answered, and holds nothing).
    """

    lines: list[str] | None
    environments: dict[str, str]

    @property
    def listing_is_authoritative(self) -> bool:
        return self.lines is not None


def _own_registry_host() -> str:
    """The host clients address this deployment's own registry at, or "".

    Needed because `ee_repository` serves two purposes at once and they differ
    for a mirrored setup: it is the reference a **kubelet** pulls, which needs
    the ingress host, and it is the repository whose tags are listed, which in
    Terrapod's own registry is named *without* that host (a mirror row is the
    client-facing path minus the host -- see `ensure_mirror_repository`).

    So an operator following the documented restricted-network advice --
    `terrapod.example.com/ghcr.io/.../terrapod-ee` -- would otherwise have the
    listing look up a repository name that does not exist and then try an
    authenticated HTTP call against Terrapod itself, degrading to "unknown" for
    exactly the deployments the advice is aimed at.
    """
    from urllib.parse import urlparse

    raw = (settings.external_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    return parsed.netloc or ""


def _local_name(reference: str) -> str:
    """`reference` as this registry would name it, with our own host removed."""
    host = _own_registry_host()
    if host and reference.startswith(f"{host}/"):
        return reference[len(host) + 1 :]
    return reference


def _split_repository(reference: str) -> tuple[str, str] | None:
    """Split `host/path/to/repo` into (host, repository).

    Returns None for a single-component reference, which cannot name a registry
    and so cannot be listed.
    """
    host, _, remainder = reference.strip().partition("/")
    if not host or not remainder:
        return None
    return host, remainder


async def _builtin_lines(db: AsyncSession) -> list[str] | None:
    """Tags in the configured EE repository that name an ansible-core line.

    Three sources, because the configured repository can be one of three things
    and an operator should not have to care which:

    1. **A repository in Terrapod's own registry** (someone mirrored or pushed
       the EEs into it). Local rows are the whole truth and no network call
       happens.
    2. **A configured pull-through upstream.** Local rows union what upstream
       holds, so an EE already cached stays selectable even if upstream is
       unreachable.
    3. **Any other registry** -- the default, since `ghcr.io` is not a
       pull-through upstream unless an operator adds it. Listed directly,
       because the host came from this deployment's own configuration rather
       than from a request. See `list_tags_at` for why that distinction matters.
    """
    reference = settings.ansible.ee_repository.strip()
    if not reference:
        return []

    # Our own host is stripped once, here, and EVERYTHING below uses the result.
    # Doing it only for the local lookup is not enough: the fall-through would
    # still resolve the full reference, find `terrapod.example.com` is not a
    # configured upstream, and make an authenticated call against Terrapod
    # itself -- so the mirrored case would degrade to "unknown" anyway, just one
    # step later. Stripped, the remainder is both the local row's name and a
    # resolvable upstream coordinate, so one substitution fixes both halves.
    reference = _local_name(reference)

    local: list[str] = []
    repository = await registry_service.get_repository(db, reference)
    if repository is not None:
        local = await registry_service.list_tags(db, repository)
        if repository.upstream is None:
            # Pushed locally: there is no upstream to ask, and these rows are
            # the complete answer rather than a cached subset of one.
            return _lines(local)

    if settings.registry.cache_only:
        # Sealed. What is held locally is exactly what can run, so this is an
        # authoritative answer rather than a degraded one.
        return _lines(local)

    split = _split_repository(reference)
    if split is None:
        logger.warning(
            "ansible.ee_repository names no registry host; cannot list lines",
            reference=reference,
        )
        return None
    host, upstream_repo = split

    if pullthrough_service.resolve_upstream(reference) is not None:
        remote = await pullthrough_service.upstream_tags(host, upstream_repo)
    else:
        remote = await pullthrough_service.list_tags_at(
            f"https://{host}", upstream_repo, label=host
        )

    if remote is None:
        # Unknown upstream-side. Local rows alone would look authoritative while
        # omitting every line not yet pulled, so the honest answer is "cannot
        # tell" -- unless we hold nothing at all, where there is nothing to be
        # wrong about either way.
        return None
    return _lines(local + remote)


def _lines(tags: list[str]) -> list[str]:
    """Line-shaped tags only, de-duplicated, newest first.

    `X.Y.Z` tags are filtered out rather than collapsed to their line: an
    immutable patch tag existing does not mean the moving line tag does, and
    offering a line that cannot be pulled is the failure this read exists to
    prevent.
    """
    seen = {t for t in tags if EE_LINE.match(t)}
    return sorted(seen, key=lambda t: tuple(int(p) for p in t.split(".")), reverse=True)


async def availability(db: AsyncSession) -> Availability:
    """The selectable environments, cached for `availability_ttl_seconds`.

    The cache holds the built-in listing only. Operator-supplied environments
    come from configuration already in memory, so caching them would add a way
    for the two to disagree and save nothing.
    """
    environments = dict(settings.ansible.execution_environments)
    ttl = settings.ansible.availability_ttl_seconds

    if ttl <= 0:
        return Availability(lines=await _builtin_lines(db), environments=environments)

    cached = await _read_cache()
    if cached is not None:
        return Availability(lines=cached, environments=environments)

    lines = await _builtin_lines(db)
    if lines is not None:
        # Only a known answer is cached. Caching "cannot tell" would hold the
        # permissive state open for the whole TTL after the registry came back.
        await _write_cache(lines, ttl)
    return Availability(lines=lines, environments=environments)


async def _read_cache() -> list[str] | None:
    try:
        from terrapod.redis.client import get_redis_client

        raw = await get_redis_client().get(_CACHE_KEY)
    except Exception:  # noqa: BLE001 - a cache read must never fail the request
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


async def _write_cache(lines: list[str], ttl: int) -> None:
    try:
        from terrapod.redis.client import get_redis_client

        await get_redis_client().setex(_CACHE_KEY, ttl, json.dumps(lines))
    except Exception:  # noqa: BLE001 - as above
        logger.debug("Could not cache the execution-environment listing")


def effective_ee_image(*, ansible_version: str | None, ee_label: str | None) -> str:
    """The image reference a workspace's configure operation runs in.

    **The one place the empty-string fallback is written.** `ansible_version`
    carries the #2010 convention -- `""` is a value meaning "the deployment's
    default", not an absence -- and the last time that fallback had more than one
    home, a deployment-wide read got back in unwritten. A gate asserts this is
    the only resolver; see `test_ee_image_resolution.py`.

    An operator label wins outright when set. That is why it is a separate
    column rather than an overloaded `ansible_version`: `is_pypi_version_allowed`
    screens only pre-releases, so a label would pass it, and a label named
    `2.21` would shadow a real line invisibly.
    """
    if ee_label:
        uri = settings.ansible.execution_environments.get(ee_label)
        if not uri:
            raise UnknownExecutionEnvironment(
                f"ansible-execution-environment '{ee_label}' is not configured on this deployment"
            )
        return uri

    line = (ansible_version or "").strip() or DEFAULT_ANSIBLE_LINE
    return f"{settings.ansible.ee_repository.strip()}:{line}"


async def validate_selection(
    db: AsyncSession, *, ansible_version: str | None, ee_label: str | None
) -> None:
    """Refuse a selection this deployment cannot run, at the write boundary.

    An unknown operator label is always refused -- the map is local
    configuration, so "cannot tell" does not arise.

    An ansible-core line is refused **only when the available set is known**.
    Refusing on an unknown set would make every workspace edit depend on
    registry reachability, turning an upstream hiccup into a refused write; the
    run-time failure path already names the image when an EE cannot be pulled,
    so the earlier error is a convenience rather than the only guard.
    """
    if ee_label:
        # Raises UnknownExecutionEnvironment, and deliberately short-circuits:
        # with a label set, `ansible_version` does not select anything.
        effective_ee_image(ansible_version=ansible_version, ee_label=ee_label)
        return

    line = (ansible_version or "").strip()
    if not line:
        # Inheriting. The resolved default is built in, so there is nothing a
        # caller could have got wrong.
        return

    try:
        available = await availability(db)
    except Exception as exc:  # noqa: BLE001
        # ANY failure to determine the set is "cannot tell", not just an
        # unreachable registry: a database error, a Redis error, a malformed
        # upstream response. The alternative is refusing a legitimate workspace
        # edit because of an infrastructure problem somewhere else entirely,
        # which is the failure mode D7's fail-open rule exists to prevent -- and
        # a broad catch is right here precisely because the consequence of
        # getting it wrong is asymmetric: accepting an unrunnable line costs a
        # legible run-time failure, refusing a runnable one costs the edit.
        logger.info(
            "Accepting an ansible line without confirming it: the set could not be read",
            line=line,
            error=str(exc),
        )
        return
    if available.lines is None:
        logger.info(
            "Accepting an ansible line without confirming it: the available set is unknown",
            line=line,
        )
        return
    if line not in available.lines:
        raise UnpublishedAnsibleLine(
            f"ansible-version '{line}' has no published execution environment. "
            f"Available: {', '.join(available.lines) or 'none'}"
        )
