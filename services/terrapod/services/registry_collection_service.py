"""Publishing Ansible collections to Terrapod's own registry (#1482).

`ansible-galaxy collection publish` sends one multipart POST carrying the
tarball and nothing else — no manifest, no signature, no declared coordinates.
Everything this module knows about a collection therefore comes from the archive
itself, which is why the archive is opened and read rather than trusted.

Three consequences shape the code:

* **The coordinates come from `MANIFEST.json`, not the request.** There is
  nowhere else for them to come from, and taking them from a client-supplied
  field would let one publisher overwrite another's namespace.
* **The digest is computed from what was stored**, never copied from anything
  asserted. `ansible-galaxy` checks the bytes it receives against the
  `artifact.sha256` we advertise, so a digest that describes something other
  than the stored file turns a verified install into a broken one.
* **`dependencies` is extracted and kept.** Resolution reads it from the version
  detail; a published collection without it resolves as though it had none,
  which is a silent wrong answer rather than an error.

Archive work is synchronous and potentially large, so it runs in a worker thread
and against a file on the ephemeral PVC — never the RAM-backed `/tmp`, and never
in the event loop (rules 13 and 14).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tarfile
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import RegistryCollection, RegistryCollectionVersion
from terrapod.logging_config import get_logger
from terrapod.storage import keys
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: The collection spec's own shape for a namespace or name.
_SEGMENT = re.compile(r"^[a-z0-9_]+$")

#: Permissive about the semver tail, strict about anything that could escape a
#: path — the version is interpolated into a storage key and a URL.
_VERSION = re.compile(r"^[A-Za-z0-9._+-]+$")

#: Read size for hashing. Large enough not to be a million round trips on a
#: sizeable collection, small enough that memory stays flat.
_CHUNK = 1024 * 1024


class PublishError(ValueError):
    """The upload is not a publishable collection. Surfaces as HTTP 400."""


def _read_manifest(path: str) -> dict:
    """Pull `collection_info` out of a collection tarball. Synchronous.

    A collection archive carries `MANIFEST.json` at its root — sometimes bare,
    sometimes under a single top-level directory, depending on how it was built.
    Both layouts are accepted because both are produced by `ansible-galaxy
    collection build` across versions, and rejecting one would refuse a
    perfectly good artifact for a cosmetic reason.

    Members are matched by name rather than extracted, so nothing is ever
    written to disk from an untrusted archive and path traversal has no surface
    to act on.
    """
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = None
            for candidate in tar.getmembers():
                parts = candidate.name.split("/")
                if parts[-1] == "MANIFEST.json" and len(parts) <= 2:
                    member = candidate
                    break
            if member is None:
                raise PublishError(
                    "no MANIFEST.json in the archive — this is not a collection built by "
                    "`ansible-galaxy collection build`"
                )
            handle = tar.extractfile(member)
            if handle is None:
                raise PublishError("MANIFEST.json is not a regular file")
            raw = handle.read()
    except tarfile.TarError as exc:
        raise PublishError(f"could not read the archive: {exc}") from exc

    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise PublishError("MANIFEST.json is not valid JSON") from exc

    info = document.get("collection_info")
    if not isinstance(info, dict):
        raise PublishError("MANIFEST.json has no collection_info")
    return info


def _read_manifest_bytes(path: str) -> bytes:
    """The raw MANIFEST.json bytes from the archive. Synchronous.

    Signatures are produced over the file exactly as it sits in the tarball, so
    verification has to read those bytes rather than re-serialise the parsed
    manifest — `json.dumps` of a decoded document is a different byte string and
    would fail against a perfectly good signature.
    """
    with tarfile.open(path, "r:gz") as tar:
        for candidate in tar.getmembers():
            parts = candidate.name.split("/")
            if parts[-1] == "MANIFEST.json" and len(parts) <= 2:
                handle = tar.extractfile(candidate)
                if handle is not None:
                    return handle.read()
    raise PublishError("no MANIFEST.json in the archive")


def _digest_and_size(path: str) -> tuple[str, int]:
    """sha256 and byte count of the file on disk. Synchronous.

    Computed from the stored bytes so the digest we advertise describes exactly
    what a client will receive.
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _coordinates(info: dict) -> tuple[str, str, str]:
    """Validate and return `(namespace, name, version)` from the manifest."""
    namespace = info.get("namespace")
    name = info.get("name")
    version = info.get("version")
    for label, value, pattern in (
        ("namespace", namespace, _SEGMENT),
        ("name", name, _SEGMENT),
        ("version", version, _VERSION),
    ):
        if not isinstance(value, str) or not pattern.match(value):
            raise PublishError(f"MANIFEST.json has an unusable {label}: {value!r}")
    return namespace, name, version  # type: ignore[return-value]


async def get_collection(db: AsyncSession, namespace: str, name: str) -> RegistryCollection | None:
    result = await db.execute(
        select(RegistryCollection).where(
            RegistryCollection.namespace == namespace,
            RegistryCollection.name == name,
        )
    )
    return result.scalar_one_or_none()


