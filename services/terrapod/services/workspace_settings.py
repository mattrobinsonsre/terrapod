"""Per-workspace security-scan and AI-summary settings, and their rules (#1763).

These settings shipped on the workspace API and the provider resource, but on
nothing else — so a rule covering hundreds of workspaces could not opt them in
at creation, and there was no apply-to-existing path either. Between those two
gaps there was no scalable way to set them at all.

The rules live here rather than in a router for the reason
`services.parallelism` already gives: four paths write a workspace setting —
workspace create, workspace update, the autodiscovery rule template, and bulk
update — and a validator only one caller uses is one the next caller forgets.
That is not a hypothetical here; it is the defect this module exists to close.

Everything raises `ValueError`. The HTTP surfaces wrap it into a 422; the
autodiscovery template and any future non-HTTP caller do not have to care.

`validate_scan_enforcement` is the one that takes an engine, and it is the
reason bulk-update cannot validate this setting from the payload alone — see
`routers/workspace_bulk._reject_scan_enforcement_on_unscannable_engines`.
"""

from __future__ import annotations

import re

from terrapod.services.cloud_identity_resolver import unsafe_target_reason

SCAN_ENFORCEMENTS = frozenset({"off", "advisory", "enforced"})
SCAN_ENGINES = frozenset({"checkov", "trivy", "both"})
SCAN_SEVERITY_THRESHOLDS = frozenset({"critical", "high", "medium", "low"})
AI_SUMMARY_MODES = frozenset({"default", "enabled", "disabled"})
VCS_WORKFLOWS = frozenset({"merge_then_apply", "apply_then_merge"})
AUTO_MERGE_STRATEGIES = frozenset({"merge", "squash", "rebase"})


def validate_ansible_version(raw: object) -> str:
    """An ansible-core version for a workspace, or "" to inherit the default.

    **The only rule is the deployment's pre-release policy** -- the same
    `binary_cache.allow_prerelease` that decides whether a workspace may pin a
    terraform/tofu release candidate. No format rule beyond that, deliberately:
    `engine_version` has none either, and a version that does not exist fails at
    install time with pip's own message, which is a better error than a regex of
    ours guessing at what PyPI publishes.

    It needs its own check rather than the engine version's because **PEP 440
    spells a pre-release without a hyphen** -- `2.21.5rc1`, not `2.21.5-rc1` --
    and `binary_cache_service._parse_stability` looks for the hyphenated form.
    Handed a PyPI version it returns "stable" for every pre-release, so a
    GA-only deployment would silently accept an ansible-core release candidate.
    `is_pypi_version_allowed` shares the tiers, so the policy means one thing.
    """
    from terrapod.services.binary_cache_service import is_pypi_version_allowed

    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("ansible-version must be a string")
    value = raw.strip()
    if not value:
        return ""
    if not is_pypi_version_allowed(value):
        from terrapod.config import settings

        policy = settings.registry.binary_cache.allow_prerelease
        raise ValueError(
            f"ansible-version {value!r} is a pre-release, which this deployment's "
            f"binary_cache.allow_prerelease policy ({policy!r}) does not permit. Set the "
            f"policy to 'rc', 'beta', 'alpha' or 'dev' to allow it, or pin a GA version."
        )
    return value


#: A fleet standardising on a handful of internal collections, not a lockfile.
#: Past this the declaration has stopped being configuration and the
#: configuration's own `requirements.yml` is the right home.
MAX_ANSIBLE_COLLECTIONS = 50

