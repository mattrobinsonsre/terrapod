"""Native cost-estimation engine.

Terrapod estimates the monthly cost of a Terraform/OpenTofu *plan* (the per-run
delta) or *state* (a workspace's current managed spend) by matching each resource
against a priced product catalogue and quoting it. The catalogue is Terrapod's
own self-generated pricesheet (produced by ``pricegen`` from cloud vendor pricing
data and published weekly to a rolling GitHub Release): a self-describing YAML
document where each product carries a *match expression*
(``type=aws_db_instance&values.…``) mapping a Terraform resource to a billable
cloud line-item, plus its price. See :mod:`terrapod.services.cost.pricer` for the
quoting logic and :mod:`terrapod.services.cost.match_set` for the matching model.

The engine ships as pure Python — no third-party binary, no subprocess, no
network — so it adds no CVE surface and needs no upstream feed at run time. It is
pure and synchronous (no DB, no async); it is imported by both the runner (plan
path) and the API (state / workspace path), and callers on the API event loop
must invoke it via ``asyncio.to_thread`` — a large pricesheet parse is CPU-bound
(hard-invariant rule 13).
"""

from terrapod.services.cost.engine import CostEstimate, ResourceCost, estimate

__all__ = ["CostEstimate", "ResourceCost", "estimate"]
