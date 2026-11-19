"""Inventorying a cached artifact into an SBOM (#2589 step 2).

Syft GENERATES the document and Trivy CONSUMES it. That is why there is no third
tool here: `trivy sbom` matches a Syft-produced CycloneDX document directly, so
the second line of defence needs no new feed -- a known-malicious package is
flagged CRITICAL from the SBOM alone, in both ecosystems.

**The SBOM is evidence, never a gate.** It is produced after an artifact has
already been admitted, and a finding on it is reported rather than enforced. For
an operator upload that is not a preference but the only coherent position:
gating on our own scan would second-guess the attestor we have just said we
trust (#2599).

So every failure here degrades. An artifact with no inventory is recorded as
having none, which is a different and honest fact from "inventoried, nothing
found" -- the ledger's `sbom` is nullable precisely so those two can be told
apart.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: What Syft is asked for. CycloneDX because that is what `trivy sbom` consumes
#: -- measured end to end rather than assumed from the format's documentation.
SBOM_FORMAT = "cyclonedx-json"

#: **Required, and the defaults are not enough.** Syft's default DIRECTORY
#: catalogers exclude javascript's installed cataloger, so an extracted npm
#: tarball laid out correctly as `node_modules/<name>/package.json` reports "No
#: packages discovered". With this, the same directory yields `lodash 4.17.21
#: npm`. Measured; it leaves the python and go cases unchanged (1, 1, 275).
_CATALOGERS = ("--override-default-catalogers", "all")

#: Archive suffixes the caches actually hold. Syft has NO generic archive
#: scheme -- `syft --help` lists only docker-archive, oci-archive, oci-dir, dir
#: and file -- so pointing `file:` at a wheel or an npm tarball produces a
#: perfectly well-formed CycloneDX document with an EMPTY component list. These
#: have to be extracted and scanned as a directory.
_ZIP_SUFFIXES = (".whl", ".zip", ".jar", ".nupkg", ".egg")
_TAR_SUFFIXES = (".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar")

#: Bounds on extraction. An artifact is attacker-influenced input -- it is
#: whatever upstream served -- so a decompression bomb must cost a bounded
#: amount of PVC rather than the whole volume. Generous enough that no real
#: package comes close: the largest wheels are tens of megabytes.
_MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_EXTRACTED_MEMBERS = 200_000

#: An inventory must not hold a transfer open indefinitely. Generous, because a
#: large archive on a loaded node is slow rather than broken, and the cost of
#: being wrong is a missing SBOM rather than a failed admission.
_TIMEOUT_SECONDS = 300.0

_SEALED_NOTE = (
    "syft is not cached and this node is sealed (registry.cache_only), so cached "
    "artifacts are admitted without an inventory rather than fetched from upstream. "
    "The ledger records that no SBOM exists, which is not the same as an empty one."
)
_FAILURE_NOTE = (
    "could not obtain syft, so this artifact is admitted without an inventory. "
    "Admission is unaffected -- an SBOM is evidence, not a gate -- and the ledger "
    "records the absence rather than an empty document."
)


async def syft_binary() -> str | None:
    """Path to a usable `syft`, or None. None is a normal outcome."""
    from terrapod.services import api_tool

    return await api_tool.tool_binary("syft", sealed_note=_SEALED_NOTE, failure_note=_FAILURE_NOTE)


def _is_archive(path: Path) -> str | None:
    """`"zip"`, `"tar"`, or None for something Syft can read directly.

    By suffix rather than by magic bytes, deliberately: the caches name their
    objects after what upstream published, so the suffix is upstream's own
    statement of the format, and sniffing would let a mislabelled artifact
    choose a different extractor from the one its name implies.
    """
    name = path.name.lower()
    if name.endswith(_ZIP_SUFFIXES):
        return "zip"
    if name.endswith(_TAR_SUFFIXES):
        return "tar"
    return None


def _extract_zip(archive: Path, into: Path) -> None:
    """Extract a zip, member by member, bounded and without traversal."""
    import zipfile

    total = 0
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > _MAX_EXTRACTED_MEMBERS:
            raise ValueError(f"{archive.name} holds {len(members)} members")
        for info in members:
            if info.is_dir():
                continue
            total += info.file_size
            if total > _MAX_EXTRACTED_BYTES:
                raise ValueError(f"{archive.name} expands past the extraction bound")
            # `Path.resolve()` on the joined path, compared against the root:
            # a member named `../../x` or an absolute path cannot land outside.
            target = (into / info.filename).resolve()
            if not target.is_relative_to(into.resolve()):
                raise ValueError(f"{archive.name} has a member escaping the root")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)


def _extract_tar(archive: Path, into: Path) -> None:
    """Extract a tarball, bounded, with Python's own traversal filter."""
    import tarfile

    total = 0
    count = 0
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            count += 1
            total += member.size
            if count > _MAX_EXTRACTED_MEMBERS or total > _MAX_EXTRACTED_BYTES:
                raise ValueError(f"{archive.name} expands past the extraction bound")
            # `filter="data"` is Python's own hardening: it refuses absolute
            # paths, `..` components, links pointing outside, devices and setuid
            # bits. Using it rather than hand-rolling those checks, because the
            # stdlib's version is the one that gets fixed when a new escape is
            # found.
            tar.extract(member, path=into, filter="data")


