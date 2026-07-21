# pricegen — self-generated cloud pricing data (#893)

Terrapod's cost engine ([`services/terrapod/services/cost/`](../services/terrapod/services/cost/))
is a native, MPL-2.0 reimplementation of [OpenInfraQuote](https://github.com/terrateamio/openinfraquote)'s
(oiq, by Terrateam) matcher + pricer. We ship no binary and shell out to nothing
— but until now we **consumed oiq's published `prices.csv`** (`cost_estimation.prices_url`)
verbatim. That is an undocumented data dependency on a free feed with no stated
terms, SLA, or coverage guarantee, and it inherits oiq's coverage (AWS only).

`pricegen` removes that dependency: it **generates the price sheet ourselves,
directly from the official cloud pricing APIs**, and opens the door to Azure/GCP
(which oiq doesn't cover).

## Hard constraint

Pricing data comes **only** from first-class official cloud-provider pricing
APIs — AWS Price List Bulk API, Azure Retail Prices API, GCP Cloud Billing
Catalog. **No third-party pricing product, dataset, API, or code is in scope —
Infracost is excluded in every form (including its OSS pricing-api).** The
SKU→resource mapping is Terrapod's own original work (the recipe YAML).

## Architecture (provider-agnostic)

The three clouds' feeds are structurally unrelated, so a per-cloud **adapter**
normalizes each into one shape and a **shared engine** does everything else.
Adding a cloud = an adapter + recipes; the engine never changes.

```
pricegen/
  models.py              # Unit(family, attrs, prices[])  — the normalized shape
  engine.py              # recipes × Units → rows   (regex-capable, tier-aware)
  generate.py            # CLI: pick provider, run engine, (AWS) row-parity vs oiq
  providers/
    aws/   { adapter.py, defaults.yaml, recipes/*.yaml }
    azure/ { adapter.py, defaults.yaml, recipes/*.yaml }   # VM proven
    gcp/   { adapter.py, defaults.yaml, recipes/*.yaml }   # compute proven
```

- **Adapter** — normalizes the vendor feed into `Unit`s. AWS joins `products`+`terms`;
  Azure iterates its flat `Items[]`; GCP expands `skus`→`tieredRates`.
- **`defaults.yaml`** (per provider) — the shared "pick the canonical SKU variant"
  knowledge (AWS: `OnDemand`/`Shared`/`Used`/`NA`/`No-License`), the pricing
  dimensions on every row, and the default price term. Applied to a unit only
  where it *has* that attribute, so a resource lacking a dimension skips it.
- **Recipe** (per resource type) — thin. A resource is a list of **components**
  (a cloud resource often has several priced parts — an instance + its storage +
  its IOPS). Each component maps one product family (by **regex**) to one row
  shape: the `match` attributes → `match_set`, the `service_class`, the charge
  `type`, and the `tier` mode.

### Two hard-won mechanisms

- **Regex field resolution.** The value we need often lives *inside a string*:
  Azure's OS/tier in `productName`/`skuName`, GCP's machine family in the sku
  `description`. So `match`/`pricing`/`select` support `{attr: X, regex: '(cap)'}`
  extraction and `{regex: 'pat', negate: bool}` filters, not just direct lookup.
- **The exceptions layer (`tier_bounds`).** Some pricing facts aren't structured
  in the vendor API — EBS io2's 32000/64000 tier breaks and gp3's free
  3000-IOPS baseline are *domain knowledge* every SKU reports as `0-Inf`. These
  live in a small, stable per-resource `tier_bounds` table — exactly the
  "exceptions layered on the pattern" model.

## Go/no-go findings (AWS Phase 0 — validated)

Row-parity against oiq's own `prices.csv` (order-independent) for `us-east-1`:

- **`aws_instance`: 1362 rows byte-match oiq.** Every divergence is explained —
  we carry the *newest* instance families oiq's snapshot lacks (**more current**),
  and where a shared row differs, **oiq's price is stale** (e.g. h1 @ oiq's $4.40
  vs the current $3.744).
- **`aws_ebs_volume` (composite: storage + IOPS): reproduces 10/10 of oiq's rows.**
  Our two extra rows are io2's storage + base-IOPS tier, which **oiq omits
  entirely** — we're also **more complete**.

Bottom line: **prices/rates are 100% in the AWS API; the mapping is thin recipes;
a small, stable exceptions layer covers the tier facts the API doesn't structure.**
Mirroring AWS ourselves is low-risk, and the result is *more* accurate than the
feed we depend on today. Azure is a direct map + regex extraction; GCP is
**computed** (an instance = Σ vCPU-core + RAM-GB skus × a machine-type catalog)
and needs a free read-only API key.

### Not a pricegen concern: derived-cost resources

Resources whose cost is *inferred from other resources* — autoscaling groups,
EKS node groups, Fargate services, spot fleets — get **no sheet row**. Deriving
their cost (ASG → launch template → instance_type × capacity) is an **engine**
job over the plan's resource graph, layered on the unit prices this generates.
Today they land in the `unpriced` bucket the AI-estimate layer (#871) already
covers; a future deterministic resolver is a `services/…/cost` enhancement.

## Running it

Requires `pyyaml`. Offer files are large (one EC2 region ≈ 458 MB) — cached
under `.cache/` (gitignored).

```sh
# fetch one AWS region offer (public, no credentials)
curl -s "https://pricing.us-east-1.amazonaws.com$(curl -s \
  https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/region_index.json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['regions']['us-east-1']['currentVersionUrl'])")" \
  -o pricegen/.cache/ec2-us-east-1.json

# generate + row-parity-check against oiq (from the repo root)
python3 -m pricegen.generate --provider aws --recipe aws_instance \
  --offer pricegen/.cache/ec2-us-east-1.json --compare-oiq us-east-1

# write a sheet (CSV interim; gzipped YAML is the published format)
python3 -m pricegen.generate --provider aws --recipe aws_ebs_volume \
  --offer pricegen/.cache/ec2-us-east-1.json --out /tmp/ebs.csv
```

## Output + publishing

CSV is interim — only to row-parity-diff against oiq. The **published** format is
gzipped, normalized YAML (`prices.yaml.gz`), produced by a scheduled CI generator
and pushed to a **rolling GitHub Release** (asset clobbered each run) — the same
GitHub-hosted, no-extra-infra story as the container images and Helm chart.
`cost_estimation.prices_url` then defaults to that stable URL; air-gapped
operators mirror it as today. Weekly regeneration is the target cadence.

## Status

- [x] Provider-agnostic architecture (adapter → Unit → shared engine)
- [x] AWS adapter + `aws_instance` (parity) + `aws_ebs_volume` (composite, exceptions)
- [x] AWS RDS (`aws_db_instance`): instance + storage + io1/io2 provisioned IOPS, all validated end-to-end
- [x] End-to-end pricing validation (real consumer engine, not just row-parity) + generator drift diagnostics
- [x] Azure adapter + `azurerm_linux_virtual_machine` (regex select, validated end-to-end) — second cloud proven
- [x] GCP computed engine + `google_compute_instance` (Σ vCPU-core + GiB-RAM, formulaic catalog, zone→region, validated end-to-end) — **all three clouds proven**
- [x] Publish pipeline: `fetch_offers` (official APIs) → `publish` (combined gzipped-YAML sheet + drift manifest) → `drift` guardrail → scheduled workflow to a rolling GitHub Release
- [ ] Consumer reads the Terrapod YAML sheet + flip `cost_estimation.prices_url` default to the rolling release
- [ ] AWS breadth (LB, NAT, EIP, S3, …) + all-regions + a row-parity CI gate
- [ ] Broaden GCP families/regions; Azure managed disks / Windows