#: `namespace.name`, which is all a Galaxy coordinate ever is. Both halves are
#: lowercase alphanumeric-plus-underscore by ansible's own rule, and a name that
#: does not match cannot resolve against any Galaxy server -- so this refuses it
#: here with the field named rather than at install time inside a Job.
_COLLECTION_NAME = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def validate_ansible_collections(raw: object) -> list[dict[str, str]]:
    """The collections a workspace declares, as given.

    Entries are `{"name": "ns.coll"}` with an optional `"version"` -- ansible's
    own `requirements.yml` shape minus the parts that do not belong in platform
    configuration: no `source` (the source is Terrapod, and letting a workspace
    name another would be a hole in the proxy this exists to route through) and
    no `type` (a git or tarball requirement bypasses the registry and its RBAC
    entirely).

    **Validated, never normalised.** No lowercasing, no trimming of a version,
    no de-duplicating into a canonical order -- a provider writes the server's
    response back into state, so a stored value that differs from the declared
    one fails every apply with "Provider produced inconsistent result after
    apply". Whitespace is refused rather than stripped for the same reason.

    No format rule on `version`, deliberately, following `ansible-version` and
    `engine_version`: ansible accepts a range (`>=1.0.0,<2.0.0`) as readily as a
    pin, so a regex of ours would refuse valid input. A version that cannot
    resolve fails at install with ansible's own message, which names the
    versions that do exist.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("ansible-collections must be a list")
    if len(raw) > MAX_ANSIBLE_COLLECTIONS:
        raise ValueError(
            f"ansible-collections: maximum {MAX_ANSIBLE_COLLECTIONS} entries. Past that "
            f"the list has stopped being platform configuration; declare them in the "
            f"configuration's own requirements.yml."
        )

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                "ansible-collections entries must be objects like "
                '{"name": "community.general", "version": "1.2.3"}'
            )
        unknown = set(entry) - {"name", "version"}
        if unknown:
            # Named rather than ignored: `source` and `type` are the two a reader
            # would reach for from ansible's own documentation, and silently
            # dropping either would route a collection around the registry while
            # reporting success.
            raise ValueError(
                f"ansible-collections entries accept only 'name' and 'version'; got "
                f"{sorted(unknown)}. A collection is always fetched from this "
                f"deployment's own Galaxy surface, so 'source' and 'type' are not "
                f"configurable here."
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("ansible-collections entries need a non-empty string 'name'")
        if not _COLLECTION_NAME.match(name):
            raise ValueError(
                f"ansible-collections: {name!r} is not a collection name. Galaxy "
                f"coordinates are 'namespace.name', lowercase, letters, digits and "
                f"underscores."
            )
        if name in seen:
            # Two entries for one collection is a conflict ansible's resolver
            # would refuse with a message about requirements it cannot satisfy;
            # refusing it here names the field instead.
            raise ValueError(f"ansible-collections: {name!r} is declared more than once")
        seen.add(name)

        item: dict[str, str] = {"name": name}
        version = entry.get("version")
        if version is not None:
            if not isinstance(version, str):
                raise ValueError(f"ansible-collections: the version for {name!r} must be a string")
            if not version or version != version.strip():
                raise ValueError(
                    f"ansible-collections: the version for {name!r} must not be empty or "
                    f"carry surrounding whitespace"
                )
            item["version"] = version
        out.append(item)
    return out


#: The quantities a resource request may be written in -- and it is deliberately
#: NARROWER than Kubernetes accepts, because it is exactly what
#: `runner.job_template._double_resource` can double. That function logs a
#: warning and returns the value unchanged when it cannot parse one, so a
#: perfectly valid Kubernetes quantity it does not recognise ("0.5", "1G",
#: "512M") yields a limit EQUAL to the request rather than twice it -- a silent
#: halving of the headroom every other workspace gets. Refusing it at the write
#: boundary is the only place that asymmetry is visible.
_RESOURCE_QUANTITY = re.compile(r"^\d+(m|Ki|Mi|Gi|Ti)?$")


def validate_resource_quantity(value: object, field: str) -> str:
    """A CPU or memory request, or `""` to inherit the deployment default.

    Shared by all four resource settings -- the orchestrator's pair and the
    Ansible execution environment's (#2154) -- so the two cannot come to
    disagree about what is writable.

    The orchestrator's pair was unvalidated until #2154: a workspace could
    store "half a cpu" and the only symptom was the Job being refused at
    admission, with the run failing on an opaque message from the Kubernetes
    API server naming neither the workspace nor the field. The value reaches a
    cluster, so it is refused here.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field}: must be a string, not {type(value).__name__}")
    text = value.strip()
    if text != value:
        raise ValueError(f"{field}: must not carry surrounding whitespace")
    if text == "":
        return ""
    if not _RESOURCE_QUANTITY.match(text):
        raise ValueError(
            f"{field}: {value!r} is not a resource quantity this deployment can size. "
            "Use a whole number of CPUs ('1', '4'), millicores ('500m'), or binary "
            "bytes ('256Mi', '2Gi') -- decimals ('0.5') and decimal SI suffixes "
            "('1G', '512M') are refused because the limit is computed as 2x the "
            "request and those forms cannot be doubled, which would silently leave "
            "the limit equal to the request. Empty inherits the deployment default."
        )
    return text


