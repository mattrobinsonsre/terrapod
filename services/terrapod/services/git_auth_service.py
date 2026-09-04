"""Git module-auth source resolution (#1028).

At ``next_run`` the server resolves each git-auth workspace variable's *source*
into a **concrete** credential before delivery, so the runner phase
(:mod:`terrapod.runner.phases.git_auth`) is source-agnostic — it always receives
a concrete ``{username, token}`` / key, never a reference.

Sources for ``git_http_auth``:

* **static** — ``{"source":"static","username","token","rewrite"}`` — passed
  through (a raw operator PAT).
* **vcs_connection** (flagship) — ``{"source":"vcs_connection",
  "vcs_connection_id","rewrite"}`` — a short-lived git-HTTPS token minted from the
  referenced :class:`VCSConnection` (GitHub-App installation token via
  ``github_service`` / GitLab access token), the same minting the VCS poller uses.

``git_ssh_auth`` is static only (VCS connections mint HTTPS tokens, not SSH keys).

A credential that can't be resolved (missing/unknown connection, mint failure,
malformed value) is **dropped with a logged warning** — one bad cred must never
fail the run.
"""

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import VCSConnection
from terrapod.services import github_service

logger = structlog.get_logger("git_auth")

_HTTP = "git_http_auth"
_SSH = "git_ssh_auth"


async def resolve_git_auth(db: AsyncSession, resolved: list) -> list[dict]:
    """Resolve the git-category resolved variables into concrete delivery entries.

    ``resolved`` is the full list of ``ResolvedVariable`` from
    ``resolve_variables``; only the two git categories are consumed. Returns
    ``[{category, key, value}]`` where ``value`` is the concrete credential JSON
    (any ``vcs_connection`` source already minted to ``{username, token}``).
    """
    out: list[dict] = []
    for v in resolved:
        if v.category not in (_HTTP, _SSH):
            continue
        try:
            cred = json.loads(v.value)
        except ValueError, TypeError:
            logger.warning("git-auth variable has a non-JSON value; skipping", key=v.key)
            continue

        if v.category == _SSH:
            # SSH keys are static only — pass the value through verbatim.
            out.append({"category": _SSH, "key": v.key, "value": v.value})
            continue

        rewrite = cred.get("rewrite", "none")
        source = cred.get("source", "static")
        if source == "vcs_connection":
            concrete = await _mint_from_connection(db, cred.get("vcs_connection_id"), rewrite)
            if concrete is None:
                continue  # already logged
        else:  # static
            if not cred.get("token"):
                logger.warning("static git_http_auth has no token; skipping", key=v.key)
                continue
            concrete = {
                "username": cred.get("username") or "x-access-token",
                "token": cred["token"],
                "rewrite": rewrite,
            }
        out.append({"category": _HTTP, "key": v.key, "value": json.dumps(concrete)})
    return out


async def _mint_from_connection(db: AsyncSession, ref, rewrite: str) -> dict | None:
    """Mint a concrete ``{username, token, rewrite}`` from a VCS connection, or
    ``None`` (logged) if it can't be resolved."""
    if not ref:
        logger.warning("git-auth vcs_connection source missing vcs_connection_id")
        return None
    try:
        conn_uuid = uuid.UUID(str(ref).removeprefix("vcs-"))
    except ValueError:
        logger.warning("git-auth has an invalid vcs_connection_id", ref=str(ref))
        return None
    conn = await db.get(VCSConnection, conn_uuid)
    if conn is None:
        logger.warning("git-auth references an unknown VCS connection", ref=str(ref))
        return None
    try:
        if conn.provider == "github":
            token = await github_service.get_installation_token(conn)
            username = "x-access-token"
        elif conn.provider == "gitlab":
            if not conn.token:
                logger.warning("git-auth GitLab connection has no token", ref=str(ref))
                return None
            token, username = conn.token, "oauth2"
        else:
            logger.warning(
                "git-auth VCS connection has an unsupported provider", provider=conn.provider
            )
            return None
    except Exception as exc:  # noqa: BLE001 — best-effort mint; never fail the run over one cred
        logger.warning(
            "failed to mint git-auth token from VCS connection", ref=str(ref), error=str(exc)
        )
        return None
    return {"username": username, "token": token, "rewrite": rewrite}
