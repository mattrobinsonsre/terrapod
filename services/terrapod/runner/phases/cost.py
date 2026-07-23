"""Cost-estimation phase (#871) — the runner's plan-cost path.

After the plan phase exports ``tofu show -json`` (the plan JSON), this phase
estimates the plan's monthly cost *delta* natively (no third-party binary,
matching the "orchestrate, don't reimplement — but where we do, do it in pure
Python" stance):

  1. fetch the cached pricesheet index from the API's
     pull-through cache endpoint (302 → presigned storage URL), and
  2. run the native cost engine (:mod:`terrapod.services.cost`) over the plan
     JSON, resolving each resource's region independently, and
  3. write ``cost_estimate.json`` for the caller to upload as a run artifact.

The whole phase is **best-effort**: cost estimation is advisory and must never
fail a run. Every failure (disabled, pricesheet unreachable, engine raise)
returns ``None`` and the orchestrator simply skips the upload.

The API instructs whether to run this per-run (``RunnerConfig.cost_estimation``,
default yes) — the runner never self-configures cost. ``cost_default_region``
is only the fallback for a resource whose region can't be resolved from its own
attributes or provider config.

See :mod:`terrapod.services.cost` for the engine that computes the estimate.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.cost")

# Where the fetched pricesheet index and the produced estimate land. On the
# runner Job pod (unlike the API pod) /tmp is ordinary ephemeral disk. The
# pre-built SQLite index (#1034) is fetched here and queried off disk — the
# runner never parses the whole 260k-product sheet.
_PRICESHEET_DB = Path("/tmp/prices.sqlite")
_COST_ESTIMATE_JSON = Path("/tmp/cost_estimate.json")

_PRICESHEET_PATH = "/api/terrapod/v1/cost-estimation/pricesheet"


def estimate_cost(cfg: RunnerConfig, plan_json: Path) -> Path | None:
    """Estimate the plan's monthly cost from ``plan_json``.

    Returns the path to the written ``cost_estimate.json`` on success, or
    ``None`` when cost estimation is disabled for this run, the pricesheet
    can't be fetched, the plan JSON is missing/empty, or the engine raises.
    Never raises — cost is advisory.
    """
    if not cfg.cost_estimation:
        logger.info("cost estimation disabled for run — skipping")
        return None
    if not cfg.has_api:
        logger.info("no API configured — skipping cost estimation")
        return None
    if not plan_json.exists() or plan_json.stat().st_size == 0:
        logger.info("no plan JSON — skipping cost estimation")
        return None

    if not _fetch_pricesheet(cfg):
        logger.warning("pricesheet unavailable — skipping cost estimation")
        return None

    try:
        # Imported lazily so a failure to fetch the pricesheet never even
        # touches the engine, and the engine's import cost is off the hot path.
        from terrapod.services.cost import estimate
        from terrapod.services.cost.pricesheet_db import PricesheetIndex

        with plan_json.open() as fh:
            tf_json = json.load(fh)
        index = PricesheetIndex.open(str(_PRICESHEET_DB))
        try:
            result = estimate(tf_json, index=index, default_region=cfg.cost_default_region)
        finally:
            index.close()
    except Exception as exc:  # noqa: BLE001 — advisory; any failure → skip
        logger.warning("cost engine raised — skipping cost estimate", err=str(exc))
        return None

    try:
        _COST_ESTIMATE_JSON.write_text(json.dumps(result.to_dict()))
    except OSError as exc:
        logger.warning("failed to write cost estimate", err=str(exc))
        return None

    logger.info(
        "cost estimate produced",
        currency=result.currency,
        monthly_min=round(result.total_min, 2),
        monthly_max=round(result.total_max, 2),
        priced=len(result.resources),
        unpriced=len(result.unpriced),
    )
    return _COST_ESTIMATE_JSON


def _fetch_pricesheet(cfg: RunnerConfig) -> bool:
    """Download the cached pricesheet **SQLite index** to ``_PRICESHEET_DB``.

    Hits the API's pull-through cache endpoint (which builds the index upstream on
    a cold/stale cache), authenticated with the runner token; the endpoint 302s
    to a presigned storage URL that ``download_to_file`` follows.
    """
    url = f"{cfg.api_url}{_PRICESHEET_PATH}"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
    result = download_to_file(
        url,
        _PRICESHEET_DB,
        headers=headers,
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=float(cfg.download_retry_delay_seconds),
    )
    if not result.ok:
        logger.warning("pricesheet download failed", url=url, status=result.status)
        return False
    return _PRICESHEET_DB.exists() and _PRICESHEET_DB.stat().st_size > 0
