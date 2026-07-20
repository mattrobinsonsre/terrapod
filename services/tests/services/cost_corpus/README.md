# Cost differential-oracle corpus

`terraform show -json` plan/state fixtures fed to **both** the native cost
engine (`terrapod.services.cost`) and the real `oiq` binary by
[`../test_cost_differential.py`](../test_cost_differential.py). The oracle
asserts the two agree bit-exact on the same pricesheet — the correctness
backstop for the ported engine.

## Hard invariant: every fixture is single-region

The differential prices the whole corpus against **one** region (the
`OIQ_REGION` env, default `us-east-1`). oiq prices a plan against a per-plan
`--region` flag; Terrapod's engine resolves region **per resource** (a
deliberate divergence — a plan can span regions, #871). To keep the oracle an
apples-to-apples test of the shared matcher + pricer (not of Terrapod's
region-resolution extension, which is unit-tested separately in
`../test_cost_engine.py`):

- **No fixture carries a per-resource region attribute** (`values.region`,
  `values.location`) or a provider-config region. Region comes solely from the
  flag/default, so both engines price identically.
- A fixture that needs a non-default region prices the *whole* file in that
  region; the test runs the corpus at one region per invocation.

## What the corpus exercises

| Fixture | Covers |
|---|---|
| `state_ec2_rds_ebs.json` | A multi-service **state** (current cost): EC2 (`Per_time`), RDS (`Per_time`), EBS gp3 (`Per_data` storage). Every resource priced, `diff` zero. |
| `plan_add_remove_noop.json` | A **plan** with an add, a remove, and a noop — pins the `price`/`prev_price`/`price_diff` (total/previous/delta) semantics. |
| `state_s3_lambda_iam.json` | Usage-driven pricing (S3, Lambda) **plus** an unpriced resource (`aws_iam_role`) that nothing in the pricesheet matches — exercises the unpriced bucket. |

## Regenerating / adding a fixture

1. Produce a `terraform show -json` of a plan or state (or hand-author one in
   the shape above), scrubbing any real account IDs / names. Keep it
   single-region (no region attrs).
2. **Verify it agrees before committing** — run the fixture through both
   engines on a real pricesheet and confirm the totals match. Only fixtures
   that already agree belong here; a mismatch is a *native-engine bug to fix*,
   never a fixture to tweak until it passes.
3. The oracle picks up any `*.json` in this directory automatically.
