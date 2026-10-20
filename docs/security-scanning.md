# IaC Security Scanning (Checkov / Trivy)

Terrapod runs a **deterministic infrastructure-as-code security scan** on every
plan — [Checkov](https://www.checkov.io/) and/or
[Trivy](https://trivy.dev/) misconfiguration scanning over the *resolved plan
JSON* — and can gate applies on the result. It is the structural twin of the
[OPA policy sets](policies.md) feature: same per-run result model, same
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

**Pulumi workspaces are not scanned yet.** Checkov and Trivy read Terraform plan
JSON, and a Pulumi run produces none (#1569). A Pulumi workspace defaults to
`off` and refuses `advisory` or `enforced` with `422`. A Pulumi workspace that
already carries another value (from before this check, or restored from a
deleted workspace) is treated as `off`, so no apply waits for a scan that never
runs. The run's `GET /api/v1/runs/{run_id}/security-scan` says so in
`meta.not-evaluated-reason`.

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
POST /api/v1/runs/{run_id}/actions/override-security-scan
```

The run is re-driven immediately (it doesn't wait for the next reconciler tick).
The override is recorded on the scan result (`overridden-by` / `overridden-at`)
and is audit-logged. Prefer fixing the finding or adding a skip rule over
overriding — an override bypasses the gate for that one run only.

The MCP server exposes both the read and the override as tools
(`terrapod_run_security_scan`, `terrapod_run_security_scan_override`), bounded by
the caller's RBAC.

The scan is also served to the `tofu`/`terraform` CLI as a policy check, so
`tofu apply` can show the findings and offer the override — see
[post-plan-decisions.md](post-plan-decisions.md).

## Reading results

- **API:** `GET /api/v1/runs/{run_id}/security-scan` returns the result
  (or `null` when the workspace has scanning off or the run wasn't scanned).
- **go-terrapod:** `Client.GetRunSecurityScan(ctx, runID)` /
  `Client.OverrideRunSecurityScan(ctx, runID)`.
- **MCP:** `terrapod_run_security_scan`.

See [`api-reference.md`](api-reference.md) for the full endpoint + attribute
reference.

## Where the engines come from

Neither engine is bundled in the runner image. Both are pulled through the
binary cache at run time, checksum-verified before they are stored, and their
versions are Helm values:

```yaml
api:
  config:
    registry:
      platform_tools:
        checkov_version: "3.3.29"
        trivy_version: "0.76.0"
```

Only the engine a run actually selects is fetched — a Checkov workspace never
downloads Trivy. The benefit over baking them in is that a fix reaches you with a
`helm upgrade` rather than waiting for a Terrapod release; the cost is
a first-run download per runner (served from your own object storage after the
first fetch, so it is a local read thereafter).

### Terrapod builds the Checkov binaries

Trivy and OPA are fetched from their publishers' releases. **Checkov is fetched
from Terrapod's**, and that is deliberate.

Checkov stopped publishing `checkov_linux_arm64.zip` at **3.3.21** — its arm
build job waits on a self-hosted runner pool that does not have capacity
([`bridgecrewio/checkov#7729`](https://github.com/bridgecrewio/checkov/issues/7729))
— so every release from 3.3.21 to 3.3.28 is x86\_64 only. On an **arm64 agent
pool** that made every scan fail to fetch its engine and record `errored`, which
on an `enforced` workspace blocks every run. If you are on 1.9.0–1.10.1 and your
pools are arm64, that is what you are seeing.

So Terrapod builds the linux assets itself, from upstream's own release tag, its
committed `Pipfile.lock` and its committed PyInstaller spec — the same recipe as
Checkov's own release workflow, on a container pinned to an older glibc so the
bundle runs on every runner image. Each one is executed before publication
(upstream's own smoke test, plus the exact `--framework terraform_plan`
invocation the runner uses) and the build is attested:

```sh
gh attestation verify checkov_linux_arm64.zip --repo mattrobinsonsre/terrapod
```

Only `linux/amd64` and `linux/arm64` are published, which is every platform a
runner can be — a runner is a Kubernetes pod, and both the API and the runner
fetch these for themselves. Nothing is re-hosted: every asset is one Terrapod
built. Nothing about the scan itself changes either: same engine, same version,
same `CKV_*` rule ids, same findings.

For the same reason OPA and Trivy are served for linux only. A request for any
other platform is refused outright rather than answered with a 404 from
upstream.

Point `checkov_mirror_url` at an internal mirror if you would rather serve it
yourself — it needs the same layout, `{base}/{version}/checkov_<platform>.zip`
— and move `checkov_checksum_api_url` with it, because the digest has to
describe the asset actually being served.

### Trivy comes from its publisher; Checkov comes from us

**Trivy** is fetched from `aquasecurity/trivy`'s own releases and verified
against the checksums manifest Trivy publishes beside them. An upstream Trivy
fix is a `helm upgrade` away.

**Checkov is different, and it is the thing to know before pinning a version.**
Upstream stopped publishing `checkov_linux_arm64.zip` after 3.3.21, so every scan
on an arm64 runner errored. Terrapod therefore **builds the Linux Checkov
binaries itself** — from upstream's own tag, lockfile and PyInstaller spec — and
publishes them on its own releases, which is where `checkov_mirror_url` points by
default.

Two consequences:

- **`checkov_version` can only name a version Terrapod has built.** Pinning one
  we have not published returns a 404, which lands as an `errored` scan — and on
  an `enforced` workspace that blocks every run. The published set is visible at
  any time, and the default (`3.3.26`) is always one of them:

  ```sh
  gh release list --repo mattrobinsonsre/terrapod | grep '^3\.'
  ```

- **A brand-new upstream Checkov release is not immediately pinnable.** The
  builder runs weekly, over the newest upstream releases, so one published
  mid-week becomes available on the next run. For an urgent Checkov fix, ask for
  a build rather than pinning a version that does not exist yet.

Taking Checkov from upstream instead is supported — set `checkov_mirror_url` to
`https://github.com/bridgecrewio/checkov/releases/download` — but read
[the platform-tool mirror layout](registry.md#the-platform-tools-and-the-one-pair-that-must-move-together)
first, because **the checksum source has to move with it**. Note that arm64 will
404 on any version after 3.3.21, which is the problem this arrangement exists to
solve.

**A failed fetch is recorded, not raised.** It lands as an `errored` scan result,
which for an `enforced` workspace the server turns into a block — the same
fail-closed behaviour as a crashed scanner, and the same behaviour a runner image
older than this feature gets. An out-of-date or offline runner can never silently
skip an enforced gate.

**Air-gapped / sealed installs** pre-warm both engines like any other cached
artifact; the bulk-warm endpoint and post-install warm Job include them
automatically, derived from the configured versions.
