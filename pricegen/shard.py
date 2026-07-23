"""Shard selection shared by fetch_offers + publish (#1025 multi-region).

The published sheet is generated across **every** region. Doing that on one
runner is disk- and bandwidth-bound (the AmazonEC2 offer is ≈458 MB per region),
so CI fans the work out: one shard per AWS region + one AWS-global shard + one
Azure shard (all regions in one paginated pass) + one GCP shard (its catalog is
already all-region). Each shard fetches only its slice and emits a *partial*
sheet; a final ``pricegen.merge`` combines them.

``fetch_offers`` and ``publish`` MUST agree on which recipes and which regions a
given shard covers — if they diverged, a shard would generate a recipe whose
offer it never fetched (or vice-versa). So the selection lives here, used by
both. A shard is described by CLI flags:

* ``--only <provider>``   — restrict to one provider's recipes.
* ``--region <r>``        — an AWS regional shard: that region's regional recipes
                            only (``global: true`` recipes are excluded — they're
                            not region-specific and belong to the global shard).
* ``--global-only``       — only ``global: true`` recipes (the AWS-global shard).
* ``--all-regions``       — ignore the curated ``regions:`` list and use every
                            region the offer serves (AWS: every region in the
                            service index; Azure: drop the region filter; GCP is
                            natively all-region).

With no flags, both tools process every recipe across the ``regions:`` set in
``recipes.yaml`` — the local-dev / single-runner default (a curated set so a
laptop run stays quick); CI passes the flags above for full coverage.
"""

from __future__ import annotations

import argparse

# Sentinel a fetcher understands as "discover every region this offer serves".
ALL = "all"


def add_shard_args(ap: argparse.ArgumentParser) -> None:
    """Add the shard-selection flags to a fetch_offers/publish arg parser."""
    ap.add_argument("--only", help="restrict to one provider (aws|azure|gcp)")
    ap.add_argument("--region", help="AWS regional shard: this region's recipes only")
    ap.add_argument(
        "--global-only",
        action="store_true",
        help="only global-offer recipes (AWS Route53/CloudFront)",
    )
    ap.add_argument(
        "--all-regions",
        action="store_true",
        help="every region the offer serves, not the curated recipes.yaml set",
    )


def matches(cfg: dict, args: argparse.Namespace) -> bool:
    """Whether recipe ``cfg`` belongs to the shard described by ``args``."""
    if args.only and cfg["provider"] != args.only:
        return False
    fetch = cfg.get("fetch") or {}
    is_global = bool(fetch.get("global"))
    if getattr(args, "global_only", False):
        return is_global
    # An AWS regional shard (--region) covers regional recipes only; the global
    # recipes are generated once by the --global-only shard, never per region.
    if getattr(args, "region", None):
        return not is_global
    return True


def regions_for(
    cfg: dict, args: argparse.Namespace, region_sets: dict
) -> list[str] | str | None:
    """Resolve the region set for recipe ``cfg`` under shard ``args``.

    Returns a list of regions, the :data:`ALL` sentinel (fetcher discovers every
    region), or ``None`` for a ``global: true`` offer (no region dimension).
    Precedence: global offer → ``--region`` (single) → ``--all-regions`` → the
    recipe's own ``regions:``/``region:`` → the provider default in
    ``recipes.yaml``.
    """
    fetch = cfg.get("fetch") or {}
    if fetch.get("global"):
        return None
    if getattr(args, "region", None):
        return [args.region]
    if getattr(args, "all_regions", False):
        return ALL
    if "regions" in fetch:
        return fetch["regions"]
    if "region" in fetch:
        return [fetch["region"]]
    return region_sets.get(cfg["provider"], [])