def effective_ee_resources(
    workspace_cpu: str | None, workspace_memory: str | None
) -> tuple[str, str]:
    """The EE sidecar's CPU and memory request for a workspace (#2154).

    The single resolver, and single for a reason: two sites deciding what to
    request is how a run's recorded size comes to differ from the size its Job
    actually asked for. Resolved once at run creation and snapshotted onto the
    run, so the concrete numbers survive the deployment default moving.

    `""` and `None` both mean inherit --
    `validate_resource_quantity` is what normalises a request to `""`, and this
    is what turns `""` into something a Job spec can carry.
    """
    from terrapod.config import settings

    return (
        (workspace_cpu or "") or settings.default_ee_resource_cpu,
        (workspace_memory or "") or settings.default_ee_resource_memory,
    )


#: A guard against a value that is certainly a mistake, not a limit the
#: scanners impose.
MAX_SCAN_SKIP_RULES = 200
MAX_SCAN_SKIP_RULE_LENGTH = 100

#: Workspace context is a hint, not a repo. Anything bigger belongs in
#: `fleet_context` or a doc the operator pastes into their `prompt_suffix`.
MAX_AI_SUMMARY_CONTEXT = 4000


def ansible_collection_arguments(declared: object) -> list[str]:
    """A workspace's stored collections as `ansible-galaxy` arguments.

    `[{"name": "ns.coll", "version": "1.2.3"}]` becomes `["ns.coll:1.2.3"]`,
    which is ansible's own argument form. Flattened here, in the API, so the
    runner carries no opinion about the stored shape and needs no YAML parser
    to read a list it is only going to pass through.

    Tolerant of a malformed row rather than raising: this runs on the dispatch
    path, where the alternative to skipping a bad entry is a run that cannot
    start. Writes are validated by `validate_ansible_collections`, so the only
    way one gets here is a row written before that existed.
    """
    if not isinstance(declared, list):
        return []
    out: list[str] = []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        version = entry.get("version")
        out.append(f"{name}:{version}" if isinstance(version, str) and version else name)
    return out


def _enum(value: object, valid: frozenset[str], field: str, default: str) -> str:
    s = str(value if value is not None else default)
    if s not in valid:
        raise ValueError(f"{field} must be one of {sorted(valid)}")
    return s


def validate_scan_enforcement(raw: object, engine: str, default: str = "advisory") -> str:
    """`security-scan-enforcement`, refused where the engine is never scanned (#1567).

    Checkov and Trivy read Terraform plan JSON, so a Pulumi run has nothing to
    scan. `enforced` there held every apply for a result that never came, and
    `advisory` would record a setting that does nothing while saying it did
    something. Such a workspace defaults to, and accepts only, `off`.
    """
    from terrapod.engines import evaluates_security_scans

    scannable = evaluates_security_scans(engine)
    if not scannable:
        default = "off"
    value = _enum(raw, SCAN_ENFORCEMENTS, "security-scan-enforcement", default)
    if value != "off" and not scannable:
        raise ValueError(
            f"security-scan-enforcement must be off for a {engine} workspace: "
            "security scanning reads Terraform plan JSON and is not available "
            f"for {engine} runs yet"
        )
    return value


def validate_scan_engine(raw: object, default: str = "checkov") -> str:
    return _enum(raw, SCAN_ENGINES, "security-scan-engine", default)


def validate_scan_severity_threshold(raw: object, default: str = "high") -> str:
    return _enum(raw, SCAN_SEVERITY_THRESHOLDS, "security-scan-severity-threshold", default)


