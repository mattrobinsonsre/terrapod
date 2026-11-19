"""The shared pull-through path: fetch, store, serve, record (#1417).

This is the half every language registry has in common. The ecosystem modules
own their index format; none of them own any of this.

Three constraints shape the code more than the protocol does:

* **Artifacts are large.** A CUDA-flavoured wheel is comfortably several hundred
  megabytes. Everything streams through a file on the ephemeral PVC and is never
  held in the API pod's memory (rule 14), and `/tmp` is not that PVC — it is
  RAM-backed, which is how a previous release OOM-killed both API pods.
* **The event loop is shared.** Hashing and file I/O go through
  `asyncio.to_thread` (rule 13); a 500 MB digest computed inline stalls every
  other request on the replica.
* **Sealing must fail honestly.** In `cache_only` mode a miss cannot be served,
  and the error says which package and what to do about it — a generic 502 sends
  an operator to look at their network.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import CachedPackageFile
from terrapod.http_retry import DEFAULT_RETRIES, retry_delay_seconds
from terrapod.logging_config import get_logger
from terrapod.services.package_cache import content_allowlist
from terrapod.storage import keys
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: Read size for streaming to and from disk. Large enough that a 500 MB artifact
#: is not a million round trips, small enough not to matter to a small one.
_CHUNK = 1024 * 1024

#: Upstream is a public CDN; a slow one is a slow build, not a broken one. The
#: connect timeout is what should be short.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


class SealedError(RuntimeError):
    """A miss that cannot be filled because the node is sealed."""


class UpstreamError(RuntimeError):
    """Upstream refused or failed. Distinct from "no such package"."""


class NotFoundUpstream(RuntimeError):
    """Upstream says this artifact does not exist."""


class UploadedArtifactUnavailable(RuntimeError):
    """An operator-uploaded artifact's object is gone, or is not what was recorded.

    Raised rather than reported as a cache miss, and that distinction is the whole
    of it (#2599). A miss has two outcomes and both are wrong here:

    * unsealed, a miss is filled from upstream -- which would silently replace a
      vetted artifact with one nobody vetted, under the coordinates the operator
      curated. That is the posture defeated by its own cache;
    * sealed, a miss deletes the row (the object is gone, so the row promises
      nothing) -- which discards the operator's own record of what should be
      there, leaving nothing to tell them what to re-upload.

    So the row is left exactly as it is and the request fails saying what is
    wrong. This is also what a failover onto a follower whose blob store was not
    replicated looks like: the ledger is complete and the bytes are not, and an
    honest 503 naming the artifact is the only useful thing to say.
    """


class DigestMismatch(RuntimeError):
    """The bytes upstream served are not the bytes upstream published a digest for.

    Fail-closed and never cached: a mismatch is either a corrupted transfer or a
    substituted artifact, and there is no way to tell which from here. The one
    thing that must not happen is serving it to a runner and finding out later.
    """


#: Resolves upstream's published digest on a miss. Takes the client already open
#: for the download so an ecosystem's extra request shares its connection pool and
#: its retry policy rather than opening a second one.
DigestResolver = Callable[[httpx.AsyncClient], Awaitable[str]]

#: `(client, digest_hex) -> Provenance | None`. Returns what was verified, or
#: None when the publisher published nothing. Raises to refuse.
ProvenanceResolver = Callable[[httpx.AsyncClient, str], Awaitable[object]]


@dataclass(frozen=True)
class Artifact:
    """What an ecosystem hands the substrate to fetch and cache.

    `digest` is upstream's own, in the form the ecosystem publishes it
    (``<algo>:<hex>`` or SRI ``<algo>-<base64>``), and it is **verified** here
    before the artifact is stored — see `_parse_digest` and `_DIGEST_REQUIRED`.

    It used to be recorded and not checked, on the grounds that the client checks
    it. That was only ever half true: `pip install` verifies nothing without
    `--require-hashes` or a hash-pinned lock, so for the commonest case in the
    commonest ecosystem there was no check anywhere. Validating what we cache is
    the job (#2589).
    """

    ecosystem: str
    name: str
    version: str
    filename: str
    upstream_url: str
    digest: str = ""
    content_type: str = "application/octet-stream"
    #: Fetch upstream's digest, for an artifact whose publisher puts it somewhere
    #: other than the index the download URL came from -- NuGet's `catalogEntry`,
    #: and the `SHA256SUMS` manifest on the release carrying Terrapod's own
    #: bridged Pulumi plugin.
    #:
    #: Per ARTIFACT rather than per ecosystem, which is what lets a requirement
    #: apply to one plugin out of an ecosystem that publishes nothing for the
    #: rest (`pulumi_plugins.is_first_party`). `_DIGEST_REQUIRED` cannot express
    #: that, because it matches on `Artifact.ecosystem`.
    #:
    #: Called ONLY from `fetch_and_cache`, which is the miss path, and only when
    #: `digest` is empty. That placement is the whole of the design: an Artifact is
    #: constructed on EVERY request including a cache hit, so resolving a digest
    #: during construction would put an upstream round-trip on the hit path and
    #: break `cache_only` outright -- a sealed node serves hits with no egress at
    #: all. On the miss path there is no such tension, because a miss already
    #: requires egress and `fetch_and_cache` refuses before anything else when
    #: sealed.
    digest_resolver: DigestResolver | None = None
    #: Verify the publisher's own signature or attestation (#2589 Gap A).
    #:
    #: Called ONLY from `fetch_and_cache`, on the miss path, and only AFTER the
    #: digest has been confirmed -- so it is handed the digest it must find in
    #: the attestation's subject, and so `ATTEST_UPSTREAM_PUBLISHER` can never
    #: mean "we checked the signature instead of the bytes". Same placement
    #: reasoning as `digest_resolver`: an Artifact is constructed on every
    #: request including a hit, so a check during construction would put an
    #: upstream round trip on the hit path and break `cache_only` outright.
    #:
    #: Returning None means the publisher published nothing for this artifact,
    #: which is the ordinary case for most of PyPI and is recorded as such.
    #: Raising `ProvenanceError` is fatal and is not configurable: what an
    #: operator may choose is whether an ABSENT attestation is tolerated, never
    #: whether a BAD one is.
    provenance_resolver: ProvenanceResolver | None = None


class CoordinateError(ValueError):
    """The coordinates an upload supplied are not ones this ecosystem addresses.

    Checked against the ecosystem's OWN naming rules and **never against an
    upstream index** (#2599): an operator must be able to upload a patched
    provider, an internal collection or a rebuilt binary that upstream has never
    published, and validating against upstream would fail the feature for its
    most important case. What is checked is what has to be: these values become a
    cache key and a storage path, so a separator or a `..` in one is refused.
    """


@dataclass(frozen=True)
class Coordinates:
    """Where an uploaded artifact is served from, in the cache's own terms.

    The upload-side peer of :class:`Artifact`, and deliberately the same
    `(name, version, filename)` triple each ecosystem's own `artifact_for`
    produces on the fetch path: an upload is that cache key arrived at from the
    other direction. It carries no URL, because there is no upstream to reach.

    Built by `upload_coordinates` in each ecosystem module rather than by one
    validator switching on a kind — every ecosystem already defines its own
    naming, normalisation and filename convention here, and a second copy of
    those rules somewhere central is how the two come to disagree.
    """

    name: str
    version: str
    filename: str
    content_type: str = "application/octet-stream"


def sealed() -> bool:
    """Whether this node is forbidden from reaching upstream."""
    return bool(settings.registry.cache_only)


def _resolve_tmpdir() -> str | None:
    """The ephemeral PVC, or None for the system default in dev and tests."""
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


#: Digest algorithms we will check, mapped to their hashlib names. An algorithm
#: an ecosystem publishes that is NOT here is refused rather than skipped: a
#: digest we cannot compute is not a weaker check, it is no check, and treating
#: it as the latter is how an unverified artifact gets cached quietly.
_ALGOS = {"sha256": "sha256", "sha512": "sha512", "sha1": "sha1"}

#: Ecosystems whose upstream publishes a per-file digest, so an artifact arriving
#: WITHOUT one is refused rather than cached unverified.
#:
#: Absence from this set is a declaration, not an oversight. Each absent
#: ecosystem has had its upstream measured and the reason is recorded in its own
#: module:
#:
#:   goproxy          the module proxy serves no digest at all (`.ziphash` and
#:                    `.zip.sha256` are both 404), so there is nothing per-file
#:                    to require here and the ecosystem cannot join this set.
#:                    The authoritative hash lives in the Go checksum database,
#:                    which this deployment MIRRORS under `/sumdb/<host>/`
#:                    (#2589) -- so the verification happens, it is simply the
#:                    toolchain checking the transparency log through us rather
#:                    than us comparing a digest at fetch time.
#:
#:                    **This used to say the database was a third upstream a
#:                    sealed deployment could not reach, and that the consumer
#:                    verified with a stronger control anyway because the `go`
#:                    command "enforces `go.sum` against every module it
#:                    downloads". The first half is no longer true now that it is
#:                    mirrored; the second was only ever true of a module the
#:                    program had already PINNED.** Measured, go1.27.3, cold
#:                    module cache: `go mod download` with `GOSUMDB=off` and no
#:                    `go.sum` entry exits 0 and WRITES the hash of whatever the
#:                    proxy served. The correction is left visible because a
#:                    reason that reads as considered is what stops the next
#:                    person looking.
#:   pulumi_plugins   get.pulumi.com publishes nothing beside the tarball (403
#:                    for `.sha256` and `SHA256SUMS`), so a Pulumi-published
#:                    plugin is admitted and recorded honestly. **Terrapod's own
#:                    plugin is the exception and IS required**: we are its
#:                    publisher, its release carries a `SHA256SUMS` manifest, and
#:                    `pulumi_plugins.resolve_first_party_digest` refuses the
#:                    fetch when that manifest is absent or does not list the
#:                    asset. That requirement is enforced in the module rather
#:                    than by joining this set, because this set matches on
#:                    `Artifact.ecosystem` -- `"pulumi"` here would demand a
#:                    digest for `random` and `aws-native` too and break every
#:                    Pulumi-published plugin.
#:
#: So absence from this set is per-ECOSYSTEM and says nothing about whether some
#: artifact within it is verified: a required digest that applies to one artifact
#: rather than to the whole ecosystem belongs in that ecosystem's module, where
#: the predicate identifying it already lives.
#:
#: Where an ecosystem's digest does live at a SEPARATE upstream that is not an
#: obstacle: `digest_resolver` fetches it on the MISS path, where egress is
#: already required, so there is no case of reaching the artifact and not its
#: digest -- a sealed node fetches neither. NuGet is wired exactly that way, and
#: so is the first-party Pulumi plugin.
#:
#: Adding an ecosystem here without wiring its digest breaks that ecosystem
#: loudly, which is the intended direction.
_DIGEST_REQUIRED = frozenset({"pypi", "npm", "galaxy", "nuget"})

#: What `CachedPackageFile.attestation` may say about how a row was admitted. The
#: vocabulary the artifact ledger uses (#2589), so it is additive from here.
#:
#: NULL is not in this set and is the load-bearing value: it means a build that
#: recorded nothing admitted the row, which is to say before verification existed.
#: `lookup_present` treats that as a miss.
ATTEST_UPSTREAM_DIGEST = "upstream-digest"
#: The ecosystem's upstream publishes no per-file digest anywhere we can reach --
#: goproxy and pulumi_plugins today, each with its reason recorded above. Admitted
#: deliberately and recorded honestly rather than left looking verified.
ATTEST_NONE_PUBLISHED = "none-published"
#: An operator uploaded these bytes having vetted them with their own process
#: (#2599). There is no upstream digest to check against and asking for one would
#: defeat the posture -- the likeliest upload is something they built, patched or
#: re-packaged, which no publisher key covers. What stands in for it is
#: provenance: `uploaded_by`, `cached_at` and `upload_reference` name who
#: asserted what, and `digest` is the digest computed on receipt.
#:
#: It is a distinct value rather than reusing either of the others because an
#: uploaded artifact must never read as fetched-and-verified in the record: that
#: would make the ledger lie about the one thing on it that matters.
ATTEST_OPERATOR_UPLOAD = "operator-upload"
#: The publisher's own signature or attestation verified, not merely the digest
#: the index published (#2589 Gap A). A STRONGER claim than
#: `ATTEST_UPSTREAM_DIGEST` and the reason it is a separate value: a digest
#: verifies an artifact against the index that named it, and whoever controls the
#: index controls both halves. This says the publisher -- PyPI's PEP 740
#: attestation, or npm's registry signature -- vouched for these bytes.
#:
#: It implies the digest check too: provenance is only attempted after the digest
#: has already been confirmed, so this value never means "we checked the
#: signature instead".
ATTEST_UPSTREAM_PUBLISHER = "upstream-publisher"
ATTESTATIONS = frozenset(
    {
        ATTEST_UPSTREAM_DIGEST,
        ATTEST_NONE_PUBLISHED,
        ATTEST_OPERATOR_UPLOAD,
        ATTEST_UPSTREAM_PUBLISHER,
    }
)


def _attestation_for(expected: tuple[str, str] | None, provenance: object | None) -> str:
    """Which attestation the row records, from what was actually established.

    Ordered by strength rather than by convenience, and the ordering is the
    claim: `upstream-publisher` always implies the bytes were checked, so this
    cannot record it for an artifact nobody verified.

    That holds by two different routes, which is worth stating because the
    second is the less obvious one. Where the index published a digest, the
    digest check has already passed before provenance is attempted at all.
    Where it published none, the provenance check itself compares the digest
    computed from the downloaded bytes against the attestation's own subject --
    so the bytes are verified against the PUBLISHER's statement rather than
    against the index, which is the stronger of the two.
    """
    if provenance is not None:
        return ATTEST_UPSTREAM_PUBLISHER
    return ATTEST_UPSTREAM_DIGEST if expected is not None else ATTEST_NONE_PUBLISHED


def _parse_digest(declared: str) -> tuple[str, str] | None:
    """`(hashlib algorithm, lowercase hex)` from upstream's published form.

    Two forms, because the ecosystems genuinely differ and normalising them in
    one place beats four partial parsers:

    * ``<algo>:<hex>`` — PyPI and Galaxy, and npm's legacy `shasum`.
    * ``<algo>-<base64>`` — Subresource Integrity, which is what npm's
      `integrity` is. Note the separator is a DASH, so a parser that splits on
      ``:`` alone silently sees no digest at all for every npm package.

    Returns None when there is nothing to parse. Raises when there is something
    and we cannot read it — an unparseable digest is a reason to stop, not to
    proceed unverified.
    """
    declared = (declared or "").strip()
    if not declared:
        return None

    if ":" in declared:
        algo, _, value = declared.partition(":")
        try:
            raw = bytes.fromhex(value.strip())
        except ValueError as exc:
            raise DigestMismatch(f"digest {declared!r} is not hex") from exc
    elif "-" in declared:
        algo, _, value = declared.partition("-")
        try:
            raw = base64.b64decode(value.strip(), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise DigestMismatch(f"digest {declared!r} is not base64") from exc
    else:
        raise DigestMismatch(f"digest {declared!r} is in no form we recognise")

    name = _ALGOS.get(algo.strip().lower())
    if name is None:
        raise DigestMismatch(
            f"digest {declared!r} uses algorithm {algo!r}, which we do not compute"
        )
    if len(raw) != hashlib.new(name).digest_size:
        raise DigestMismatch(
            f"digest {declared!r} is {len(raw)} bytes, not the "
            f"{hashlib.new(name).digest_size} a {name} digest is"
        )
    return name, raw.hex()


def _digest_file(path: str, algorithm: str = "sha256") -> tuple[int, str]:
    """Size and digest of a file on disk. Runs in a thread — never inline."""
    hasher = hashlib.new(algorithm)
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


async def _file_chunks(path: str) -> AsyncIterator[bytes]:
    """Stream a file from disk without reading it into memory."""
    handle = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, _CHUNK)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


async def lookup(
    db: AsyncSession, ecosystem: str, name: str, filename: str
) -> CachedPackageFile | None:
    """The cached row for an artifact, or None."""
    result = await db.execute(
        select(CachedPackageFile).where(
            CachedPackageFile.ecosystem == ecosystem,
            CachedPackageFile.name == name,
            CachedPackageFile.filename == filename,
        )
    )
    return result.scalar_one_or_none()


async def touch(db: AsyncSession, record: CachedPackageFile) -> None:
    """Mark an artifact as used, so retention does not evict a hot package.

    Retention compares `last_accessed_at`, so anything cached that is not touched
    on read looks permanently untouched and gets reaped however heavily it is
    used — which then re-fetches it, in a loop, forever.
    """
    record.last_accessed_at = datetime.now(UTC)
    await db.flush()


async def _fetch_to_file_with_retry(
    client: httpx.AsyncClient, artifact: Artifact, path: str
) -> None:
    """Stream an upstream artifact to disk, restarting after transient failures.

    The temporary file is opened with ``wb`` for each successful response, so a
    read timeout after partial progress cannot append a second copy of the file.
    Nothing is written to object storage until one complete upstream response
    has arrived.
    """
    url = artifact.upstream_url
    for attempt in range(DEFAULT_RETRIES + 1):
        retry_status: int | None = None
        try:
            async with client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise NotFoundUpstream(url)
                if response.status_code >= 500:
                    retry_status = response.status_code
                elif response.status_code >= 400:
                    raise UpstreamError(f"upstream returned {response.status_code} for {url}")
                else:
                    handle = await asyncio.to_thread(open, path, "wb")
                    try:
                        async for chunk in response.aiter_bytes(_CHUNK):
                            await asyncio.to_thread(handle.write, chunk)
                    finally:
                        await asyncio.to_thread(handle.close)
        except httpx.RequestError as exc:
            if attempt == DEFAULT_RETRIES:
                raise UpstreamError(f"could not reach {url}: {exc}") from exc
            logger.warning(
                "Retrying package cache upstream stream after transport error",
                ecosystem=artifact.ecosystem,
                url=url,
                attempt=attempt + 1,
                retries=DEFAULT_RETRIES,
                error=str(exc),
            )
            await asyncio.sleep(retry_delay_seconds(attempt))
            continue

        if retry_status is None:
            return
        if attempt == DEFAULT_RETRIES:
            raise UpstreamError(f"upstream returned {retry_status} for {url}")
        logger.warning(
            "Retrying package cache upstream stream after 5xx",
            ecosystem=artifact.ecosystem,
            url=url,
            attempt=attempt + 1,
            retries=DEFAULT_RETRIES,
            status=retry_status,
        )
        await asyncio.sleep(retry_delay_seconds(attempt))


async def fetch_and_cache(
    db: AsyncSession,
    storage: ObjectStore,
    artifact: Artifact,
    *,
    client: httpx.AsyncClient | None = None,
) -> CachedPackageFile:
    """Fetch an artifact from upstream, store it, and record it.

    Safe to run concurrently for the same artifact. Two replicas racing a cold
    package both fetch — wasteful once, and cheaper than the coordination needed
    to prevent it — but the bytes are identical and the losing writer's unique
    violation resolves to the winner's row rather than a 500 in someone's
    `pip install`.
    """
    if sealed():
        raise SealedError(
            f"{artifact.ecosystem} package {artifact.name} {artifact.filename} is not "
            f"cached, and this node is sealed (registry.cache_only). Warm it before "
            f"sealing, or unset registry.cache_only."
        )

    # BEFORE the fetch, not after it (#2589). The control is about what this
    # deployment is willing to serve, and an artifact that has already been
    # downloaded has already been reached for -- the refusal has to happen while
    # it still means something. It sits here rather than in each ecosystem module
    # because this is the one pull-through point all six share, so a cache added
    # later is covered without anybody remembering to add it.
    #
    # Deliberately AFTER the sealed check: a sealed node fetches nothing at all,
    # so its own error is the more useful one and this would only mask it.
    content_allowlist.check(artifact.ecosystem, artifact.name, artifact.filename)

    key = keys.package_cache_key(artifact.ecosystem, artifact.name, artifact.filename)
    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT)

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="tp-pkg-", dir=_resolve_tmpdir())
    os.close(tmp_fd)
    try:
        try:
            await _fetch_to_file_with_retry(client, artifact, tmp_path)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not reach {artifact.upstream_url}: {exc}") from exc

        # Verify BEFORE storing. An artifact that fails must never reach the
        # object store, because `lookup` gates serving on the row and the row is
        # written from here -- but a half-stored object is still a thing a future
        # bug or a manual read could serve, and the cost of not storing it is one
        # re-fetch.
        declared = artifact.digest
        provenance = None
        if not declared and artifact.digest_resolver is not None:
            # One extra request, on a miss only. If the digest source is
            # unreachable while the artifact source is up we fail closed below --
            # a transient, and `arequest_with_retry` has already had its go at it.
            declared = await artifact.digest_resolver(client)

        expected = _parse_digest(declared)
        if expected is None and artifact.ecosystem in _DIGEST_REQUIRED:
            raise DigestMismatch(
                f"{artifact.ecosystem} published no digest for {artifact.name} "
                f"{artifact.filename}, and this ecosystem's upstream always does. "
                "Refusing to cache it: an absent digest here means the index was "
                "not the one we expect, not that the artifact is fine."
            )

        algorithm = expected[0] if expected else "sha256"
        size, computed = await asyncio.to_thread(_digest_file, tmp_path, algorithm)
        if expected is not None and computed != expected[1]:
            raise DigestMismatch(
                f"{artifact.ecosystem} {artifact.name} {artifact.filename}: upstream "
                f"published {algorithm}:{expected[1]} and served bytes whose "
                f"{algorithm} is {computed}. Not cached."
            )

        # Publisher provenance, after the digest and before storing (#2589).
        #
        # After, because the attestation names a digest and the check is that it
        # names THIS one -- so `ATTEST_UPSTREAM_PUBLISHER` can never mean the
        # signature was checked instead of the bytes. Before storing, for the
        # same reason the digest check is: an artifact that fails must not reach
        # the object store at all.
        if artifact.provenance_resolver is not None:
            from terrapod.services.package_cache import publisher_provenance

            try:
                provenance = await artifact.provenance_resolver(client, computed)
            except publisher_provenance.ProvenanceUnavailable as exc:
                # Says nothing about the artifact -- an unreachable trust root or
                # a 5xx is our egress, not their signature. Degrade to the digest
                # check, which has already passed, and record that rather than
                # refusing an artifact on the strength of our own network.
                logger.warning(
                    "Could not check publisher provenance; keeping the digest result",
                    ecosystem=artifact.ecosystem,
                    name=artifact.name,
                    filename=artifact.filename,
                    reason=str(exc),
                )

        await storage.put_stream(key, _file_chunks(tmp_path), content_type=artifact.content_type)
    finally:
        # The PVC is shared and finite; a failed fetch must not leave its partial
        # download behind to be discovered as a disk-full incident later.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if owns_client:
            await client.aclose()

    record = CachedPackageFile(
        ecosystem=artifact.ecosystem,
        name=artifact.name,
        version=artifact.version,
        filename=artifact.filename,
        storage_key=key,
        size=size,
        # What was VERIFIED, which for a resolved digest is not what the
        # Artifact was constructed with.
        digest=declared,
        attestation=_attestation_for(expected, provenance),
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        # Either we lost a race, or the row is a legacy one carrying no attestation
        # -- which is the COMMON case here, not an edge: `lookup_present` sends
        # every unattested row down the miss path, and that row is still present, so
        # the insert above is always a duplicate for one.
        await db.rollback()
        existing = await lookup(db, artifact.ecosystem, artifact.name, artifact.filename)
        if existing is None:  # pragma: no cover — the constraint says otherwise
            raise
        if existing.attestation is None:
            # Re-verified just now, so rewrite the row with what was actually
            # checked. Returning it unchanged would re-download the artifact on
            # every single request and still serve a row nobody can vouch for --
            # which is the bug this branch existed to avoid, arriving the other way
            # round.
            existing.digest = declared
            existing.attestation = (
                ATTEST_UPSTREAM_DIGEST if expected is not None else ATTEST_NONE_PUBLISHED
            )
            existing.size = size
            await db.flush()
            # A genuine admission: this row had nobody vouching for it and now
            # does. The race-lost case below needs no ledger write -- the
            # request that won recorded it.
            await _record_in_ledger(db, existing, provenance)
        # Otherwise we lost a race: the winner stored identical bytes at the
        # identical key, so its row is as good as ours.
        return existing

    logger.info(
        "Cached package artifact",
        ecosystem=artifact.ecosystem,
        name=artifact.name,
        filename=artifact.filename,
        size=size,
    )
    await _record_in_ledger(db, record, provenance)
    return record


async def _record_in_ledger(
    db: AsyncSession, record: CachedPackageFile, provenance: object | None
) -> None:
    """Record an admission in `artifact_ledger` (#2589).

    Called from the two places an artifact is genuinely ADMITTED -- a fresh
    fetch, and a legacy row re-verified -- and deliberately NOT from the
    race-lost path, where the request that won stored identical bytes and
    recorded the admission itself.

    Best-effort by construction: `record_admission` swallows its own failures,
    because a ledger problem must not fail a fetch somebody is waiting on.
    """
    from terrapod.services.artifact_ledger import coordinate, record_admission

    attestor: dict = {}
    if provenance is not None:
        publisher = getattr(provenance, "publisher", None)
        if publisher:
            attestor = {"publisher": publisher}

    await record_admission(
        db,
        digest=record.digest,
        kind="package",
        attestation=record.attestation,
        attestor=attestor,
        coordinates=coordinate(
            "package",
            ecosystem=record.ecosystem,
            name=record.name,
            version=record.version or "",
            filename=record.filename,
        ),
        admission={"storage_key": record.storage_key, "size": record.size},
    )


async def lookup_present(
    db: AsyncSession, storage: ObjectStore, ecosystem: str, name: str, filename: str
) -> CachedPackageFile | None:
    """A cached record whose object is really there, or None.

    **The row is not the truth; the store is.** A row whose object has gone —
    a pruned bucket, a recreated container, a partially restored backup — must be
    treated as a miss and re-fetched, never served as a redirect to a 404 that
    nobody can trace back to a database row. That is not hypothetical: it is what
    a stale row produced during this feature's own testing, and it looked exactly
    like a bug in the client.

    Every read path goes through here so the check cannot be skipped by one of
    them, which is precisely how it got skipped the first time.
    """
    record = await lookup(db, ecosystem, name, filename)
    if record is None:
        return None
    if record.attestation is None:
        # Admitted by a build that recorded nothing, i.e. before this cache verified
        # anything. Serving it would mean an operator cannot tell a verified artifact
        # from one that merely arrived, so it is a miss: re-fetched once, verified
        # under the current rules, and the row rewritten with what was checked.
        #
        # The row is left in place rather than deleted, which matters in two ways.
        # `fetch_and_cache` rewrites it once the artifact has been re-verified, so
        # the transition happens exactly once per artifact. And deleting here would
        # be destructive under `cache_only`: the re-fetch cannot succeed while
        # sealed, so the row would be gone for good rather than merely unserved.
        logger.info(
            "Cached package row carries no attestation; re-fetching to verify it",
            ecosystem=ecosystem,
            name=name,
            filename=filename,
        )
        return None
    if record.attestation == ATTEST_OPERATOR_UPLOAD:
        # An uploaded artifact gets the strictest check this path can afford, for
        # the reason the issue states as a requirement rather than a caveat: a
        # fetched artifact can be re-verified any time, because the publisher's
        # material is still upstream, and an uploaded one cannot be re-verified
        # against anything at all. So the digest recorded on receipt is its whole
        # integrity story, and never looking at it again is the one thing "we
        # trust the operator vetted it" must not become.
        #
        # What a REDIRECT permits is a metadata check: every read here ends in a
        # redirect to object storage, so the API never sees the bytes and cannot
        # hash them per request. `head` costs the same round trip as `exists`
        # (on S3 `exists` IS a head_object), and a size that no longer matches
        # catches a truncated write or a substitution of a different length.
        # `POST /admin/package-cache/{id}/verify` is the full digest check, on
        # demand; `uploads.verify_object` is what it calls.
        await _check_uploaded_object(storage, record, ecosystem, name, filename)
    elif not await storage.exists(record.storage_key):
        logger.warning(
            "Cached package row has no object; treating as a miss",
            ecosystem=ecosystem,
            name=name,
            filename=filename,
        )
        await db.delete(record)
        await db.flush()
        return None
    await touch(db, record)
    return record


async def _check_uploaded_object(
    storage: ObjectStore,
    record: CachedPackageFile,
    ecosystem: str,
    name: str,
    filename: str,
) -> None:
    """Refuse to serve an uploaded artifact whose object disagrees with its row.

    Raises :class:`UploadedArtifactUnavailable`; see that class for why this is
    not a miss. Split out so the serve path reads as one decision and so the
    refusal can be driven directly in a test.
    """
    from terrapod.storage.protocol import ObjectNotFoundError

    try:
        meta = await storage.head(record.storage_key)
    except ObjectNotFoundError as exc:
        raise UploadedArtifactUnavailable(
            f"{ecosystem} {name} {filename} was uploaded by "
            f"{record.uploaded_by or 'an operator'} and its object is missing from "
            "storage. It cannot be re-fetched -- there is no upstream for an "
            "uploaded artifact -- so it has to be uploaded again."
        ) from exc
    if meta.size_bytes != record.size:
        raise UploadedArtifactUnavailable(
            f"{ecosystem} {name} {filename} was uploaded as {record.size} bytes "
            f"({record.digest}) and the stored object is now {meta.size_bytes}. "
            "Refusing to serve it: an uploaded artifact's recorded digest is the "
            "whole of its integrity story, and these are not those bytes."
        )


async def get_or_fetch(
    db: AsyncSession,
    storage: ObjectStore,
    artifact: Artifact,
    *,
    client: httpx.AsyncClient | None = None,
) -> CachedPackageFile:
    """The cached artifact, fetching it first if we do not have it."""
    existing = await lookup_present(
        db, storage, artifact.ecosystem, artifact.name, artifact.filename
    )
    if existing is not None:
        return existing

    return await fetch_and_cache(db, storage, artifact, client=client)


async def store_document(
    db: AsyncSession, storage: ObjectStore, artifact: Artifact, data: bytes
) -> CachedPackageFile:
    """Cache a small document we already hold in memory.

    Used for index documents rather than distributions — an npm packument, which
    a sealed node has no other way to obtain and cannot serve an install without,
    because the dependency ranges live in it and nowhere else.

    Overwrites any existing copy: the newest fetch is the best answer a sealed
    node will ever have for what versions exist.
    """
    key = keys.package_cache_key(artifact.ecosystem, artifact.name, artifact.filename)
    await storage.put(key, data, content_type="application/json")

    existing = await lookup(db, artifact.ecosystem, artifact.name, artifact.filename)
    if existing is not None:
        now = datetime.now(UTC)
        existing.size = len(data)
        existing.version = artifact.version
        # The row describes the bytes just written, so its age restarts here.
        # Without this a `max_age` read against `cached_at` would expire a copy
        # that was refreshed a moment ago -- on every request, forever, which is
        # the same full upstream round trip the cache exists to remove.
        existing.cached_at = now
        existing.last_accessed_at = now
        await db.flush()
        return existing

    record = CachedPackageFile(
        ecosystem=artifact.ecosystem,
        name=artifact.name,
        version=artifact.version,
        filename=artifact.filename,
        storage_key=key,
        size=len(data),
        digest=artifact.digest,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        found = await lookup(db, artifact.ecosystem, artifact.name, artifact.filename)
        if found is None:  # pragma: no cover — the constraint says otherwise
            raise
        return found
    return record


def _age(record: CachedPackageFile) -> timedelta:
    """How long ago this document was written.

    The column is timezone-aware, but a row that reached us from somewhere that
    dropped the offset must not raise out of an index route: a comparison error
    there reads as the registry being down.
    """
    stamp = record.cached_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - stamp


async def load_document(
    db: AsyncSession,
    storage: ObjectStore,
    ecosystem: str,
    name: str,
    filename: str,
    *,
    max_age: timedelta | None = None,
) -> bytes | None:
    """A cached document's bytes, or None if we hold no usable copy.

    `max_age` bounds a document whose content can change upstream -- a list of
    which versions exist. Omit it for one that cannot: an immutable per-version
    detail would only be re-fetched byte for byte. An expired copy returns None
    exactly as an absent one does, so a caller has one cold path rather than two.

    **A hit touches `last_accessed_at`.** Retention is access-based, so a
    document served from here and never touched looks permanently unused and is
    reaped however heavily it is read -- which then re-fetches it, in a loop.
    """
    record = await lookup(db, ecosystem, name, filename)
    if record is None:
        return None
    if max_age is not None and _age(record) > max_age:
        return None
    try:
        data = await storage.get(record.storage_key)
    except Exception:
        # The row promised something the store does not have. Treated as a miss;
        # on a sealed node that becomes an honest "not cached" rather than a 500.
        logger.warning("Cached document missing from storage", key=record.storage_key)
        return None
    await touch(db, record)
    return data


async def cached_filenames(db: AsyncSession, ecosystem: str, name: str) -> list[str]:
    """Every artifact filename cached for a package.

    What a sealed node can actually serve, which is what its index must describe:
    advertising a version whose bytes are not here sends a client to ask for it
    and get a 503 it cannot do anything about.
    """
    result = await db.execute(
        select(CachedPackageFile.filename).where(
            CachedPackageFile.ecosystem == ecosystem,
            CachedPackageFile.name == name,
        )
    )
    return list(result.scalars().all())


async def cached_files(db: AsyncSession, ecosystem: str, name: str) -> list[CachedPackageFile]:
    """Every cached artifact row for a package."""
    result = await db.execute(
        select(CachedPackageFile)
        .where(
            CachedPackageFile.ecosystem == ecosystem,
            CachedPackageFile.name == name,
        )
        .order_by(CachedPackageFile.filename)
    )
    return list(result.scalars().all())
