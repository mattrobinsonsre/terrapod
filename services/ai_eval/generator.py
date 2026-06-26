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
    {
        "type": "aws_redshift_cluster",
        "name": "warehouse",
        "irreversible": True,
        "after": {"node_type": "ra3.xlplus"},
    },
    {
        "type": "aws_neptune_cluster",
        "name": "graph",
        "irreversible": True,
        "after": {"engine": "neptune"},
    },
    {
        "type": "aws_docdb_cluster",
        "name": "docs",
        "irreversible": True,
        "after": {"engine": "docdb"},
    },
    {
        "type": "aws_msk_cluster",
        "name": "events",
        "irreversible": True,
        "after": {"kafka_version": "3.6.0"},
    },
    {
        "type": "aws_memorydb_cluster",
        "name": "memdb",
        "irreversible": True,
        "after": {"node_type": "db.r6g.large"},
    },
    {
        "type": "aws_fsx_lustre_file_system",
        "name": "scratch",
        "irreversible": True,
        "after": {"storage_capacity": 1200},
    },
    {
        "type": "aws_timestreamwrite_table",
        "name": "metrics",
        "irreversible": True,
        "after": {"table_name": "metrics"},
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
        # A single snapshot is ONE recovery point — high (unmitigated destroy),
        # not critical. Reserve critical for the encryption key (locks out all
        # data) and the backup vault (holds every backup). The model reasoned
        # this distinction consistently; the label follows the reasoning.
        sev = "high" if spec["type"].endswith("snapshot") else "critical"
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
                    risk=RiskBand(min=sev),
                    must_flag=(MustFlag(addr, sev),),
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


def _apply_log(addr: str, rtype: str, line: int, error_block: str) -> str:
    """Render a realistic terraform apply error tail for one resource."""
    verb = "Creating" if "Creating" not in error_block else "Modifying"
    return (
        f"{addr}: {verb}...\n"
        f"╷\n"
        f"│ Error: {error_block}\n"
        f"│\n"
        f"│   with {addr},\n"
        f'│   on main.tf line {line}, in resource "{rtype}" "{addr.split(".", 1)[1]}":\n'
        f'│   {line}: resource "{rtype}" "{addr.split(".", 1)[1]}" {{\n'
        f"╵\n"
    )


def _apply_failure_cases() -> list[Case]:
    """Apply-phase failures — failure_analysis must name the failed resource,
    explain the root cause, and rate how blocking it is. risk_factors are
    candidate fixes; the rubric's must_flag maps to 'the analysis attaches a
    fix to the failed resource'."""
    out: list[Case] = []

    def case(cid, addr, rtype, line, err, *, band, sev, facts, tags=("apply_failure",), forbid=()):
        out.append(
            Case(
                id=cid,
                surface="apply_failure",
                source="generated",
                tags=tags,
                title=cid.replace("gen-applyfail-", "").replace("-", " "),
                apply_log=_apply_log(addr, rtype, line, err),
                truth=Truth(
                    risk=RiskBand(**band),
                    must_flag=(MustFlag(addr, sev),),
                    key_facts=facts,
                    forbidden_claims=forbid,
                ),
            )
        )

    # Already-exists → fix is import (not recreate).
    case(
        "gen-applyfail-iam-already-exists",
        "aws_iam_role.task",
        "aws_iam_role",
        12,
        "creating IAM Role (acme-task): operation error IAM: CreateRole, "
        "https response error StatusCode: 409, EntityAlreadyExists: Role with "
        "name acme-task already exists.",
        band={"min": "medium"},
        sev="medium",
        facts=("aws_iam_role.task", "already exists"),
    )
    # AccessDenied → IAM permission gap.
    case(
        "gen-applyfail-s3-access-denied",
        "aws_s3_bucket.data",
        "aws_s3_bucket",
        3,
        "creating S3 Bucket (acme-data): operation error S3: CreateBucket, "
        "https response error StatusCode: 403, api error AccessDenied: "
        "User is not authorized to perform: s3:CreateBucket.",
        band={"min": "medium"},
        sev="medium",
        facts=("aws_s3_bucket.data", "AccessDenied"),
    )
    # DependencyViolation deleting a SG → detach ENIs first.
    case(
        "gen-applyfail-sg-dependency-violation",
        "aws_security_group.app",
        "aws_security_group",
        40,
        "deleting Security Group (sg-0abc): operation error EC2: "
        "DeleteSecurityGroup, DependencyViolation: resource sg-0abc has a "
        "dependent object (a network interface is still attached).",
        band={"min": "medium"},
        sev="medium",
        facts=("aws_security_group.app", "DependencyViolation"),
    )
    # InvalidParameterValue — bad engine version.
    case(
        "gen-applyfail-rds-invalid-engine-version",
        "aws_db_instance.main",
        "aws_db_instance",
        20,
        "creating RDS DB Instance (acme-main): InvalidParameterCombination: "
        "Cannot find version 13.99 for postgres.",
        band={"min": "medium"},
        sev="medium",
        facts=("aws_db_instance.main", "13.99"),
    )
    # Throttling — transient, retry. Lower blocking level.
    case(
        "gen-applyfail-throttling-transient",
        "aws_cloudwatch_log_group.app",
        "aws_cloudwatch_log_group",
        5,
        "creating CloudWatch Log Group (/acme/app): ThrottlingException: "
        "Rate exceeded. (RequestLimitExceeded)",
        band={"min": "low", "max": "medium"},
        sev="low",
        facts=("ThrottlingException",),
        tags=("apply_failure", "churn"),
    )
    # Service quota exceeded — needs a quota increase, not a code fix.
    case(
        "gen-applyfail-vpc-quota-exceeded",
        "aws_vpc.main",
        "aws_vpc",
        1,
        "creating EC2 VPC: operation error EC2: CreateVpc, VpcLimitExceeded: "
        "The maximum number of VPCs has been reached.",
        band={"min": "medium"},
        sev="medium",
        facts=("aws_vpc.main", "VpcLimitExceeded"),
    )
    return out


def _hard_cases() -> list[Case]:
    """Discriminating cases — the ones that catch a prompt that over-flags
    (cry-wolf) or under-flags (misses a buried risk). These are where an
    already-strong model actually fails, so they're what validate the prompt."""
    out: list[Case] = []

    # --- cry-wolf traps: scary-looking actions, ~zero real risk -> low -------

    # A null_resource replace: actions [delete, create] but no infrastructure.
    out.append(
        Case(
            id="hard-noop-null-resource-replace",
            surface="plan",
            source="generated",
            tags=("churn", "calibration"),
            title="Replace null_resource (no blast radius)",
            plan_json=_plan(
                [
                    _rc(
                        "null_resource.provisioner",
                        "null_resource",
                        "provisioner",
                        ["delete", "create"],
                        {"triggers": {"v": "1"}},
                        {"triggers": {"v": "2"}},
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(exact="low"),
                must_not_flag=("null_resource.provisioner",),
                forbidden_claims=("data loss", "critical"),
            ),
        )
    )
    # Rotating a managed password: a replace, routine, not data loss.
    out.append(
        Case(
            id="hard-noop-random-password-rotate",
            surface="plan",
            source="generated",
            tags=("churn", "calibration"),
            title="Rotate random_password (routine)",
            plan_json=_plan(
                [
                    _rc(
                        "random_password.db",
                        "random_password",
                        "db",
                        ["delete", "create"],
                        {"length": 32},
                        {"length": 32},
                        after_unknown={"result": True, "bcrypt_hash": True},
                    )
                ]
            ),
            truth=Truth(risk=RiskBand(max="medium"), must_not_flag=("random_password.db",)),
        )
    )
    # Tightening a security group: 0.0.0.0/0 -> internal CIDR is an IMPROVEMENT.
    out.append(
        Case(
            id="hard-improve-sg-tighten",
            surface="plan",
            source="generated",
            tags=("security", "calibration"),
            title="Tighten SG from world to internal (improvement)",
            plan_json=_plan(
                [
                    _rc(
                        "aws_security_group.api",
                        "aws_security_group",
                        "api",
                        ["update"],
                        {
                            "ingress": [
                                {"from_port": 443, "to_port": 443, "cidr_blocks": ["0.0.0.0/0"]}
                            ]
                        },
                        {
                            "ingress": [
                                {"from_port": 443, "to_port": 443, "cidr_blocks": ["10.0.0.0/8"]}
                            ]
                        },
                    )
                ]
            ),
            truth=Truth(risk=RiskBand(max="medium"), must_not_flag=("aws_security_group.api",)),
        )
    )
    # Enabling encryption: a security improvement, not a risk.
    out.append(
        Case(
            id="hard-improve-enable-encryption",
            surface="plan",
            source="generated",
            tags=("security", "calibration"),
            title="Enable S3 bucket encryption (improvement)",
            plan_json=_plan(
                [
                    _rc(
                        "aws_s3_bucket_server_side_encryption_configuration.data",
                        "aws_s3_bucket_server_side_encryption_configuration",
                        "data",
                        ["create"],
                        None,
                        {
                            "rule": [
                                {
                                    "apply_server_side_encryption_by_default": [
                                        {"sse_algorithm": "aws:kms"}
                                    ]
                                }
                            ]
                        },
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(exact="low"),
                must_not_flag=("aws_s3_bucket_server_side_encryption_configuration.data",),
            ),
        )
    )
    # Deleting an observability resource: low consequence.
    out.append(
        Case(
            id="hard-noop-delete-log-group",
            surface="plan",
            source="generated",
            tags=("churn", "calibration"),
            title="Delete a CloudWatch log group (low)",
            plan_json=_plan(
                [
                    _rc(
                        "aws_cloudwatch_log_group.debug",
                        "aws_cloudwatch_log_group",
                        "debug",
                        ["delete"],
                        {"name": "/acme/debug"},
                        None,
                    )
                ]
            ),
            truth=Truth(risk=RiskBand(max="medium"), must_not_flag=()),
        )
    )
    # Large benign greenfield: many creates, all safe -> low (no inflation at scale).
    greenfield = [
        _rc(
            "aws_cloudwatch_log_group.app",
            "aws_cloudwatch_log_group",
            "app",
            ["create"],
            None,
            {"name": "/acme/app", "retention_in_days": 30},
        ),
        _rc("aws_iam_role.task", "aws_iam_role", "task", ["create"], None, {"name": "acme-task"}),
        _rc(
            "aws_lb_target_group.app",
            "aws_lb_target_group",
            "app",
            ["create"],
            None,
            {"port": 443, "protocol": "HTTPS"},
        ),
        _rc(
            "aws_ssm_parameter.cfg",
            "aws_ssm_parameter",
            "cfg",
            ["create"],
            None,
            {"name": "/acme/cfg", "type": "String"},
        ),
    ]
    out.append(
        Case(
            id="hard-noop-benign-greenfield",
            surface="plan",
            source="generated",
            tags=("benign", "calibration"),
            title="Greenfield of benign resources (low)",
            plan_json=_plan(greenfield),
            truth=Truth(
                risk=RiskBand(exact="low"),
                must_not_flag=("aws_iam_role.task", "aws_lb_target_group.app"),
            ),
        )
    )
    # In-place DB resize: medium (brief failover), not high.
    out.append(
        Case(
            id="hard-calib-rds-resize-inplace",
            surface="plan",
            source="generated",
            tags=("calibration",),
            title="In-place RDS instance_class resize (medium)",
            plan_json=_plan(
                [
                    _rc(
                        "aws_db_instance.main",
                        "aws_db_instance",
                        "main",
                        ["update"],
                        {"instance_class": "db.r6g.large", "apply_immediately": True},
                        {"instance_class": "db.r6g.xlarge", "apply_immediately": True},
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="medium", max="high"),
                must_flag=(MustFlag("aws_db_instance.main", "medium"),),
            ),
        )
    )

    # --- buried needles + subtle exposures: real risk, easy to miss ----------

    # One real DB destroy among many tag-only updates.
    needle = [
        _rc(
            f"aws_ssm_parameter.p{i}",
            "aws_ssm_parameter",
            f"p{i}",
            ["update"],
            {"tags": {}},
            {"tags": {"team": "x"}},
        )
        for i in range(12)
    ]
    needle.insert(
        7,
        _rc(
            "aws_rds_cluster.core",
            "aws_rds_cluster",
            "core",
            ["delete"],
            {"engine": "aurora-postgresql"},
            None,
        ),
    )
    out.append(
        Case(
            id="hard-needle-db-destroy-in-churn",
            surface="plan",
            source="generated",
            tags=("data_loss", "churn"),
            title="One RDS destroy buried in 12 tag updates",
            plan_json=_plan(needle),
            truth=Truth(
                risk=RiskBand(min="critical"),
                must_flag=(MustFlag("aws_rds_cluster.core", "critical"),),
                churn_addresses=tuple(f"aws_ssm_parameter.p{i}" for i in range(12)),
                key_facts=("aws_rds_cluster.core",),
            ),
        )
    )
    # S3 bucket policy that adds a wildcard Principal — public, subtle (in a doc).
    out.append(
        Case(
            id="hard-subtle-s3-policy-wildcard",
            surface="plan",
            source="generated",
            tags=("security",),
            title="S3 bucket policy adds Principal:* (public)",
            plan_json=_plan(
                [
                    _rc(
                        "aws_s3_bucket_policy.data",
                        "aws_s3_bucket_policy",
                        "data",
                        ["update"],
                        {
                            "policy": '{"Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::111111111111:root"},"Action":"s3:GetObject"}]}'
                        },
                        {
                            "policy": '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject"}]}'
                        },
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="high"),
                must_flag=(MustFlag("aws_s3_bucket_policy.data", "high"),),
            ),
        )
    )
    # IAM policy that adds iam:PassRole on * — privilege escalation, subtle.
    out.append(
        Case(
            id="hard-subtle-iam-passrole-wildcard",
            surface="plan",
            source="generated",
            tags=("security",),
            title="IAM policy adds iam:PassRole on * (priv-esc)",
            plan_json=_plan(
                [
                    _rc(
                        "aws_iam_role_policy.ci",
                        "aws_iam_role_policy",
                        "ci",
                        ["update"],
                        {
                            "policy": '{"Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::acme/*"}]}'
                        },
                        {
                            "policy": '{"Statement":[{"Effect":"Allow","Action":["s3:GetObject","iam:PassRole"],"Resource":"*"}]}'
                        },
                    )
                ]
            ),
            truth=Truth(
                risk=RiskBand(min="high"), must_flag=(MustFlag("aws_iam_role_policy.ci", "high"),)
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
    cases += _apply_failure_cases()
    cases += _hard_cases()
    return cases