async def inventory(path: str | Path) -> dict | None:
    """A CycloneDX SBOM for the artifact at `path`, or None.

    None means no inventory could be produced -- syft unavailable, the scan
    failed, the archive could not be extracted, or it timed out. The caller
    records that absence rather than treating it as an empty inventory, because
    "we did not look" and "we looked and found nothing" are different facts and
    only one of them is reassuring.

    **An archive is extracted first**, and that is not an optimisation. Syft has
    no generic archive scheme, so scanning a wheel or an npm tarball directly
    returns a valid CycloneDX document with an empty component list -- which
    passes every schema check and inventories nothing. Measured on both.
    """
    binary = await syft_binary()
    if binary is None:
        return None

    source = Path(path)
    kind = _is_archive(source)
    if kind is None:
        return await _scan(binary, f"file:{source}", str(source))

    import shutil
    import tempfile

    from terrapod.services.api_tool import tool_dir

    # On the PVC, not `/tmp`: `/tmp` on the API pod is RAM-backed and an
    # extracted package is exactly the kind of thing rule 14 keeps out of
    # memory.
    root = tool_dir() / "sbom"
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=root, prefix="inv."))
    try:
        extract = _extract_zip if kind == "zip" else _extract_tar
        try:
            await asyncio.to_thread(extract, source, workdir)
        except Exception as exc:  # noqa: BLE001 - every failure degrades the same way
            logger.warning(
                "could not extract an artifact to inventory it",
                path=str(source),
                kind=kind,
                error=str(exc),
            )
            return None
        return await _scan(binary, f"dir:{workdir}", str(source))
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _scan(binary: str, target: str, label: str) -> dict | None:
    """Run syft against one resolved source. None on any failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            target,
            "-o",
            SBOM_FORMAT,
            *_CATALOGERS,
            "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("could not execute syft", path=label, error=str(exc))
        return None

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), _TIMEOUT_SECONDS)
    except TimeoutError:
        # Kill it, or the orphan keeps the PVC file open and the CPU busy long
        # after the admission it was for has finished.
        process.kill()
        await process.wait()
        logger.warning("syft timed out inventorying an artifact", path=label)
        return None

    if process.returncode != 0:
        logger.warning(
            "syft could not inventory an artifact",
            path=label,
            returncode=process.returncode,
            stderr=stderr.decode("utf-8", "replace")[:400],
        )
        return None

    try:
        document = json.loads(stdout)
    except ValueError as exc:
        logger.warning("syft produced output that is not JSON", path=label, error=str(exc))
        return None

    if not isinstance(document, dict):
        logger.warning("syft produced a document that is not an object", path=label)
        return None

    logger.info(
        "Inventoried an artifact",
        path=label,
        # Counted on `purl`, not on `components`: a wheel's component list is
        # mostly FILE entries (`/flask-3.0.0.dist-info/METADATA` and friends),
        # so a raw count reads as thirty times more inventory than there is.
        packages=packages_in(document),
        format=document.get("bomFormat"),
        spec_version=document.get("specVersion"),
    )
    return document


def packages_in(document: dict) -> int:
    """How many REAL packages a CycloneDX document names.

    A component without a `purl` is a file entry rather than a package, and the
    two are mixed in the same list. This is the number worth asserting on: every
    one of the zero-inventory cases measured in #2589 produced a well-formed
    document, so "valid CycloneDX" says nothing about whether anything was
    found.
    """
    return sum(
        1 for c in (document.get("components") or []) if isinstance(c, dict) and c.get("purl")
    )


def _reset_for_tests() -> None:
    from terrapod.services import api_tool

    api_tool._reset_for_tests()
