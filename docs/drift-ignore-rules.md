# Drift-ignore rules

Per-workspace allowlist for the drift-detection classifier (#482). Lets
you say "this attribute is co-managed elsewhere, don't count it as
drift" without changing apply semantics (which is what HCL's
`lifecycle { ignore_changes }` block does).

Drift detection runs `tofu plan` against the workspace's current state
+ tracked branch and reports `has_changes=true` when anything differs.
The classifier sits between that result and the workspace's
`drift_status` field — if every changed attribute matches a rule in
the workspace's `drift_ignore_rules`, the status drops to `no_drift`.

The classifier is **drift-only**. It does not affect `tofu apply` —
the next time someone runs an apply against an ignored attribute, it
will change exactly as configured. The rules are a UX layer over the
drift signal, not a replacement for `ignore_changes`.

## Rule grammar

Each entry is a single string of the form:

```
<terraform-address>[.<attribute-path>]
```

* `*` matches **zero or more** characters that are NOT `.`. It can span
  across `[N]` index suffixes (so `module.eks*` matches both
  `module.eks` and `module.eks_legacy[0]`) but it never crosses a
  segment boundary into the next module/resource label.
* `[*]` (inside literal brackets) matches any one bracketed index —
  `[0]`, `["foo"]`, `["bar/baz"]`. Use this when you want "any index"
  without accidentally matching surrounding text.
* **Numeric block indices are optional.** HCL nested blocks serialize
  as single-element lists in plan JSON, so an attribute path arrives
  as `config[0].tls_client_config[0].ca_data`. Write the rule the way
  it reads in HCL — `config.tls_client_config.ca_data` — and it
  matches the indexed shape automatically. (String-key `for_each`
  indices like `["prod"]` are NOT optional — they're semantically
  meaningful, so a bare rule won't span every instance.)

## Examples

```jsonc
"drift-ignore-rules": [
  // Co-managed by external automation
  "aws_autoscaling_group.workers[*].desired_capacity",
  "kubernetes_deployment.*.spec[0].replicas",

  // Provider-managed churn we've decided to live with
  "aws_iam_role_policy_attachment.eks_cluster_policy.policy_arn",
  "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data",

  // Whole-resource ignore — silences any change to this resource,
  // including destroys. Use for resources you intentionally let
  // someone else fully own.
  "aws_iam_role.externally_managed"
]
```

## What gets suppressed vs reported

For each `resource_change` in the drift run's plan:

1. The classifier walks `before` vs `after` and enumerates the
   attribute paths that actually differ.
2. For each diff path, it builds `<address>.<path>` and tests it
   against every compiled rule.
3. If **every** diff path matches some rule, the whole resource_change
   is suppressed.
4. If **any** diff path doesn't match, the resource still counts as
   drift — partial suppression is intentional. You'll see in the
   workspace's drift run logs which paths got silenced and which
   didn't.

After all resource_changes are classified:

* Every resource suppressed → `drift_status = "no_drift"`.
* Anything still counts → `drift_status = "drifted"` (badge surfaces
  only the un-ignored changes).

## Safety: per-attribute rules cannot silence destroys

A rule like `kubernetes_deployment.api.spec[0].replicas` will **not**
silence a `delete` action against `kubernetes_deployment.api`. The
classifier only allows whole-resource lifecycle changes (create,
delete, replace) to be silenced by a **bare-address** rule with no
attribute suffix. The reasoning: a per-attribute ignore probably
exists because the operator wants `replicas` left alone, not because
they want to be quiet about their workload disappearing.

If you genuinely want to silence the delete (e.g. an autodiscovery-
managed resource you've decided you don't care about), use the bare
address: `kubernetes_deployment.api`.

## Configuration

Set via the API, the Terraform provider, or the workspace settings UI:

### Terraform provider

```hcl
resource "terrapod_workspace" "platform" {
  name = "platform-prod"
  # …
  drift_ignore_rules = [
    "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data",
    "module.eks_legacy*.argocd_cluster.*.config.tls_client_config.ca_data",
  ]
}
```

### API (PATCH)

```bash
curl -X PATCH "https://<host>/api/v2/workspaces/<id>" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "drift-ignore-rules": [
          "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data"
        ]
      }
    }
  }'
```

## Limits

| Limit | Value |
|---|---|
| Maximum rules per workspace | 50 |
| Maximum characters per rule | 500 |
| Allowed characters | `A-Z`, `a-z`, `0-9`, `_`, `-`, `.`, `[`, `]`, `*`, `"` |

The character allow-list is intentionally narrow so a stray space,
backtick, or shell-quote artefact can't reach the classifier and
crash the regex compiler.

## Audit + observability

Every drift run that suppressed at least one change emits an `info`
log line on the API pod naming the run, the rules in effect, and the
suppressed `(address, paths)` tuples. Useful for:

* Confirming a new rule is matching what you expect after deploying
  it.
* Spotting "creeping suppression" — rules that started life as a
  one-off workaround and ended up silencing more than intended.
* Telling future-you why a workspace stopped flagging drift.

The drift run's plan output still contains the full diff — the
classifier does not redact the underlying plan JSON. The
`drift_status` flip is purely cosmetic; the audit trail is intact.
