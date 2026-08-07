# IaC Security Scanning (Checkov / Trivy)

Terrapod runs a **deterministic infrastructure-as-code security scan** on every
plan — [Checkov](https://www.checkov.io/) and/or
[Trivy](https://trivy.dev/) misconfiguration scanning over the *resolved plan
JSON* — and can gate applies on the result. It is the structural twin of the
[OPA policy sets](policy-sets.md) feature: same per-run result model, same
post-plan gate, same override flow — but the rules are prebuilt by the scanners
instead of operator-authored Rego, and the config is a per-workspace setting
rather than a shareable, label-scoped rule set.

Use policy sets when you want to author bespoke organizational rules in Rego;
use security scanning when you want the large, maintained Checkov/Trivy rule
catalogues (S3 public access, unencrypted volumes, open security groups, IAM
wildcards, …) with zero authoring.

## How it works

1. When a plan completes, the runner fetches the workspace's scan config, runs
   the configured engine(s) against the **plan JSON** (`tofu show -json` — so
   the scan sees *resolved* values, not raw HCL), normalises the findings, and
   computes an outcome against the severity threshold.
2. The runner POSTs the result to Terrapod. The server re-resolves the
   enforcement level and severity threshold from the workspace (it does **not**
   trust the runner's claim), records the result, and applies the gate.
3. The scan result and its findings appear on the run — engine, outcome, each
   finding's rule id / severity / resource / `file:line`, and a summary.

**Plan-fed, resolved values.** Checkov runs with `--framework terraform_plan`,
so it scans what the plan *will actually create* (post-variable, post-module),
which is more accurate than scanning raw `.tf`. Trivy config-scans the same plan
JSON (best-effort — Trivy's plan-JSON support is version-dependent).

## Enforcement modes

Each workspace picks one enforcement level (default **advisory**):

| Mode | Behaviour |
|---|---|
| `off` | The scan stage is skipped entirely. |
| `advisory` (default) | The scan runs and its findings are recorded and shown, but it **never blocks** — apply proceeds as soon as the run's other work is done (the scan runs as no-wait add-on work). |
| `enforced` | A **failed** (a finding at/above the threshold) or **errored** (the scanner crashed, or an enforced run recorded no result) scan **holds the run in `planning`** until the finding is fixed or a workspace admin overrides it. The run is *not* errored — it waits at the gate, so fixing the config and re-planning, or overriding, resolves it cleanly. |

There is **no platform-wide off switch** — scanning is a per-workspace choice.
Speculative (plan-only) runs are always recorded but never gated.

## Configuration

Set these per workspace (via the API, the `terraform_workspace` provider
resource, or `terrapod-migrate`):

| Attribute | Values | Default | Meaning |
|---|---|---|---|
| `security-scan-enforcement` | `off` \| `advisory` \| `enforced` | `advisory` | See the table above. |
| `security-scan-engine` | `checkov` \| `trivy` \| `both` | `checkov` | Which scanner(s) run. `both` = union of findings, deduped. |
| `security-scan-severity-threshold` | `critical` \| `high` \| `medium` \| `low` | `high` | The lowest finding severity that counts as a failure. |
| `security-scan-skip-rules` | list of rule ids | `[]` | Rule ids to suppress (Checkov `CKV_*` / Trivy `AVD-*`). |

### Severity and the threshold

Checkov's OSS build does **not** rate most findings with a severity (severity is
a Prisma-paid signal), so Terrapod treats an **unrated finding as `high`**. With
the default `high` threshold, every unsuppressed misconfiguration therefore
counts — a safe default. Raising the threshold to `critical` narrows the gate to
critical-rated findings (which in practice means Trivy findings and the subset of
Checkov checks that carry a severity). Lowering it to `medium`/`low` has no
additional effect on unrated Checkov findings (they're already `high`) but pulls
in lower-rated Trivy findings.

Tune noise with `security-scan-skip-rules` rather than by raising the threshold —
skipping is precise (one rule id) and auditable.

Example (provider):

```hcl
resource "terrapod_workspace" "prod" {
  name                              = "prod"
  execution_mode                    = "agent"
  security_scan_enforcement         = "enforced"
  security_scan_engine              = "both"
  security_scan_severity_threshold  = "high"
  security_scan_skip_rules          = ["CKV_AWS_18", "AVD-AWS-0107"]
}
```

## Overriding a blocking scan

When an `enforced` scan holds a run, a **workspace admin** can override it:

```
POST /api/terrapod/v1/runs/{run_id}/actions/override-security-scan
```

The run is re-driven immediately (it doesn't wait for the next reconciler tick).
The override is recorded on the scan result (`overridden-by` / `overridden-at`)
and is audit-logged. Prefer fixing the finding or adding a skip rule over
overriding — an override bypasses the gate for that one run only.

The MCP server exposes both the read and the override as tools
(`terrapod_run_security_scan`, `terrapod_run_security_scan_override`), bounded by
the caller's RBAC.

## Reading results

- **API:** `GET /api/terrapod/v1/runs/{run_id}/security-scan` returns the result
  (or `null` when the workspace has scanning off or the run wasn't scanned).
- **go-terrapod:** `Client.GetRunSecurityScan(ctx, runID)` /
  `Client.OverrideRunSecurityScan(ctx, runID)`.
- **MCP:** `terrapod_run_security_scan`.

See [`api-reference.md`](api-reference.md) for the full endpoint + attribute
reference.

## Where the engines come from

Neither engine is bundled in the runner image. Both are pulled through the
binary cache at run time, checksum-verified against what the publisher
publishes, and their versions are Helm values:

```yaml
api:
  config:
    registry:
      platform_tools:
        checkov_version: "3.3.9"
        trivy_version: "0.73.0"
```

Only the engine a run actually selects is fetched — a Checkov workspace never
downloads Trivy. The benefit over baking them in is that an upstream fix reaches
you with a `helm upgrade` rather than waiting for a Terrapod release; the cost is
a first-run download per runner (served from your own object storage after the
first fetch, so it is a local read thereafter).

**A failed fetch is recorded, not raised.** It lands as an `errored` scan result,
which for an `enforced` workspace the server turns into a block — the same
fail-closed behaviour as a crashed scanner, and the same behaviour a runner image
older than this feature gets. An out-of-date or offline runner can never silently
skip an enforced gate.

**Air-gapped / sealed installs** pre-warm both engines like any other cached
artifact; the bulk-warm endpoint and post-install warm Job include them
automatically, derived from the configured versions.
