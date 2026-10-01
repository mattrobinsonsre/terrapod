"""Git module-auth source resolution (#1028).

At ``next_run`` the server resolves each git-auth workspace variable's *source*
into a **concrete** credential before delivery, so the runner phase
(:mod:`terrapod.runner.phases.git_auth`) is source-agnostic — it always receives
a concrete ``{username, token}`` / key, never a reference.

Sources for ``git_http_auth``:

* **static** — ``{"source":"static","username","token","rewrite"}`` — passed
  through (a raw operator PAT).
* **vcs_connection** (flagship on GitHub) — ``{"source":"vcs_connection",
  "vcs_connection_id","rewrite"}`` — a git-HTTPS token derived from the
  referenced :class:`VCSConnection`, the same derivation the VCS poller uses.
  On **GitHub** that is a short-lived installation token minted per run and
  narrowed to ``contents: read``. On **GitLab** there is nothing to mint: the
  connection's stored access token is the credential, and it is gated — see
  :data:`_GITLAB_REFUSAL`.

``git_ssh_auth`` is static only (VCS connections mint HTTPS tokens, not SSH keys).

A credential that can't be resolved (missing/unknown connection, mint failure,
malformed value) is **dropped with a logged warning** — one bad cred must never
fail the run.

The one exception is a **refusal**, which fails the run with :class:`GitAuthRefused`
rather than dropping. Dropping is right for an accident; it is wrong for a policy
decision, because the operator who set the switch gets no signal and the run
instead fails later inside ``init`` with an error naming neither the credential
nor the cause. See :data:`_GITLAB_REFUSAL` for the one case.
"""

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import VCSConnection
from terrapod.services import github_service

logger = structlog.get_logger("git_auth")

_HTTP = "git_http_auth"
_SSH = "git_ssh_auth"


class GitAuthRefused(RuntimeError):
    """Delivering a configured git credential is forbidden by policy.

    Distinct from the drop paths around it: the credential resolved fine and we
    are declining to hand it over. The caller errors the run with this message,
    so the operator reads the reason once rather than debugging a clone failure.
    """


#: Why a GitLab VCS connection cannot be a git credential unless an operator
#: says so. A GitLab connection holds a Personal or Group Access Token someone
#: pasted in, and there is no call that returns a narrower copy of one — so the
#: runner gets it whole, with every permission and every project it covers, in a
#: Job that is also running workspace-supplied IaC. The connection is named in a
#: variable *value*, so the chooser is whoever can set a workspace variable, not
#: an admin. GitHub needs no switch: its installation token is minted per run and
#: narrowed to `contents: read`, which is all a clone needs.
_GITLAB_REFUSAL = (
    "git credential {key!r} references GitLab VCS connection {ref} and "
    "`api.config.vcs.gitlab.allow_token_delivery_to_runners` is off, so the run "
    "is refused rather than given the credential. A GitLab connection stores an "
    "access token that cannot be narrowed: the runner would receive it whole, "
    "with every permission and every project it covers, and any workspace owner "
    "can name any connection in a variable value. Either set that key to true to "
    "accept the disclosure, or replace the variable with a `static` git_http_auth "
    "credential holding a token you scoped yourself."
)


async def resolve_git_auth(db: AsyncSession, resolved: list) -> list[dict]:
    """Resolve the git-category resolved variables into concrete delivery entries.

    ``resolved`` is the full list of ``ResolvedVariable`` from
    ``resolve_variables``; only the two git categories are consumed. Returns
    ``[{category, key, value}]`` where ``value`` is the concrete credential JSON
    (any ``vcs_connection`` source already minted to ``{username, token}``).

    Raises :class:`GitAuthRefused` when a credential resolved but policy forbids
    delivering it; the caller errors the run with the message.
    """
    out: list[dict] = []
    for v in resolved:
        if v.category not in (_HTTP, _SSH):
            continue
        try:
            cred = json.loads(v.value)
        except (ValueError, TypeError):
            logger.warning("git-auth variable has a non-JSON value; skipping", key=v.key)
            continue

        if v.category == _SSH:
            # SSH keys are static only — pass the value through verbatim.
            out.append({"category": _SSH, "key": v.key, "value": v.value})
            continue

        rewrite = cred.get("rewrite", "none")
        source = cred.get("source", "static")
        if source == "vcs_connection":
            concrete = await _mint_from_connection(
                db, cred.get("vcs_connection_id"), rewrite, key=v.key
            )
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


async def _mint_from_connection(db: AsyncSession, ref, rewrite: str, *, key: str) -> dict | None:
    """Mint a concrete ``{username, token, rewrite}`` from a VCS connection, or
    ``None`` (logged) if it can't be resolved.

    Raises :class:`GitAuthRefused` for the one case that is a decision rather
    than a failure — see :data:`_GITLAB_REFUSAL`.
    """
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
    # Checked BEFORE the try below, which turns every exception into a drop. A
    # refusal that fell into it would be indistinguishable from a mint failure
    # and the run would carry on without the credential, which is the behaviour
    # this exists to replace.
    if conn.provider == "gitlab" and not settings.vcs.gitlab.allow_token_delivery_to_runners:
        logger.warning(
            "refusing to deliver a GitLab connection token as a git credential",
            key=key,
            ref=str(ref),
        )
        raise GitAuthRefused(_GITLAB_REFUSAL.format(key=key, ref=str(ref)))
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