def validate_scan_skip_rules(raw: object) -> list[str]:
    """A list of non-empty rule-id strings (Checkov `CKV_*` / Trivy `AVD-*`)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("security-scan-skip-rules must be a list")
    if len(raw) > MAX_SCAN_SKIP_RULES:
        raise ValueError(f"security-scan-skip-rules: maximum {MAX_SCAN_SKIP_RULES} entries")
    out: list[str] = []
    for v in raw:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("security-scan-skip-rules entries must be non-empty strings")
        if len(v) > MAX_SCAN_SKIP_RULE_LENGTH:
            raise ValueError(
                f"security-scan-skip-rules entries must be ≤ {MAX_SCAN_SKIP_RULE_LENGTH} characters"
            )
        out.append(v.strip())
    return out


def validate_ai_summary_mode(raw: object) -> str:
    """A three-state enum, also constrained by a DB CHECK.

    Rejected here rather than at the constraint so the caller gets something
    actionable instead of a 500 from the integrity error.
    """
    if raw not in AI_SUMMARY_MODES:
        raise ValueError("ai-summary-mode must be 'default', 'enabled', or 'disabled'")
    return str(raw)


def validate_ai_policy_mode(raw: object) -> str:
    """The AI policy gate's per-workspace override (#1766).

    Same three states as `ai-summary-mode`, and validated the same way -- but
    accepting a value here is not the same as honouring it, in BOTH directions.

    "disabled": a MANDATORY gate ignores it by design
    (`ai_policy_service.effective_enforcement`), because a fleet-wide blocking
    control any workspace admin could switch off is not a control. Rejecting
    the value instead would be worse: it would stop an operator recording an
    advisory opt-out that is perfectly legitimate, and imply the gate is
    weaker than it is.

    "enabled": accepted, and a **synonym for "default"** -- it cannot opt a
    workspace into a gate the deployment has not turned on. See
    `effective_enforcement` for why there is nothing to opt into.
    """
    if raw not in AI_SUMMARY_MODES:
        raise ValueError("ai-policy-mode must be 'default', 'enabled', or 'disabled'")
    return str(raw)


def validate_ai_summary_context(raw: object) -> str:
    ctx = "" if raw is None else raw
    if not isinstance(ctx, str):
        raise ValueError("ai-summary-context must be a string")
    if len(ctx) > MAX_AI_SUMMARY_CONTEXT:
        raise ValueError(f"ai-summary-context max length is {MAX_AI_SUMMARY_CONTEXT} characters")
    return ctx


def validate_vcs_workflow(raw: object) -> str:
    if raw not in VCS_WORKFLOWS:
        raise ValueError("vcs-workflow must be 'merge_then_apply' or 'apply_then_merge'")
    return str(raw)


def validate_auto_merge_strategy(raw: object) -> str:
    if raw not in AUTO_MERGE_STRATEGIES:
        raise ValueError("auto-merge-strategy must be 'merge', 'squash', or 'rebase'")
    return str(raw)


def check_apply_then_merge_allowed(
    workflow: str, *, has_vcs_connection: bool, auto_apply: bool
) -> None:
    """The two invariants `apply_then_merge` carries, in one place.

    Under `apply_then_merge` the apply runs BEFORE the PR merges, so it needs a
    VCS connection to have a PR at all, and auto-applying would apply changes
    from a branch nobody has approved — which is the whole thing the workflow
    exists to prevent.

    Shared because three paths set the workflow — create, the workspace PATCH,
    and bulk update — and only the PATCH used to enforce this. Create ignored
    `vcs-workflow` entirely (#1763), so a configuration asking for
    `apply_then_merge` silently got the default and no one was told.
    """
    if workflow != "apply_then_merge":
        return
    if not has_vcs_connection:
        raise ValueError(
            "vcs-workflow 'apply_then_merge' requires a VCS connection — "
            "configure the workspace's VCS settings first"
        )
    if auto_apply:
        raise ValueError(
            "vcs-workflow 'apply_then_merge' is incompatible with auto-apply — "
            "set auto-apply to false in the same request"
        )


# ── Settings that three or more paths write (#1763) ──────────────────────
#
# These moved out of `routers/tfe_v2` when bulk update gained them. They were
# private helpers on the one router that happened to need them first, which is
# the shape `services.parallelism` warns about: the next caller re-implements
# the rule, or skips it.

MAX_TRIGGER_PREFIXES = 20
MAX_DRIFT_IGNORE_RULES = 50
MAX_DRIFT_IGNORE_RULE_LENGTH = 500
MAX_SLACK_CHANNEL = 128

_DRIFT_IGNORE_RULE_RE = re.compile(r"^[A-Za-z0-9_*.\-\[\]\"]+$")


def sanitize_working_directory(raw: str) -> str:
    """Strip leading/trailing slashes, reject traversal."""
    v = raw.strip().strip("/")
    if ".." in v:
        raise ValueError("working-directory: path traversal not allowed")
    return v


#: The Ansible roots and run defaults (#1929). `inventory_paths` is the same
#: shape as a trigger prefix -- a short ordered list of repo-relative paths --
#: so it takes the same bound; `inventory_ignore_paths` holds globs rather than
#: roots and a real repository accumulates more of them.
MAX_INVENTORY_PATHS = 20
MAX_INVENTORY_IGNORE_PATHS = 50
MAX_ANSIBLE_PATH_LEN = 500


def _check_relative_path(value: str, field: str) -> str:
    """Refuse what a repo-relative path may not be. Returns it UNCHANGED.

    Deliberately not `sanitize_working_directory`, which strips slashes: see
    this module's note on why these refuse instead of normalising.
    """
    if len(value) > MAX_ANSIBLE_PATH_LEN:
        raise ValueError(f"{field}: maximum {MAX_ANSIBLE_PATH_LEN} characters")
    if ".." in value:
        raise ValueError(f"{field}: path traversal not allowed")
    if value.startswith("/") or value.endswith("/"):
        raise ValueError(
            f"{field}: write the path without a leading or trailing slash "
            f"(it is relative to the repository root)"
        )
    if value != value.strip():
        raise ValueError(f"{field}: leading or trailing whitespace is not allowed")
    return value


def validate_config_directory(raw: object) -> str:
    """The Ansible config root -- an ordinary repo-relative path.

    `""` means the configuration version's root, exactly as it does for
    `working-directory`. There is no third state: whether the workspace HAS a
    configure operation is `configure-enabled` (#1966), not the absence of a
    path. #1929 shipped this nullable with `None` carrying that meaning, which
    made one field answer two questions and left `""` and `null` distinguishable
    for a path whose natural default is the root it is relative to.
    """
    if not isinstance(raw, str):
        raise ValueError("config-directory must be a string")
    return _check_relative_path(raw, "config-directory")


def validate_operations(*, plan_apply: bool, configure: bool) -> None:
    """Refuse a workspace that declares no operations at all (#1966).

    The one combination two independent booleans permit and nobody wants. A
    workspace with neither operation can never produce a run, so accepting it
    would push a "nothing to do" branch into the dispatcher, the reconciler,
    autodiscovery materialisation and every UI surface -- to represent a state
    whose only honest meaning is "I did not mean to do that". `locked` already
    expresses "park this workspace", reversibly.

    **Takes the RESULTING values, not the submitted ones**, so it must be
    called with the merge of what the caller sent over what is stored. That is
    deliberately unlike the format validators either side of it: those check
    only what the caller supplied, because re-validating a stored value is how
    a row written by a migration becomes uneditable (#316). A cross-field
    invariant is the exception that proves the rule -- it is about the state the
    write produces, so it cannot be answered from the submitted fields alone.
    """
    if not plan_apply and not configure:
        raise ValueError(
            "a workspace must have at least one operation: set plan-apply-enabled "
            "or configure-enabled (or both)"
        )


class PlanApplyNotEnabled(ValueError):
    """Raised when a plan or apply is asked for on a workspace that declares no
    such operation (#2116).

    A `ValueError` subclass for the reason `LabelValidationError` gives: a
    caller using a bare `except ValueError` still catches it, while the explicit
    type lets a router translate to 422 without catching unrelated
    `ValueError`s -- and, which matters more here, lets the platform's own
    run creators tell this refusal apart from a genuine failure and **skip
    quietly** instead of logging an error every cycle.
    """


def require_plan_apply_operation(workspace) -> None:
    """Refuse a plan/apply run on a workspace that does not declare one (#2116).

    The enforcement twin of `validate_operations` above: that one refuses a
    workspace declaring *no* operations at the write boundary, this one refuses
    a *run* the declaration excludes. #1966 shipped the declaration and nothing
    read it on the run path, so `plan-apply-enabled: false` was accepted by
    every write surface and ignored by all fifteen run creators -- the
    flag-nothing-reads defect, on a column an operator had deliberately set.

    **Called from `run_service.create_run` and nowhere else**, deliberately.
    That is the only `Run` constructor on the live path, so one call there
    cannot be bypassed, and no caller has to remember the rule. What a caller
    chooses is only how to *react*:

    * a run somebody **asked for** -- the API routes, the service catalog,
      onboarding, a Pulumi CLI update -- translates it into a 4xx naming the
      column, because a request for something the workspace declares it does
      not do is a mistake worth reporting;
    * a run the **platform decided on its own** -- a VCS push, drift detection,
      a run trigger, module impact, autodiscovery lifecycle, a destroy retry --
      catches it and skips, because there is nobody to tell and erroring once
      per poll cycle would be noise about a workspace behaving exactly as
      declared.

    `configure_enabled` is deliberately **not** checked here: configure
    operations do not exist yet (#1972 and its neighbours), so a workspace
    declaring one has nothing to run and nothing to refuse. When a configure
    run arrives it will not come through this guard -- it is not a plan/apply.
    """
    if not workspace.plan_apply_enabled:
        raise PlanApplyNotEnabled(
            "this workspace declares no plan/apply operation "
            "(plan-apply-enabled is false), so it cannot plan or apply; "
            "set plan-apply-enabled on the workspace to allow it"
        )


def _validate_path_list(raw: object, field: str, cap: int) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list of strings")
    if len(raw) > cap:
        raise ValueError(f"{field}: maximum {cap} entries")
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError(f"{field} entries must be strings")
        if not entry:
            raise ValueError(f"{field} entries must be non-empty")
        out.append(_check_relative_path(entry, field))
    return out


def validate_inventory_paths(raw: object) -> list[str]:
    """Inventory sources, in order. **The order is precedence.**

    Each entry may name a file as well as a directory, so nothing here checks
    for one or the other: ansible's own documented layout puts `production` and
    `staging` side by side as files, and pointing at the containing directory
    instead merges them.
    """
    if raw is None:
        return []
    return _validate_path_list(raw, "inventory-paths", MAX_INVENTORY_PATHS)


def validate_inventory_ignore_paths(raw: object) -> list[str]:
    """Globs pruned from a fetched inventory source before ansible reads it.

    The trailing-slash refusal earns its keep here rather than merely being
    consistent: `_apply_ignore_paths` matches against a path relative to the
    config root, and that path carries **no trailing slash even for a
    directory**. So `cache/` is a pattern that reads as "exclude this
    directory" and excludes nothing -- which fails in the direction that looks
    harmless, leaving the source wider than the operator asked for.
    """
    if raw is None:
        return []
    return _validate_path_list(raw, "inventory-ignore-paths", MAX_INVENTORY_IGNORE_PATHS)


def validate_ansible_run_default(raw: object, field: str) -> str:
    """`default_playbook` / `default_limit`: a plain string, length-capped.

    Stored byte for byte. A `--limit` pattern is ansible's own syntax -- `:`,
    `!`, `&`, `~` and wildcards are all meaningful in it -- so there is no
    format rule to impose here beyond the column's width, and guessing at one
    would refuse patterns ansible accepts.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    if len(raw) > MAX_ANSIBLE_PATH_LEN:
        raise ValueError(f"{field}: maximum {MAX_ANSIBLE_PATH_LEN} characters")
    return raw


def validate_trigger_prefixes(raw: object) -> list[str]:
    """Each entry is normalised like a working directory; at most 20."""
    if not isinstance(raw, list):
        raise ValueError("trigger-prefixes must be a list of strings")
    if len(raw) > MAX_TRIGGER_PREFIXES:
        raise ValueError(f"trigger-prefixes: maximum {MAX_TRIGGER_PREFIXES} entries")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("trigger-prefixes entries must be strings")
        v = sanitize_working_directory(entry)
        if not v:
            raise ValueError("trigger-prefixes entries must be non-empty")
        result.append(v)
    return result


def validate_drift_ignore_rules(raw: object) -> list[str]:
    """Glob-aware address-plus-attribute-path strings (#482).

    The character set is deliberately narrow, and the length cap is what keeps
    a legitimate deep path expressible without allowing unbounded growth.
    """
    if not isinstance(raw, list):
        raise ValueError("drift-ignore-rules must be a list of strings")
    if len(raw) > MAX_DRIFT_IGNORE_RULES:
        raise ValueError(f"drift-ignore-rules: maximum {MAX_DRIFT_IGNORE_RULES} entries")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("drift-ignore-rules entries must be strings")
        v = entry.strip()
        if not v:
            raise ValueError("drift-ignore-rules entries must be non-empty")
        if len(v) > MAX_DRIFT_IGNORE_RULE_LENGTH:
            raise ValueError(
                f"drift-ignore-rules entries must be ≤ {MAX_DRIFT_IGNORE_RULE_LENGTH} characters"
            )
        if not _DRIFT_IGNORE_RULE_RE.match(v):
            raise ValueError(
                "drift-ignore-rules entries may only contain letters, digits, "
                "underscores, hyphens, dots, brackets, asterisks, and double quotes"
            )
        result.append(v)
    return result


def validate_plan_expiry_seconds(raw: object) -> int | None:
    """A plan-expiry TTL (#646). None / 0 disables it and stores NULL."""
    if raw is None:
        return None
    try:
        seconds = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("plan-expiry-seconds must be an integer") from None
    if seconds < 0:
        raise ValueError("plan-expiry-seconds must not be negative")
    return seconds or None


def clamp_drift_interval(raw: object) -> int:
    """Clamp to the deployment's configured minimum."""
    from terrapod.config import settings

    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("drift-detection-interval-seconds must be an integer") from None
    return max(value, settings.drift_detection.min_workspace_interval_seconds)


def validate_terragrunt_version(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("terragrunt-version must be a non-empty string")
    cleaned = raw.strip()
    # The column is String(50). Without this the over-long case is a DataError
    # at commit -- a 500 -- rather than the 422 this validator exists to give.
    if len(cleaned) > 50:
        raise ValueError("terragrunt-version must be 50 characters or fewer")
    return cleaned


def validate_slack_channel(raw: object) -> str:
    """Empty means silent for this workspace (#556)."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("slack-channel must be a string")
    return raw.strip()[:MAX_SLACK_CHANNEL]


def validate_bool(raw: object, field: str) -> bool:
    """Type-check rather than coerce.

    `bool("false")` is True, so a JSON string sails through as an enable AND
    writes the string itself into a Boolean column. A caller who typed the
    value wrong gets told, instead of getting the opposite of what they asked
    for (#1301).
    """
    if not isinstance(raw, bool):
        raise ValueError(f"{field} must be true or false, not a string or number")
    return raw


#: Two dimensions now that one token is minted per target, and both want a
#: bound. The target cap limits how many tokens a single run mints — each is an
#: HTTP round trip and a file on disk — and the per-target cap is where the
#: original reasoning lands: a token carrying several audiences is replayable
#: between those targets, so a deployment that allowed unlimited audiences on
#: one target would allow exactly the thing per-target minting exists to stop.
MAX_OIDC_TARGETS = 10
MAX_OIDC_AUDIENCES_PER_TARGET = 10
MAX_OIDC_AUDIENCE_LEN = 255
MAX_OIDC_TARGET_LEN = 128


def validate_oidc_audiences(raw: object) -> dict[str, list[str]]:
    """The workspace's cloud-identity audience OVERRIDE (#1901).

    A map of provider name to that provider's audiences — `aws`, `vault`, or
    `provider.alias` for one aliased configuration (`vault.eu`). One token is
    minted per key, carrying only that key's audiences, because a token
    audienced for two targets is replayable between them and AWS refuses a
    multi-valued `aud` outright.

    **An empty map is valid and is the common case.** It does not mean "mints
    nothing": this is an override *over* the deployment catalogue in
    `api.config.auth.oidc_issuer.audiences`, so an empty map means "take the
    catalogue as it stands". A workspace mints nothing only when the resolved
    merge is empty, and then its runs authenticate with the agent pool's own
    identity exactly as before.

    **An explicitly empty list for a key is REFUSED**, because it has no
    meaning. Removing a key falls back to the catalogue — that is the defined
    way to stop overriding — so an empty list is neither an override nor a
    removal, and accepting it would let an operator believe they had suppressed
    a target when they had not.

    **Stored byte-for-byte after rejecting the unacceptable, never normalised.**
    The Terraform provider writes the server's response back into state, so
    lower-casing or trimming here would make every plan disagree with its own
    apply ("Provider produced inconsistent result after apply") — the defect
    that bit `terrapod_vcs_connection` in v1.9.0. An audience is an opaque
    string the federation target chose; there is nothing to canonicalise even
    if it were safe to.

    Deliberately NOT validated: whether an audience suits the provider it is
    keyed under. Any provider may be mapped to any audience — the cloud-side
    trust policy is the gate, and Terrapod holds no per-cloud knowledge. Nor is
    the same audience under two keys refused: it may be two Vault instances that
    genuinely share a `bound_audiences`, and we cannot tell that from a replay.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            "oidc-audiences must be an object mapping a provider name to its audiences, "
            'e.g. {"aws": ["sts.amazonaws.com"]}'
        )
    if len(raw) > MAX_OIDC_TARGETS:
        raise ValueError(f"oidc-audiences accepts at most {MAX_OIDC_TARGETS} providers")

    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError("every oidc-audiences key must be a provider name")
        if not key.strip():
            raise ValueError("oidc-audiences provider names cannot be blank")
        if len(key) > MAX_OIDC_TARGET_LEN:
            raise ValueError(
                f"oidc-audiences provider names must be {MAX_OIDC_TARGET_LEN} characters or fewer"
            )
        # Conservative rather than a grammar: a provider name is whatever the
        # configuration calls it, so inventing a pattern risks refusing a
        # legitimate key. These three catch the mistakes without guessing —
        # whitespace is never part of a provider name, and `provider.alias` has
        # exactly one dot, so a second is a typo rather than a deeper namespace.
        if any(c.isspace() for c in key):
            raise ValueError(f"oidc-audiences provider name {key!r} cannot contain whitespace")
        if key.count(".") > 1:
            raise ValueError(
                f"oidc-audiences provider name {key!r} has more than one '.' — the form is "
                "'provider' or 'provider.alias'"
            )
        if key.startswith(".") or key.endswith("."):
            raise ValueError(f"oidc-audiences provider name {key!r} cannot start or end with '.'")
        # The one place the conservative-not-a-grammar stance above does not
        # hold: this key becomes a DIRECTORY NAME under the runner's token dir.
        unsafe = unsafe_target_reason(key)
        if unsafe:
            raise ValueError(f"oidc-audiences provider name {key!r} {unsafe}")

        if not isinstance(value, list):
            raise ValueError(f"oidc-audiences[{key!r}] must be a list of audience strings")
        if not value:
            raise ValueError(
                f"oidc-audiences[{key!r}] cannot be an empty list — remove the key instead, "
                "which falls back to the deployment's configured audiences for that provider"
            )
        if len(value) > MAX_OIDC_AUDIENCES_PER_TARGET:
            raise ValueError(
                f"oidc-audiences[{key!r}] accepts at most {MAX_OIDC_AUDIENCES_PER_TARGET} audiences"
            )

        entries: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError(f"every oidc-audiences[{key!r}] entry must be a string")
            if not entry.strip():
                # Refused rather than dropped. A blank entry is a mistake, and a
                # silently-dropped one means an operator who believes they
                # granted an audience did not.
                raise ValueError(f"oidc-audiences[{key!r}] entries cannot be blank")
            if len(entry) > MAX_OIDC_AUDIENCE_LEN:
                raise ValueError(
                    f"oidc-audiences[{key!r}] entries must be "
                    f"{MAX_OIDC_AUDIENCE_LEN} characters or fewer"
                )
            if entry in entries:
                raise ValueError(f"oidc-audiences[{key!r}] contains {entry!r} twice")
            entries.append(entry)
        out[key] = entries
    return out
