"""The one place that knows what a run's URL looks like.

Five call sites built this string independently — `notification_service`
(as `run_ui_url`), `slack_notify_service` (as `run_url`),
`vcs_status_dispatcher` and `module_impact_service` inline, and the PR
status comment. They agreed on the shape and on the meaning of an unset
`external_url`, but only by coincidence, and the next caller had no
obvious one to copy.
"""

from __future__ import annotations


def run_url(workspace_id: object, run_id: object) -> str | None:
    """The run's page in the web UI, or None when `external_url` is unset.

    Returns None rather than "" because "no link" and "an empty link" are
    different facts, and a renderer has to be able to tell them apart to
    decide between omitting a link and emitting a broken one. Callers that
    want the empty string for their own rendering say `or ""` at the edge —
    `notification_service.run_ui_url` and `slack_notify_service.run_url`
    both do, keeping the contracts their consumers already depend on.

    **Ids are passed through, not normalised, and that is deliberate.**
    `notification_service` calls this with *prefixed* ids (`ws-<uuid>`,
    `run-<uuid>`); the others pass bare UUIDs. Both resolve, because
    `api/ids.py` registers those prefixes and `parse_id` tolerates them, so
    the two forms are equally valid links. Normalising to one form here
    would silently rewrite every URL in existing notifications — a change
    nobody asked for, arriving in people's inboxes. The inconsistency is
    real but harmless; this note exists so the next person to notice it
    fixes the right thing, or leaves it alone.
    """
    from terrapod.config import settings

    base = (settings.external_url or "").rstrip("/")
    return f"{base}/workspaces/{workspace_id}/runs/{run_id}" if base else None
