"""Parametric generator for labelled plan-JSON cases (#602).

Because *we* synthesize each plan, the ground-truth labels fall out of the
generation parameters for free — no manual labelling. This is how the corpus
gets to "substantial" (hundreds of cases) without hand-writing each one.

The generator is deterministic (no randomness): the same code always yields
the same cases in the same order, so a run is reproducible and the offline
integrity test can assert on it. Cases are produced in memory as :class:`Case`
objects (combined with the curated YAML corpus by the CLI); nothing is written
to disk, keeping the repo clean while the corpus stays large and regenerable.

Coverage spans the five risk axes the refinement targets:
  - data_loss        — destroy / replace of stateful resources
  - security         — public exposure, IAM/policy broadening, encryption off
  - irreversibility  — KMS key / snapshot / backup deletion
  - blast_radius     — mass replace, region/provider change
  - churn            — tag-only / known_after_apply / no-op drift (NOT risk)
"""

from __future__ import annotations

from typing import Any

from .cases import Case, MustFlag, RiskBand, Truth

# --- resource archetypes -----------------------------------------------------


# A stateful resource holds data that a destroy/replace would lose.
# ``irreversible`` ones have no practical recovery (KMS key, final-snapshot-
# skipped DB) → critical on destroy. ``name`` is the local name used in the
# terraform address.
STATEFUL = [
    {
        "type": "aws_db_instance",
        "name": "main",
        "irreversible": True,
        "after": {"engine": "postgres", "instance_class": "db.r6g.large"},
    },
    {
        "type": "aws_rds_cluster",
        "name": "core",
        "irreversible": True,
        "after": {"engine": "aurora-postgresql"},
    },
    {
        "type": "aws_dynamodb_table",
        "name": "sessions",
        "irreversible": True,
        "after": {"billing_mode": "PAY_PER_REQUEST"},
    },
    {
        "type": "aws_s3_bucket",
        "name": "data",
        "irreversible": False,
        "after": {"bucket": "acme-data"},
    },
    {
        "type": "aws_ebs_volume",
        "name": "data",
        "irreversible": True,
        "after": {"size": 500, "type": "gp3"},
    },
    {
        "type": "aws_efs_file_system",
        "name": "shared",
        "irreversible": True,
        "after": {"encrypted": True},
    },
    {
        "type": "aws_elasticache_cluster",
        "name": "cache",
        "irreversible": False,
        "after": {"engine": "redis"},
    },
]

# Irreversible-by-nature security/crypto material.
IRREVERSIBLE = [
    {
        "type": "aws_kms_key",
        "name": "primary",
        "after": {"description": "primary CMK", "enable_key_rotation": True},
    },
    {
        "type": "aws_db_snapshot",
        "name": "preupgrade",
        "after": {"db_snapshot_identifier": "preupgrade"},
    },
    {"type": "aws_backup_vault", "name": "prod", "after": {"name": "prod-vault"}},
]

# Benign, stateless, additive resources — create is low risk, churn updates
# are noise.
BENIGN = [
    {
        "type": "aws_cloudwatch_log_group",
        "name": "app",
        "after": {"name": "/acme/app", "retention_in_days": 30},
    },
    {"type": "aws_iam_role", "name": "task", "after": {"name": "acme-task"}},
    {
        "type": "aws_ssm_parameter",
        "name": "config",
        "after": {"name": "/acme/config", "type": "String"},
    },
    {"type": "aws_lb_target_group", "name": "app", "after": {"port": 443, "protocol": "HTTPS"}},
]


def _plan(resource_changes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format_version": "1.2",
        "terraform_version": "1.12.0",
        "resource_changes": resource_changes,
    }


def _rc(
    address: str,
    rtype: str,
    name: str,
    actions: list[str],
    before: Any,
    after: Any,
    after_unknown: dict | None = None,
) -> dict[str, Any]:
    change: dict[str, Any] = {"actions": actions, "before": before, "after": after}
    if after_unknown is not None:
        change["after_unknown"] = after_unknown
    return {"address": address, "type": rtype, "name": name, "change": change}


# --- per-axis generators -----------------------------------------------------


def _data_loss_cases() -> list[Case]:
    out: list[Case] = []
    for spec in STATEFUL:
        addr = f"{spec['type']}.{spec['name']}"
        # destroy
        crit = spec["irreversible"]
        out.append(
            Case(
                id=f"gen-dataloss-destroy-{spec['type']}",
                surface="plan",
                source="generated",
                tags=("data_loss",),
                title=f"Destroy stateful {spec['type']}",
                plan_json=_plan(
                    [_rc(addr, spec["type"], spec["name"], ["delete"], spec["after"], None)]
                ),
                truth=Truth(
                    risk=RiskBand(min="critical" if crit else "high"),
                    must_flag=(MustFlag(addr, "critical" if crit else "high"),),
                    key_facts=(addr, "destroy"),
                    forbidden_claims=("no changes", "no-op"),
                ),
            )
        )
        # replace (delete+create) — still destructive on a stateful resource
        out.append(
            Case(
                id=f"gen-dataloss-replace-{spec['type']}",
                surface="plan",
                source="generated",
                tags=("data_loss", "blast_radius"),
                title=f"Force-replace stateful {spec['type']}",
                plan_json=_plan(
                    [
                        _rc(
                            addr,
                            spec["type"],
                            spec["name"],
                            ["delete", "create"],
                            spec["after"],
                            spec["after"],
                        )
                    ]
                ),
                truth=Truth(
                    risk=RiskBand(min="high"),
                    must_flag=(MustFlag(addr, "high"),),
                    key_facts=(addr,),
                    forbidden_claims=("no changes",),
                ),
            )
        )
    return out


