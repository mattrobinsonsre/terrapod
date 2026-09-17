"""Which vocabulary a response uses for a run stopped after its plan (#1704).

1.x reports such a run as `planning`, with `blocked-by` naming the gate. The
Terraform Enterprise vocabulary reports `post_plan_running`,
`post_plan_awaiting_decision` or `policy_override`, and exposes the run's policy
checks and task stages so the `tofu`/`terraform` CLI can show them and offer an
override.

`api.config.runs.tfe_post_plan_decisions` sets the default (it flips in 2.0.0).
A client can ask for either vocabulary per request, which is how a consumer
moves ahead of the flip -- or holds back after it -- without waiting for the
operator. The CLI cannot send a header, so it follows the default.
"""

from fastapi import Request

from terrapod.config import settings

HEADER = "X-Terrapod-Post-Plan-Decisions"
TFE = "tfe"
LEGACY = "legacy"


def reports_tfe_post_plan(request: Request | None) -> bool:
    """True when this response should use the Terraform Enterprise vocabulary."""
    if request is not None:
        asked = request.headers.get(HEADER, "").strip().lower()
        if asked == TFE:
            return True
        if asked == LEGACY:
            return False
    return settings.runs.tfe_post_plan_decisions
