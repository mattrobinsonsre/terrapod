"""VCS connection CRUD endpoints.

VCS connections are platform-level resources that configure auth for a VCS
provider (GitHub App installation, GitLab access token). Workspaces reference
a connection to link to a repository.

UX CONTRACT: VCS connection endpoints are consumed by the web frontend:
  - web/src/app/admin/vcs-connections/page.tsx (connection CRUD)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    GET    /api/terrapod/v1/vcs-connections   (list connections)
    POST   /api/terrapod/v1/vcs-connections   (create connection)
    GET    /api/terrapod/v1/vcs-connections/{id}                  (show connection)
    DELETE /api/terrapod/v1/vcs-connections/{id}                  (delete connection)
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.ids import parse_id
from terrapod.api.pagination import paginate
from terrapod.db.models import VCSConnection, generate_uuid7
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import vcs_rate_limit

router = APIRouter(tags=["vcs-connections"])
logger = get_logger(__name__)

SUPPORTED_PROVIDERS = {"github", "gitlab"}


def _rfc3339(dt) -> str:
    if dt is None:
        return ""

    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connection_json(
    conn: VCSConnection, *, quota: object | None, consumption: object | None
) -> dict:
    """Serialize a VCSConnection to JSON:API format.

    `quota` is the last rate-limit observation for this connection (#1334), or
    None when nothing is known — either because no call has been made yet or
    because the server does not report rate limits at all. It is keyword-only
    and has no default so each caller says which it means; a serializer that
    silently omitted the budget would be indistinguishable from a connection
    with no budget left.

    `consumption` is required for the same reason and had a default by
    oversight: omitting it returned `saturation: null` on a 200, so the UI
    rendered "Not reported" for a connection that might be exhausted. The
    argument the paragraph above makes applies to it exactly.
    """
    attrs: dict = {
        "name": conn.name,
        "provider": conn.provider,
        "server-url": conn.server_url,
        "status": conn.status,
        "has-token": conn.token is not None and conn.token != "",
        "created-at": _rfc3339(conn.created_at),
        "updated-at": _rfc3339(conn.updated_at),
    }

    # Include GitHub-specific fields when relevant
    if conn.provider == "github":
        attrs["github-app-id"] = conn.github_app_id
        attrs["github-installation-id"] = conn.github_installation_id
        attrs["github-account-login"] = conn.github_account_login
        attrs["github-account-type"] = conn.github_account_type
        # Write-only: surface only whether a per-connection webhook secret is
        # set, never the value (same pattern as has-token).
        attrs["has-webhook-secret"] = bool(conn.webhook_secret)

    # Rate-limit budget (#1334). Always present as keys so a consumer can tell
    # "not reported" (null) from "nothing left" (0) — the distinction that
    # makes the indicator trustworthy. Read from headers the provider returned
    # on calls we were making anyway, so these are an observation as of
    # `rate-limit-observed-at`, not a live reading; the timestamp ships with
    # them precisely so the UI can say so.
    attrs["rate-limit"] = getattr(quota, "limit", None)
    attrs["rate-limit-remaining"] = getattr(quota, "remaining", None)
    attrs["rate-limit-resource"] = getattr(quota, "resource", None)
    attrs["rate-limit-reset-at"] = (
        _rfc3339(datetime.fromtimestamp(quota.reset_at, tz=UTC)) if quota is not None else None
    )
    attrs["rate-limit-observed-at"] = (
        _rfc3339(datetime.fromtimestamp(quota.observed_at, tz=UTC)) if quota is not None else None
    )

    # Consumption (#1339). The budget level above says how much is left at an
    # instant, which cannot tell you whether the configuration is straining the
    # limit — the budget refills on a window, so it reads healthy right after a
    # reset however fast it is being spent. These say how fast, how long until
    # the refill, whether that combination lands badly, and which repo or
    # workspace is responsible.
    attrs["calls-per-hour"] = getattr(consumption, "calls_per_hour", None)
    attrs["rate-window-minutes"] = getattr(consumption, "window_minutes", None)
    attrs["seconds-to-reset"] = getattr(consumption, "seconds_to_reset", None)
    attrs["saturation"] = getattr(consumption, "verdict", None)
    attrs["exhausts-in-seconds"] = getattr(consumption, "exhausts_in_seconds", None)
    attrs["top-consumers"] = getattr(consumption, "top_consumers", None) or []
    # Labels, because the two remedies for an over-budget connection are "poll
    # less often" and "split the load across more connections", and splitting
    # needs a line to split along. Terrapod has no teams — labels are how an
    # estate divides — so this is what turns a list of repos into a decision.
    attrs["label-totals"] = getattr(consumption, "label_totals", None) or []
    # The window the provider's budget refills on, so a share can be computed
    # on the same basis as the allowance. NOT assumed hourly: GitHub refills
    # 5,000/hour, GitLab.com 2,000/minute, and comparing an hourly call count
    # against a per-minute allowance overstates utilisation by ~60x.
    attrs["budget-window-seconds"] = getattr(consumption, "budget_window_seconds", None)
    # Total across ALL consumers over the same window the breakdown covers.
    # Each entry's share divides by this, not by `calls-per-hour` — those were
    # different bases, which is how shares came to exceed 100%.
    attrs["consumers-window-total"] = getattr(consumption, "consumers_window_total", None)

    # GHSA-v8g7-pqrj-8mcm. Who may point a workspace at this connection, and
    # where it may be pointed. Serialised so the provider and the admin UI can
    # manage them; none of the three is a secret — the credential is, and that is
    # still write-only.
    attrs["owner-email"] = conn.owner_email or ""
    attrs["labels"] = conn.labels or {}
    attrs["allowed-repositories"] = list(conn.allowed_repositories or [])

    return {
        "id": f"vcs-{conn.id}",
        "type": "vcs-connections",
        "attributes": attrs,
        "relationships": {
            "organization": {
                "data": {"id": "default", "type": "organizations"},
            },
        },
    }


def _rbac_attrs(attrs: dict, supplied: set[str] | None = None) -> tuple[str, dict, list]:
    """Parse and validate `owner-email`, `labels` and `allowed-repositories`.

    GHSA-v8g7-pqrj-8mcm. Shared by create and update so the two cannot drift —
    they have drifted before, and the shape of that bug is a reserved label
    accepted on create and then rejected on every subsequent edit, leaving the
    entity uneditable.

    **Validate, never transform a value a Terraform provider sends.** All three of
    these are provider-managed attributes (`terrapod_vcs_connection`), and a provider
    writes the server's response back into state. So any value the server *changes*
    on the way in — folding case, stripping whitespace, truncating, reordering,
    deduplicating — makes the plan disagree with the result and fails the apply with
    "Provider produced inconsistent result after apply". The resource then cannot be
    managed as code at all, which for a security control means it does not get
    adjusted. Refuse what is unacceptable with a 422 and store the rest byte for
    byte; where tolerance is wanted, put it on the **read** side, where it costs
    nothing. Both transforms removed here had a read-side equivalent already.

    `supplied` names the keys the CALLER actually sent; `None` means all of them
    (the create path). The partial-update path passes it, because the merged dict it
    builds carries **stored** values for the keys the caller omitted — and refusing a
    stored value makes the connection uneditable: a PATCH of `labels` alone would
    answer 422 about an `allowed-repositories` entry the caller never mentioned. That
    is the #316 shape exactly, accepted by one path and refused by every later edit
    until the entity is stuck. Validation therefore applies only to what was sent, so
    a row written by hand or by a migration can always be edited back into shape.
    """
    # The HTTP-translating wrapper, not the raw service function. The raw one raises
    # LabelValidationError (a ValueError), which has no handler, so a reserved label
    # key on a connection answered 500 "Internal server error" instead of telling the
    # operator which key is reserved. Every other labelled entity uses this wrapper.
    from terrapod.api.labels import validate_labels

    def sent(key: str) -> bool:
        return supplied is None or key in supplied

    # **Validated, never transformed** — see this function's own rule below. An
    # earlier version lower-cased and truncated here, reasoning that
    # `may_reference_connection` compares for equality so `Owner@Example.com` would
    # create a grant that never matches `owner@example.com`. The premise was wrong:
    # that comparison folds case on BOTH sides precisely so a row written before this
    # release, or by hand, still matches. So the fold bought nothing and cost the
    # provider: `terraform apply` with `owner_email = "Owner@Example.com"` planned one
    # value, read back another, and failed with "Provider produced inconsistent result
    # after apply" — a resource that cannot be managed as code at all.
    raw_owner = attrs.get("owner-email")
    if raw_owner is None:
        raw_owner = ""
    if sent("owner-email") and not isinstance(raw_owner, str):
        # Without this, `.strip()` raised AttributeError and the operator got a 500
        # with no indication of which attribute was wrong. Every sibling attribute
        # here answers 422; this one answered "Internal server error".
        raise HTTPException(status_code=422, detail="owner-email must be a string")
    owner_email = raw_owner if isinstance(raw_owner, str) else str(raw_owner)
    if sent("owner-email") and len(owner_email) > 255:
        # The column is String(255). Truncating silently stored a DIFFERENT owner
        # than the caller asked for — and an owner grant is a security control, so
        # a quietly altered one is worse than a refused one.
        raise HTTPException(
            status_code=422,
            detail=f"owner-email must be at most 255 characters (got {len(owner_email)})",
        )

    labels = attrs.get("labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise HTTPException(status_code=422, detail="labels must be an object")
    if sent("labels"):
        validate_labels(labels)

    repos = attrs.get("allowed-repositories")
    if repos is None:
        repos = []
    if sent("allowed-repositories") and (
        not isinstance(repos, list) or not all(isinstance(r, str) for r in repos)
    ):
        raise HTTPException(
            status_code=422, detail="allowed-repositories must be a list of strings"
        )
    # Blank entries are dropped, because one would match nothing while looking like
    # a restriction — but the entries that remain are stored **exactly as sent**.
    # Stripping each one was the same provider-breaking transform as the fold above
    # and bought just as little: `repository_allowed` strips every pattern before
    # matching, so ` myorg/* ` already works.
    cleaned = [r for r in repos if isinstance(r, str) and r.strip()]
    # But dropping them must not turn a narrowing into a widening. An empty list
    # means ANY repository, so `["  "]` silently became "allow everything" — a
    # fat-fingered pattern answered 200 and left the connection WIDER than before,
    # which is the one direction a validation error is cheaper than. A caller who
    # meant "any" sends `[]` and gets it; a caller whose patterns all vanished gets
    # told.
    if sent("allowed-repositories") and repos and not cleaned:
        raise HTTPException(
            status_code=422,
            detail=(
                "allowed-repositories contained only blank entries. Send an empty "
                "list to allow any repository the connection's credential can "
                "reach; a list of blanks would do that silently."
            ),
        )
    return owner_email, labels, cleaned


async def _list_connections(db: AsyncSession) -> list[VCSConnection]:
    result = await db.execute(select(VCSConnection).order_by(VCSConnection.created_at))
    return list(result.scalars().all())


async def _get_connection(db: AsyncSession, connection_id: uuid.UUID) -> VCSConnection | None:
    result = await db.execute(select(VCSConnection).where(VCSConnection.id == connection_id))
    return result.scalar_one_or_none()


@router.get("/vcs-connections")
async def list_connections(
    request: Request = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all VCS connections (admin only)."""
    connections = await _list_connections(db)
    quotas = {c.id: await vcs_rate_limit.get_snapshot(c.id) for c in connections}
    usage = {
        c.id: await vcs_rate_limit.get_consumption(c.id, quotas.get(c.id)) for c in connections
    }
    items = [
        _connection_json(c, quota=quotas.get(c.id), consumption=usage.get(c.id))
        for c in connections
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/vcs-connections", status_code=201)
async def create_connection(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a VCS connection (admin only).

    For GitHub: provide github-app-id, github-installation-id, and private-key
    (the PEM-encoded GitHub App private key). Optionally server-url for GHE.
    For GitLab: provide token and optionally server-url (defaults to gitlab.com).
    """

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "")
    provider = attrs.get("provider", "github")

    if not name:
        raise HTTPException(status_code=422, detail="Connection name is required")
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported provider '{provider}'. Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}",
        )

    # Provider-specific validation
    token_value = None

    if provider == "github":
        app_id = int(attrs.get("github-app-id", 0))
        installation_id = int(attrs.get("github-installation-id", 0))
        private_key = attrs.get("private-key", "")
        if not app_id:
            raise HTTPException(
                status_code=422, detail="github-app-id is required for GitHub connections"
            )
        if not installation_id:
            raise HTTPException(
                status_code=422, detail="github-installation-id is required for GitHub connections"
            )
        if not private_key:
            raise HTTPException(
                status_code=422, detail="private-key is required for GitHub connections"
            )
        token_value = private_key
        # Check for duplicate GitHub installation
        existing = await db.execute(
            select(VCSConnection).where(
                VCSConnection.provider == "github",
                VCSConnection.github_installation_id == installation_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=422,
                detail=f"GitHub installation {installation_id} is already connected",
            )

    elif provider == "gitlab":
        token = attrs.get("token", "")
        if not token:
            raise HTTPException(status_code=422, detail="token is required for GitLab connections")
        token_value = token

    # Optional per-connection webhook secret (GitHub HMAC secret or GitLab
    # X-Gitlab-Token, #590). Write-only. Trimmed to match the PATCH path so a
    # whitespace-only value is treated as unset rather than stored verbatim.
    webhook_secret = (attrs.get("webhook-secret") or "").strip()

    # GHSA-v8g7-pqrj-8mcm. Labels go through the same chokepoint every labelled
    # entity uses, at CREATE as well as update: a create path that skips it lets a
    # reserved key in, and the update path's re-validation then traps the entity so
    # it cannot be edited at all (#316).
    owner_email, conn_labels, allowed_repos = _rbac_attrs(attrs)

    conn = VCSConnection(
        id=generate_uuid7(),
        provider=provider,
        name=name,
        server_url=attrs.get("server-url", ""),
        token=token_value,
        owner_email=owner_email,
        labels=conn_labels,
        allowed_repositories=allowed_repos,
        # GitHub-specific
        github_app_id=int(attrs.get("github-app-id", 0)),
        github_installation_id=int(attrs.get("github-installation-id", 0)),
        github_account_login=attrs.get("github-account-login", ""),
        github_account_type=attrs.get("github-account-type", ""),
        webhook_secret=webhook_secret or None,
        status="active",
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    logger.info(
        "VCS connection created",
        connection_id=str(conn.id),
        name=name,
        provider=provider,
    )

    return JSONResponse(
        content={
            "data": _connection_json(
                conn,
                quota=(q := await vcs_rate_limit.get_snapshot(conn.id)),
                consumption=await vcs_rate_limit.get_consumption(conn.id, q),
            )
        },
        status_code=201,
    )


@router.get("/vcs-connections/{connection_id}")
async def show_connection(
    connection_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a VCS connection (admin only)."""
    conn_uuid = parse_id(connection_id, "vcs-", detail="VCS connection not found")
    conn = await _get_connection(db, conn_uuid)
    if conn is None:
        raise HTTPException(status_code=404, detail="VCS connection not found")
    return JSONResponse(
        content={
            "data": _connection_json(
                conn,
                quota=(q := await vcs_rate_limit.get_snapshot(conn.id)),
                consumption=await vcs_rate_limit.get_consumption(conn.id, q),
            )
        }
    )


@router.patch("/vcs-connections/{connection_id}")
async def update_connection(
    connection_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update a VCS connection (admin only).

    Partial update — only attributes present in the request are
    changed. `provider` is immutable (a different provider is a
    different connection; delete + recreate instead). Credentials are
    write-only: pass `private-key` (GitHub) or `token` (GitLab) to
    rotate; omit them to leave the stored credential untouched. Editable
    fields: name, server-url, status, the GitHub App identifiers, and the
    three reach-and-scope attributes `owner-email`, `labels` and
    `allowed-repositories` (GHSA-v8g7-pqrj-8mcm).

    On those three, an absent key leaves the field alone and an explicitly empty
    value clears it — `allowed-repositories: []` is how a connection is widened back
    to any repository its credential can reach, so an allowlist that could not be
    cleared by removing its last entry would be a one-way door. A list whose entries
    are all blank is refused rather than treated as empty, because dropping blanks
    would otherwise turn a typo into a silent widening.
    """
    conn_uuid = parse_id(connection_id, "vcs-", detail="VCS connection not found")
    conn = await _get_connection(db, conn_uuid)
    if conn is None:
        raise HTTPException(status_code=404, detail="VCS connection not found")

    attrs = body.get("data", {}).get("attributes", {})

    if "provider" in attrs and attrs["provider"] != conn.provider:
        raise HTTPException(
            status_code=422,
            detail="provider is immutable — delete and recreate to change it",
        )

    if "name" in attrs:
        new_name = (attrs.get("name") or "").strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="Connection name cannot be empty")
        conn.name = new_name
    if "server-url" in attrs:
        conn.server_url = attrs.get("server-url") or ""
    if "status" in attrs:
        status = attrs.get("status") or ""
        if status not in ("active", "disabled"):
            raise HTTPException(status_code=422, detail="status must be 'active' or 'disabled'")
        conn.status = status

    # GHSA-v8g7-pqrj-8mcm. Partial update: each is applied only when its key is
    # PRESENT, so omitting one leaves it alone, while an explicitly empty value
    # clears it. Sending `allowed-repositories: []` has to mean "allow any
    # repository again" — an allowlist that cannot be cleared by deleting its last
    # entry is a trap, which is the same reasoning as the policy-set scope in
    # #1765.
    if any(k in attrs for k in ("owner-email", "labels", "allowed-repositories")):
        owner_email, conn_labels, allowed_repos = _rbac_attrs(
            {
                "owner-email": attrs.get("owner-email", conn.owner_email),
                "labels": attrs.get("labels", conn.labels),
                "allowed-repositories": attrs.get(
                    "allowed-repositories", conn.allowed_repositories
                ),
            },
            supplied={k for k in ("owner-email", "labels", "allowed-repositories") if k in attrs},
        )
        if "owner-email" in attrs:
            conn.owner_email = owner_email
        if "labels" in attrs:
            conn.labels = conn_labels
        if "allowed-repositories" in attrs:
            conn.allowed_repositories = allowed_repos

    if conn.provider == "github":
        if "github-app-id" in attrs:
            conn.github_app_id = int(attrs.get("github-app-id") or 0)
        if "github-account-login" in attrs:
            conn.github_account_login = attrs.get("github-account-login") or ""
        if "github-account-type" in attrs:
            conn.github_account_type = attrs.get("github-account-type") or ""
        if "github-installation-id" in attrs:
            new_install = int(attrs.get("github-installation-id") or 0)
            if new_install != conn.github_installation_id:
                dup = await db.execute(
                    select(VCSConnection).where(
                        VCSConnection.provider == "github",
                        VCSConnection.github_installation_id == new_install,
                        VCSConnection.id != conn.id,
                    )
                )
                if dup.scalar_one_or_none():
                    raise HTTPException(
                        status_code=422,
                        detail=f"GitHub installation {new_install} is already connected",
                    )
                conn.github_installation_id = new_install
        # Credential rotation: only when a non-empty key is supplied.
        new_key = attrs.get("private-key") or ""
        if new_key:
            conn.token = new_key
        # Webhook-secret rotation (write-only). Supply a non-empty value to
        # set/rotate; pass an explicit empty string to clear it (fall back to
        # the global secret); omit the key entirely to leave it untouched.
        if "webhook-secret" in attrs:
            conn.webhook_secret = (attrs.get("webhook-secret") or "").strip() or None
    elif conn.provider == "gitlab":
        new_token = attrs.get("token") or ""
        if new_token:
            conn.token = new_token

    await db.commit()
    await db.refresh(conn)

    logger.info(
        "VCS connection updated",
        connection_id=str(conn.id),
        name=conn.name,
        provider=conn.provider,
    )

    return JSONResponse(
        content={
            "data": _connection_json(
                conn,
                quota=(q := await vcs_rate_limit.get_snapshot(conn.id)),
                consumption=await vcs_rate_limit.get_consumption(conn.id, q),
            )
        }
    )


@router.delete("/vcs-connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a VCS connection (admin only)."""
    conn_uuid = parse_id(connection_id, "vcs-", detail="VCS connection not found")
    conn = await _get_connection(db, conn_uuid)
    if conn is None:
        raise HTTPException(status_code=404, detail="VCS connection not found")
    await db.delete(conn)
    await db.commit()
    logger.info("VCS connection deleted", connection_id=str(conn.id))
