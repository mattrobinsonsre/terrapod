"""VCS webhook event receiver (optional).

Only active when a webhook secret is configured. Validates the inbound request
(GitHub: HMAC-SHA256 signature; GitLab: ``X-Gitlab-Token`` secret) and enqueues
a triggered immediate poll via the distributed scheduler. The poller does all
the real work — webhooks are an accelerator, not the source of truth
(hook-and-poll model per #282). Nothing is lost without a webhook: the same
runs fire on the next poll cycle, just later.

Endpoints:
    POST /api/terrapod/v1/vcs-events/github   (GitHub webhook receiver)
    POST /api/terrapod/v1/vcs-events/gitlab   (GitLab webhook receiver, #590)
"""

import hmac
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from terrapod.config import settings
from terrapod.db.models import VCSConnection
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import gitlab_service
from terrapod.services.github_service import validate_webhook_signature
from terrapod.services.scheduler import enqueue_trigger

router = APIRouter(tags=["vcs-events"])
logger = get_logger(__name__)


async def _resolve_connection(installation_id: int) -> VCSConnection | None:
    """Look up the VCSConnection corresponding to an incoming installation id.

    Returns None if no connection matches. The receiver rejects unknown
    installations with 404 so a webhook from an installation Terrapod
    doesn't have a connection for can't enqueue spurious work (Q10 fix
    in #282).
    """
    if not installation_id:
        return None
    async with get_db_session() as db:
        result = await db.execute(
            select(VCSConnection).where(
                VCSConnection.provider == "github",
                VCSConnection.github_installation_id == installation_id,
            )
        )
        return result.scalar_one_or_none()


