"""Native cost-estimation engine — an OpenInfraQuote-compatible port.

Terrapod estimates the monthly cost of a Terraform/OpenTofu *plan* (the per-run
delta) or *state* (a workspace's current managed spend) by matching each
resource against a priced product catalogue and quoting it. The catalogue is
OpenInfraQuote's published pricing sheet (``prices.csv``) — a ~200k-row CSV
where each row carries a *match expression* (``type=aws_db_instance&values.…``)
mapping a Terraform resource to a billable cloud line-item, plus its price.

The real, ongoing work — generating and daily-refreshing that pricesheet across
every cloud SKU — is done by **OpenInfraQuote** (by Terrateam, with LocalStack);
Terrapod consumes their CSV verbatim and depends on it entirely. This module is
a faithful, native-Python reimplementation of the small *reader* engine (the
``oiq`` binary's matcher + pricer), so Terrapod ships no third-party binary,
shells out to no subprocess, and carries no extra CVE surface — while still
crediting and relying on OpenInfraQuote for the data and the engine design.

Provenance / licence
--------------------
This package is an MPL-2.0 *derivative work* of OpenInfraQuote
(https://github.com/terrateamio/openinfraquote, MPL-2.0). The module layout and
pricing semantics mirror its OCaml source (``oiq_match_set``, ``oiq_match_query``,
``oiq_prices``, ``oiq_tf``, ``oiq_usage``, ``oiq_range``, ``oiq_pricer``).
Terrapod is MPL-2.0, so the derivation is licence-clean. Cost estimates
surfaced to users must credit OpenInfraQuote.

Correctness is pinned in CI by a *differential test* that runs the real ``oiq``
binary against a plan corpus and asserts this engine agrees — the binary lives
only in CI as an oracle, never in a shipped image.

The engine is pure, synchronous Python (no DB, no network, no async). It is
imported by both the runner (plan path) and the API (state / workspace path);
callers on the API event loop must invoke it via ``asyncio.to_thread`` (a large
pricesheet parse is CPU-bound — hard-invariant rule 13).
"""

from terrapod.services.cost.engine import CostEstimate, ResourceCost, estimate

__all__ = ["CostEstimate", "ResourceCost", "estimate"]
