"""Shared JSON:API serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException


def engine_version_attr(attrs: dict[str, Any], default: str) -> str:
    """Read the engine version under either name, preferring `engine-version`.

    The column pins the version of whichever engine the workspace runs, so
    ``engine-version`` is the canonical attribute (#1559). ``terraform-version``
    is the name it had when Terraform was the only engine.

    **`terraform-version` is not going away.** It is go-tfe's own attribute
    name, so `tofu`, `terraform` and `tfci` send and read it on the TFE
    compatibility surface; accepting and returning both is permanent, not a
    deprecation window. This mirrors ``structured``/``hcl`` in
    ``routers/variables.py``, for the same reason.

    Supplying both with different values is a 422 rather than a silent
    precedence rule -- a client that disagrees with itself about which version
    to run has a bug, and picking a winner would hide it. An empty string is a
    value, not an absence: it means "the deployment's default", so only the key
    being absent falls through to ``default``.
    """
    has_engine = "engine-version" in attrs
    has_terraform = "terraform-version" in attrs
    if (
        has_engine
        and has_terraform
        and str(attrs["engine-version"] or "") != str(attrs["terraform-version"] or "")
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "'engine-version' and 'terraform-version' are the same version "
                "under two names and disagree; send either one, or both with "
                "the same value"
            ),
        )
    if has_engine:
        return str(attrs["engine-version"] or "")
    if has_terraform:
        return str(attrs["terraform-version"] or "")
    return default


def rfc3339(dt: datetime | None) -> str | None:
    """Serialize a tz-aware UTC datetime as RFC3339 with a trailing ``Z``
    (never ``+00:00``).

    Rule 10 / go-tfe compatibility: ``datetime.isoformat()`` on a tz-aware UTC
    column emits ``...+00:00``, which `go-tfe` rejects. This is the one canonical
    serializer — prefer it over per-router ``_rfc3339`` copies that have drifted
    (some used bare ``.isoformat()`` and regressed the ``Z`` suffix).
    """
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