@router.post("/vcs-events/github")
async def github_webhook(request: Request) -> Response:
    """Receive GitHub webhook events.

    Validates HMAC-SHA256 signature when webhook_secret is configured.
    Resolves the incoming `installation.id` to a known VCSConnection and
    rejects unknown installations. Enqueues a triggered task via the
    distributed scheduler so any replica can pick it up.

    Events handled:
      - ping: handshake (acks with pong)
      - push, pull_request: immediate-poll trigger (existing behaviour)
      - issue_comment: parse for `terrapod ...` command (#282)
      - pull_request_review: re-evaluate mergeability gate (#282)
      - pull_request:closed (action): release any locks held by planned
        runs on this PR (#282, hook fast path for the poller's
        _reconcile_closed_pr_sessions)
    """
    payload = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")

    # Parse the body first so we can resolve which connection the event is for
    # (installation.id) and pick that connection's webhook secret. Parsing
    # untrusted JSON is safe; NO action is taken until the signature is
    # verified against the resolved secret below.
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    repo = body.get("repository", {})
    full_name = repo.get("full_name", "")
    installation_id = (body.get("installation") or {}).get("id", 0)
    conn = await _resolve_connection(installation_id)

    # Effective secret: the connection's own webhook secret takes precedence;
    # otherwise fall back to the global vcs.github.webhook_secret. A
    # per-connection secret means a webhook from one installation can't be
    # forged by another that only knows the global secret.
    effective_secret = (conn.webhook_secret if conn else None) or settings.vcs.github.webhook_secret
    if not effective_secret:
        raise HTTPException(status_code=404, detail="Webhooks not configured")

    # Validate signature against the effective secret BEFORE any action.
    if not validate_webhook_signature(payload, signature, effective_secret):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Handle ping event (GitHub sends this when webhook is first configured)
    if event_type == "ping":
        logger.info("GitHub webhook ping received")
        return JSONResponse(content={"message": "pong"})

    # Reject unknown installations so a webhook from an unrelated installation
    # can't enqueue work (Q10 in #282). The poll remains the source of truth —
    # this just closes the noise / abuse vector at the webhook surface.
    if conn is None:
        logger.info(
            "Webhook event for unknown installation",
            event_type=event_type,
            installation_id=installation_id,
            repo=full_name,
        )
        # 200 (not 404) so GitHub doesn't keep retrying — we'll re-
        # process organically via polling if a connection is added.
        return JSONResponse(content={"message": "unknown installation"})

    if not full_name:
        logger.debug("Webhook event without repository", event_type=event_type)
        return JSONResponse(content={"message": "ignored"})

    from terrapod.api.metrics import VCS_WEBHOOK_RECEIVED

    if event_type in ("push", "pull_request"):
        VCS_WEBHOOK_RECEIVED.labels(provider="github").inc()
        logger.info("Webhook event received", event_type=event_type, repo=full_name)
        # Enqueue an immediate poll. The poller picks up everything
        # downstream including (in apply-then-merge mode) creating
        # full-run-with-tfplan for new PR head SHAs and reconciling
        # closed PRs.
        await enqueue_trigger(
            "vcs_immediate_poll",
            {"repo": full_name, "provider": "github"},
            dedup_key=f"vcs_poll:github:{full_name}",
        )
        await enqueue_trigger(
            "module_impact_immediate_poll",
            {"repo": full_name},
            dedup_key=f"module_impact_poll:{full_name}",
        )

    elif event_type == "issue_comment":
        # GitHub fires `issue_comment` for both PR comments and plain
        # issue comments. We only want the PR variety. Filter via
        # `payload.issue.pull_request` (present only on PR comments).
        issue = body.get("issue") or {}
        if "pull_request" not in issue:
            return JSONResponse(content={"message": "ignored (not a PR comment)"})
        action = body.get("action")
        if action not in ("created", "edited"):
            return JSONResponse(content={"message": f"ignored ({action})"})
        comment = body.get("comment") or {}
        pr_number = issue.get("number")
        if not pr_number:
            return JSONResponse(content={"message": "ignored (missing PR number)"})
        VCS_WEBHOOK_RECEIVED.labels(provider="github").inc()
        # Dispatch via scheduler with dedup keyed on the comment id so a
        # webhook/poll race only runs the dispatch once.
        await enqueue_trigger(
            "vcs_comment_dispatch",
            {
                "connection_id": str(conn.id),
                "repo": full_name,
                "pr_number": pr_number,
                "comment_id": str(comment.get("id", "")),
                "actor_login": (comment.get("user") or {}).get("login", ""),
                "actor_user_id": str((comment.get("user") or {}).get("id", "")),
                "body": comment.get("body") or "",
            },
            dedup_key=f"vcs_cmd:{conn.id}:{full_name}:{pr_number}:{comment.get('id')}",
        )

    elif event_type == "pull_request_review":
        # Reviews can flip mergeability state (approved → no longer
        # blocked by "review required"). We don't auto-resume the
        # previously-blocked apply (Q3 design decision); just trigger
        # an immediate poll so the status comment can refresh.
        VCS_WEBHOOK_RECEIVED.labels(provider="github").inc()
        await enqueue_trigger(
            "vcs_immediate_poll",
            {"repo": full_name, "provider": "github"},
            dedup_key=f"vcs_poll:github:{full_name}",
        )

    elif event_type == "pull_request" and body.get("action") == "closed":
        # Already handled in the (push, pull_request) branch above —
        # this elif is unreachable because the above branch matches
        # event_type == 'pull_request' first. Left as documentation:
        # the poller's _reconcile_closed_pr_sessions handles the close
        # cleanup; the immediate poll triggered above runs it sub-second.
        pass

    return JSONResponse(content={"message": "accepted"})


# ── GitLab ────────────────────────────────────────────────────────────


def _url_host(url: str) -> str:
    """Return the lowercased host of a URL, or '' if it has none."""
    if not url:
        return ""
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