async def get_version(
    db: AsyncSession, namespace: str, name: str, version: str
) -> RegistryCollectionVersion | None:
    result = await db.execute(
        select(RegistryCollectionVersion)
        .join(RegistryCollection)
        .where(
            RegistryCollection.namespace == namespace,
            RegistryCollection.name == name,
            RegistryCollectionVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def list_versions(
    db: AsyncSession, namespace: str, name: str
) -> list[RegistryCollectionVersion]:
    result = await db.execute(
        select(RegistryCollectionVersion)
        .join(RegistryCollection)
        .where(
            RegistryCollection.namespace == namespace,
            RegistryCollection.name == name,
        )
        .order_by(RegistryCollectionVersion.created_at)
    )
    return list(result.scalars().all())


async def get_version_by_id(db: AsyncSession, version_id: str) -> RegistryCollectionVersion | None:
    """Look a version up by its own id — how the import poll answers.

    The publish response hands `ansible-galaxy` a task URL whose last path
    segment it treats as an import id (see `docs/galaxy-cli-surface.md`). Using
    the version row's id means the poll is answered from the same fact the
    publish established, with nothing extra to store or expire.
    """
    import uuid as _uuid

    try:
        parsed = _uuid.UUID(version_id)
    except ValueError:
        return None
    result = await db.execute(
        select(RegistryCollectionVersion).where(RegistryCollectionVersion.id == parsed)
    )
    return result.scalar_one_or_none()


async def publish(
    db: AsyncSession,
    storage: ObjectStore,
    tmp_path: str,
    *,
    owner_email: str,
) -> RegistryCollectionVersion:
    """Publish a collection archive already streamed to the PVC.

    Returns the version row. Raises :class:`PublishError` for anything wrong
    with the archive, which the router turns into a 400 — the client shows it,
    so the message names what is wrong with the file rather than the request.

    Republishing an existing version is refused. Galaxy's own registry treats a
    published version as immutable, and so does everything downstream: a client
    that has already resolved `1.0.0` and cached its digest would silently
    receive different bytes under the same name.
    """
    info = await asyncio.to_thread(_read_manifest, tmp_path)
    namespace, name, version = _coordinates(info)

    existing = await get_version(db, namespace, name, version)
    if existing is not None:
        raise PublishError(
            f"{namespace}.{name}:{version} is already published; "
            "publish a new version rather than replacing one"
        )

    sha256, size = await asyncio.to_thread(_digest_and_size, tmp_path)

    collection = await get_collection(db, namespace, name)
    if collection is None:
        collection = RegistryCollection(
            namespace=namespace, name=name, owner_email=owner_email, labels={}
        )
        db.add(collection)
        await db.flush()

    # Storage before the row: a row promising an object that is not there is a
    # 404 at install time, whereas an object with no row is invisible and
    # harmless until the retention story for orphans is built.
    from terrapod.api.upload_stream import file_chunks

    key = keys.collection_tarball_key(namespace, name, version)
    await storage.put_stream(key, file_chunks(tmp_path), content_type="application/gzip")

    row = RegistryCollectionVersion(
        collection_id=collection.id,
        version=version,
        artifact_sha256=sha256,
        size=size,
        manifest=info,
    )
    db.add(row)
    await db.flush()

    logger.info(
        "Collection published",
        collection=f"{namespace}.{name}",
        version=version,
        size=size,
    )
    return row


class SignatureError(ValueError):
    """The signature is not one this platform will vouch for. HTTP 422."""


async def attach_signature(
    db: AsyncSession,
    storage: ObjectStore,
    namespace: str,
    name: str,
    version: str,
    sig_bytes: bytes,
) -> RegistryCollectionVersion:
    """Verify a detached signature over the collection's MANIFEST.json, and keep it.

    The trust gate, and the same shape the provider registry uses: the signature
    is checked against a key **already registered** with this platform, and the
    server never re-signs. A signature from an unregistered key is refused with
    422 rather than stored unverified — storing it would let the registry
    advertise a signature it cannot itself vouch for, which is worse than
    advertising none.

    The manifest is read back out of the *stored* artifact rather than anything
    supplied alongside the signature, so what is verified is what a client will
    download.
    """
    from terrapod.services.gpg_key_service import (
        extract_signature_key_id,
        get_gpg_key_by_key_id,
        verify_detached_signature,
    )

    row = await get_version(db, namespace, name, version)
    if row is None:
        raise SignatureError(f"{namespace}.{name}:{version} is not published")

    key_id = await extract_signature_key_id(sig_bytes)
    if not key_id:
        raise SignatureError("could not parse a key ID from the signature")

    gpg_key = await get_gpg_key_by_key_id(db, key_id)
    if gpg_key is None:
        raise SignatureError(
            f"signature was made by key {key_id}, which is not registered with this "
            "platform; register the public key before publishing a signature for it"
        )

    key = keys.collection_tarball_key(namespace, name, version)
    tmpdir = _resolve_tmpdir()
    fd, tmp_path = await asyncio.to_thread(tempfile.mkstemp, suffix=".sigcheck.tar.gz", dir=tmpdir)
    try:
        handle = await asyncio.to_thread(os.fdopen, fd, "wb")
        try:
            async for chunk in storage.get_stream(key):
                await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
        manifest_bytes = await asyncio.to_thread(_read_manifest_bytes, tmp_path)
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    if not await verify_detached_signature(gpg_key.ascii_armor, manifest_bytes, sig_bytes):
        raise SignatureError(
            "the signature does not verify against the registered key over this "
            "collection's MANIFEST.json"
        )

    row.signature = sig_bytes.decode("utf-8", errors="replace")
    row.signing_key_id = key_id
    await db.flush()
    logger.info(
        "Collection signature verified",
        collection=f"{namespace}.{name}",
        version=version,
        key_id=key_id,
    )
    return row


def _resolve_tmpdir() -> str | None:
    """The ephemeral PVC, or None for local dev (rule 14).

    Matches `upload_stream.resolve_ephemeral_tmpdir` and its siblings; a
    collection archive is not small enough to stage in the RAM-backed `/tmp`.
    """
    from terrapod.config import settings

    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None
