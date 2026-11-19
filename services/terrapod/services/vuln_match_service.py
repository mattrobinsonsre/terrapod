"""Matching a stored SBOM against the vulnerability database (#2589 steps 2c, 2d).

**Syft generates and Trivy consumes.** That is why there is no third tool and no
new feed: `trivy sbom` reads the CycloneDX document the inventory already wrote,
so the second line of defence costs one more invocation of a binary the
deployment already caches. The content check (2d) comes with it rather than
separately -- Trivy flags a known-malicious package as CRITICAL from the SBOM
alone, in both ecosystems.

**Matching is re-derivable, and that is the design.** A finding is a statement
about an SBOM *and* a database epoch, so it goes stale when the database moves
rather than when the artifact does. Keeping the SBOM means a re-match is a local
operation -- no re-download, no re-extraction -- which is what makes it
affordable to redo the whole ledger when a new CVE lands.

**It is evidence, never a gate**, for the same reason the SBOM is: this runs
long after admission, and for an operator upload gating on our own scan would
second-guess the attestor we just said we trust (#2599). A finding is reported.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import structlog

from terrapod.db.models import now_utc

logger = structlog.get_logger(__name__)

_TIMEOUT_SECONDS = 300.0

_SEALED_NOTE = (
    "trivy is not cached and this node is sealed (registry.cache_only), so inventoried "
    "artifacts are not matched against the vulnerability database. The ledger keeps the "
    "SBOM, so a match costs nothing once trivy is available."
)
_FAILURE_NOTE = (
    "could not obtain trivy, so inventoried artifacts are not matched. The SBOM is kept, "
    "so this is a deferral rather than a loss."
)


async def trivy_binary() -> str | None:
    """Path to a usable `trivy`, or None. None is a normal outcome."""
    from terrapod.services import api_tool

    return await api_tool.tool_binary("trivy", sealed_note=_SEALED_NOTE, failure_note=_FAILURE_NOTE)


async def match(sbom: dict) -> tuple[list, dict] | None:
    """Findings for one SBOM, with the provenance that makes them reproducible.

    Returns `(matches, provenance)`, or None when no match could be made at all
    -- trivy absent, its database unreachable, the scan failed. None is distinct
    from an empty list on purpose: "nothing matched" is a reassuring claim, and
    only one of the two establishes it.
    """
    binary = await trivy_binary()
    if binary is None:
        return None

    from terrapod.services.api_tool import tool_dir

    # On the PVC, not `/tmp`: an SBOM for a large artifact is substantial, and
    # `/tmp` on the API pod is RAM-backed (rule 14).
    root = tool_dir() / "match"
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix="sbom.", suffix=".cdx.json")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        await asyncio.to_thread(tmp.write_text, json.dumps(sbom))
        return await _scan(binary, tmp)
    finally:
        await asyncio.to_thread(tmp.unlink, True)


async def _scan(binary: str, path: Path) -> tuple[list, dict] | None:
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "sbom",
            str(path),
            "--format",
            "json",
            "--quiet",
            # Trivy keeps its vulnerability database beside the other tools, so
            # it is fetched once per pod rather than per artifact.
            "--cache-dir",
            str(path.parent.parent / "trivy-cache"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("could not execute trivy", error=str(exc))
        return None

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), _TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("trivy timed out matching an SBOM", path=str(path))
        return None

    if process.returncode != 0:
        # The commonest cause is the vulnerability database being unreachable,
        # which is a deferral rather than a verdict -- so None, never an empty
        # findings list.
        logger.warning(
            "trivy could not match an SBOM",
            returncode=process.returncode,
            stderr=stderr.decode("utf-8", "replace")[:400],
        )
        return None

    try:
        report = json.loads(stdout)
    except ValueError as exc:
        logger.warning("trivy produced output that is not JSON", error=str(exc))
        return None
    if not isinstance(report, dict):
        return None

    return _findings(report), _provenance(report, await _db_epoch(binary))


#: Trivy reports a malicious package as a vulnerability of this class, so the
#: content check (2d) needs no separate pass -- it is a severity and a type in
#: the same report.
MALICIOUS = "malicious"


def _findings(report: dict) -> list:
    """Flatten Trivy's per-target results into one list of findings.

    Keeps the fields an operator acts on and drops the prose: a ledger row that
    carried every description would be large, and the description is re-derivable
    from the id by anybody who wants it.
    """
    out: list = []
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            if not isinstance(v, dict):
                continue
            out.append(
                {
                    "id": v.get("VulnerabilityID"),
                    "package": v.get("PkgName"),
                    "installed": v.get("InstalledVersion"),
                    "fixed": v.get("FixedVersion") or None,
                    "severity": (v.get("Severity") or "").upper(),
                    # What makes this a CONTENT check as well as a vulnerability
                    # one: a package flagged malicious is not a bug in a
                    # dependency, it is the artifact being hostile.
                    "malicious": (v.get("Type") or "").lower() == MALICIOUS
                    or MALICIOUS in (v.get("Title") or "").lower(),
                    "purl": (v.get("PkgIdentifier") or {}).get("PURL"),
                }
            )
    return out


async def _db_epoch(binary: str) -> dict:
    """The vulnerability database's epoch, from `trivy version --format json`.

    **Not from the scan report**, which was the obvious place and does not carry
    it: measured, a `trivy sbom` report has no `Metadata` key at all, so reading
    the epoch there yields `{}` for every scan and the re-match trigger silently
    degrades to "did the matcher version change". `trivy version` reports
    `VulnerabilityDB.{Version,UpdatedAt,NextUpdate}` and is a fast local call --
    the database is already on disk by the time a scan has run.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "version",
            "--format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 30.0)
    except (OSError, TimeoutError):
        return {}
    if process.returncode != 0:
        return {}
    try:
        payload = json.loads(stdout)
    except ValueError:
        return {}

    db = (payload if isinstance(payload, dict) else {}).get("VulnerabilityDB") or {}
    return {
        k: db[v]
        for k, v in (
            ("version", "Version"),
            ("updated_at", "UpdatedAt"),
            ("next_update", "NextUpdate"),
        )
        if db.get(v) is not None
    }


def _provenance(report: dict, db: dict) -> dict:
    """What makes a finding reproducible rather than merely asserted.

    Without this, "no findings" is undated and unattributable -- and a stale
    empty list is indistinguishable from a fresh one, which is exactly the claim
    somebody will rely on.
    """
    from terrapod.config import settings

    return {
        "matcher": "trivy",
        # The binary's OWN reported version where it gives one, falling back to
        # the configured pin: an operator-uploaded trivy can differ from the
        # value in values.yaml, and the provenance must say what actually ran.
        "matcher_version": (report.get("Trivy") or {}).get("Version")
        or settings.registry.platform_tools.trivy_version,
        "sbom_generator": "syft",
        "sbom_generator_version": settings.registry.platform_tools.syft_version,
        # Trivy stamps the database it used into the report; without it a
        # finding cannot be tied to an epoch and re-matching has no trigger.
        "db": db,
        "matched_at": now_utc().isoformat(),
    }