async def _resolve_gitlab_connection(project_url: str, token: str) -> VCSConnection | None:
    """Resolve the VCSConnection an incoming GitLab webhook belongs to.

    GitLab webhooks carry no installation identity (unlike GitHub), so we
    bind the event to a connection by **host** — the project's host must match
    a configured GitLab connection's ``server_url`` host (empty server_url ⇒
    gitlab.com). When several GitLab connections share one host, we
    disambiguate by which one's per-connection ``webhook_secret`` matches the
    presented token (timing-safe). Returns None when no connection matches, so
    the receiver can reject events for unconfigured projects.
    """
    host = _url_host(project_url)
    if not host:
        return None
    async with get_db_session() as db:
        result = await db.execute(select(VCSConnection).where(VCSConnection.provider == "gitlab"))
        conns = list(result.scalars().all())

    candidates = [
        c for c in conns if _url_host(c.server_url or gitlab_service.DEFAULT_GITLAB_URL) == host
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Multiple connections on the same host — disambiguate by the per-connection
    # secret that matches the token. (The final auth check still runs below.)
    for c in candidates:
        if c.webhook_secret and hmac.compare_digest(token.encode(), c.webhook_secret.encode()):
            return c
    return None


@router.post("/vcs-events/gitlab")
async def gitlab_webhook(request: Request) -> Response:
    """Receive GitLab webhook events (Push / Tag Push / Merge Request hooks).

    GitLab does not HMAC-sign the body — it sends the configured secret verbatim
    in the ``X-Gitlab-Token`` header. We resolve the connection by host, take
    that connection's ``webhook_secret`` (falling back to the global
    ``vcs.gitlab.webhook_secret``), and validate the token with a timing-safe
    compare BEFORE any action. Valid events enqueue an immediate poll; the
    poller does the real work. Unknown projects are acked without enqueuing
    (mirrors the GitHub unknown-installation behaviour).
    """
    payload = await request.body()
    event_type = request.headers.get("X-Gitlab-Event", "")
    token = request.headers.get("X-Gitlab-Token", "")

    # Parse the body first to resolve which connection the event is for. Parsing
    # untrusted JSON is safe; NO action is taken until the token is verified.
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    project = body.get("project") or {}
    path_with_namespace = project.get("path_with_namespace", "")
    project_url = (
        project.get("git_http_url") or project.get("http_url") or project.get("web_url") or ""
    )

    conn = await _resolve_gitlab_connection(project_url, token)

    # Effective secret: the connection's own webhook secret takes precedence;
    # otherwise fall back to the global vcs.gitlab.webhook_secret.
    effective_secret = (conn.webhook_secret if conn else None) or settings.vcs.gitlab.webhook_secret
    if not effective_secret:
        raise HTTPException(status_code=404, detail="Webhooks not configured")

    # Validate the token against the effective secret BEFORE any action.
    if not gitlab_service.validate_webhook_token(token, effective_secret):
        logger.warning("Invalid GitLab webhook token")
        raise HTTPException(status_code=401, detail="Invalid token")

    # Reject events for projects with no matching connection so an unrelated
    # project can't enqueue work (mirrors the GitHub Q10 fix in #282). 200 (not
    # 404) so GitLab doesn't keep retrying — polling picks it up organically if
    # a connection is later added.
    if conn is None:
        logger.info(
            "GitLab webhook for unknown project",
            event_type=event_type,
            project=path_with_namespace,
        )
        return JSONResponse(content={"message": "unknown project"})

    if not path_with_namespace:
        logger.debug("GitLab webhook event without project", event_type=event_type)
        return JSONResponse(content={"message": "ignored"})

    from terrapod.api.metrics import VCS_WEBHOOK_RECEIVED

    if event_type in ("Push Hook", "Tag Push Hook", "Merge Request Hook"):
        VCS_WEBHOOK_RECEIVED.labels(provider="gitlab").inc()
        logger.info(
            "GitLab webhook event received",
            event_type=event_type,
            repo=path_with_namespace,
        )
        # `path_with_namespace` is GitLab's "group/project" (or
        # "group/subgroup/project") — the same form the immediate-poll handler
        # exact-matches against each workspace URL via gitlab_service.parse_repo_url.
        await enqueue_trigger(
            "vcs_immediate_poll",
            {"repo": path_with_namespace, "provider": "gitlab"},
            dedup_key=f"vcs_poll:gitlab:{path_with_namespace}",
        )
        await enqueue_trigger(
            "module_impact_immediate_poll",
            {"repo": path_with_namespace},
            dedup_key=f"module_impact_poll:{path_with_namespace}",
        )

    return JSONResponse(content={"message": "accepted"})