def _irreversibility_cases() -> list[Case]:
    out: list[Case] = []
    for spec in IRREVERSIBLE:
        addr = f"{spec['type']}.{spec['name']}"
        out.append(
            Case(
                id=f"gen-irrev-destroy-{spec['type']}",
                surface="plan",
                source="generated",
                tags=("irreversibility",),
                title=f"Destroy irreversible {spec['type']}",
                plan_json=_plan(
                    [_rc(addr, spec["type"], spec["name"], ["delete"], spec["after"], None)]
                ),
                truth=Truth(
                    risk=RiskBand(min="critical"),
                    must_flag=(MustFlag(addr, "critical"),),
                    key_facts=(addr,),
                    forbidden_claims=("no changes",),
                ),
            )
        )
    return out


def _security_cases() -> list[Case]:
    out: list[Case] = []

    # Security group opening 0.0.0.0/0 to a sensitive port.
    for port, label in [(22, "ssh"), (3389, "rdp"), (5432, "postgres")]:
        addr = f"aws_security_group.{label}"
        before = {"ingress": [{"from_port": port, "to_port": port, "cidr_blocks": ["10.0.0.0/8"]}]}
        after = {"ingress": [{"from_port": port, "to_port": port, "cidr_blocks": ["0.0.0.0/0"]}]}
        out.append(
            Case(
                id=f"gen-sec-sg-open-{label}",
                surface="plan",
                source="generated",
                tags=("security",),
                title=f"Open {label} ({port}) to 0.0.0.0/0",
                plan_json=_plan(
                    [_rc(addr, "aws_security_group", label, ["update"], before, after)]
                ),
                truth=Truth(
                    risk=RiskBand(min="high"),
                    must_flag=(MustFlag(addr, "high"),),
                    key_facts=("0.0.0.0/0",),
                ),
            )
        )

    # S3 public-access-block disabled.
    addr = "aws_s3_bucket_public_access_block.data"
    out.append(
        Case(
            id="gen-sec-s3-public",
            surface="plan",
            source="generated",
            tags=("security",),
            title="Disable S3 public access block",
            plan_json=_plan(
                [
                    _rc(
                        addr,
                        "aws_s3_bucket_public_access_block",
                        "data",
                        ["update"],
                        {"block_public_acls": True, "block_public_policy": True},
                        {"block_public_acls": False, "block_public_policy": False},
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="high"),
                must_flag=(MustFlag(addr, "high"),),
            ),
        )
    )

    # Encryption turned off on an EBS volume (in-place update).
    addr = "aws_ebs_volume.data"
    out.append(
        Case(
            id="gen-sec-ebs-encryption-off",
            surface="plan",
            source="generated",
            tags=("security",),
            title="Disable EBS encryption",
            plan_json=_plan(
                [
                    _rc(
                        addr,
                        "aws_ebs_volume",
                        "data",
                        ["update"],
                        {"encrypted": True},
                        {"encrypted": False},
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="high"),
                must_flag=(MustFlag(addr, "high"),),
            ),
        )
    )

    # IAM policy broadened to Action:* Resource:*.
    addr = "aws_iam_role_policy.task"
    out.append(
        Case(
            id="gen-sec-iam-broaden",
            surface="plan",
            source="generated",
            tags=("security",),
            title="Broaden IAM policy to *:*",
            plan_json=_plan(
                [
                    _rc(
                        addr,
                        "aws_iam_role_policy",
                        "task",
                        ["update"],
                        {
                            "policy": '{"Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::acme/*"}]}'
                        },
                        {
                            "policy": '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'
                        },
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="high"),
                must_flag=(MustFlag(addr, "high"),),
            ),
        )
    )
    return out


def _benign_create_cases() -> list[Case]:
    out: list[Case] = []
    for spec in BENIGN:
        addr = f"{spec['type']}.{spec['name']}"
        out.append(
            Case(
                id=f"gen-benign-create-{spec['type']}",
                surface="plan",
                source="generated",
                tags=("benign",),
                title=f"Create {spec['type']}",
                plan_json=_plan(
                    [_rc(addr, spec["type"], spec["name"], ["create"], None, spec["after"])]
                ),
                truth=Truth(
                    risk=RiskBand(exact="low"),
                    must_not_flag=(addr,),
                    key_facts=(addr,),
                ),
            )
        )
    return out


def _churn_cases() -> list[Case]:
    """Changes that look like changes but carry no real risk — the model must
    not inflate risk on them, and must not list them as risk factors."""
    out: list[Case] = []

    # Tag-only update on a stateful resource — NOT a data-loss event.
    addr = "aws_db_instance.main"
    out.append(
        Case(
            id="gen-churn-tags-only",
            surface="plan",
            source="generated",
            tags=("churn",),
            title="Tag-only update on RDS (no data risk)",
            plan_json=_plan(
                [
                    _rc(
                        addr,
                        "aws_db_instance",
                        "main",
                        ["update"],
                        {"tags": {"env": "prod"}, "instance_class": "db.r6g.large"},
                        {"tags": {"env": "prod", "team": "data"}, "instance_class": "db.r6g.large"},
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(exact="low"),
                churn_addresses=(addr,),
                forbidden_claims=("destroy", "data loss", "replace"),
            ),
        )
    )

    # known_after_apply churn — an ARN that will be computed; not a risk.
    addr = "aws_lb_target_group.app"
    out.append(
        Case(
            id="gen-churn-known-after-apply",
            surface="plan",
            source="generated",
            tags=("churn",),
            title="known_after_apply attribute churn",
            plan_json=_plan(
                [
                    _rc(
                        addr,
                        "aws_lb_target_group",
                        "app",
                        ["update"],
                        {"port": 443},
                        {"port": 443},
                        after_unknown={"arn": True},
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(max="low"),
                churn_addresses=(addr,),
            ),
        )
    )

    # A real high-risk destroy BURIED among benign churn — must surface the
    # one real risk and not be distracted by the churn.
    real = "aws_db_instance.main"
    churn1 = "aws_cloudwatch_log_group.app"
    churn2 = "aws_ssm_parameter.config"
    out.append(
        Case(
            id="gen-churn-needle-in-haystack",
            surface="plan",
            source="generated",
            tags=("churn", "data_loss"),
            title="One real DB destroy among tag churn",
            plan_json=_plan(
                [
                    _rc(
                        churn1,
                        "aws_cloudwatch_log_group",
                        "app",
                        ["update"],
                        {"tags": {}},
                        {"tags": {"team": "x"}},
                    ),
                    _rc(real, "aws_db_instance", "main", ["delete"], {"engine": "postgres"}, None),
                    _rc(
                        churn2,
                        "aws_ssm_parameter",
                        "config",
                        ["update"],
                        {"tags": {}},
                        {"tags": {"team": "x"}},
                    ),
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="high"),
                must_flag=(MustFlag(real, "high"),),
                churn_addresses=(churn1, churn2),
                key_facts=(real,),
            ),
        )
    )
    return out


def _drift_cases() -> list[Case]:
    """Drift-detection runs — framed as detection reports, not proposals."""
    out: list[Case] = []

    # Drift the apply WOULD revert (manual change to live infra) — elevated.
    addr = "aws_s3_bucket_versioning.logs"
    plan = _plan(
        [
            _rc(
                addr,
                "aws_s3_bucket_versioning",
                "logs",
                ["update"],
                {"versioning_configuration": [{"status": "Disabled"}]},
                {"versioning_configuration": [{"status": "Enabled"}]},
            )
        ]
    )
    plan["resource_drift"] = [
        _rc(
            addr,
            "aws_s3_bucket_versioning",
            "logs",
            ["update"],
            {"versioning_configuration": [{"status": "Enabled"}]},
            {"versioning_configuration": [{"status": "Disabled"}]},
        )
    ]
    out.append(
        Case(
            id="gen-drift-reverting-manual-change",
            surface="drift",
            source="generated",
            tags=("churn", "security"),
            title="Drift detected: versioning disabled out-of-band",
            plan_json=plan,
            truth=Truth(
                risk=RiskBand(min="medium"),
                must_flag=(MustFlag(addr, "medium"),),
                key_facts=("drift",),
                forbidden_claims=("this plan will disable",),
            ),
        )
    )

    # No-op drift only — informational, low.
    addr = "aws_instance.web"
    plan = _plan([])
    plan["resource_drift"] = [
        _rc(
            addr,
            "aws_instance",
            "web",
            ["update"],
            {"tags": {"patched": "no"}},
            {"tags": {"patched": "yes"}},
        )
    ]
    out.append(
        Case(
            id="gen-drift-noop-informational",
            surface="drift",
            source="generated",
            tags=("churn",),
            title="No-op drift (no apply action)",
            plan_json=plan,
            truth=Truth(
                risk=RiskBand(max="low"),
                churn_addresses=(addr,),
                key_facts=("drift",),
            ),
        )
    )
    return out


def build_generated_cases() -> list[Case]:
    """Return the full deterministic set of generated cases."""
    cases: list[Case] = []
    cases += _data_loss_cases()
    cases += _irreversibility_cases()
    cases += _security_cases()
    cases += _benign_create_cases()
    cases += _churn_cases()
    cases += _drift_cases()
    return cases
