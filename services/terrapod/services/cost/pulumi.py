"""Pricing a Pulumi preview: Pulumi's vocabulary, the cost engine's shape (#1569).

A Pulumi run produced no cost estimate at all. The engine reads
``terraform show -json``, and a preview differs from one in three ways at once:

1. **A type is a token.** ``aws:ec2/instance:Instance``, not ``aws_instance``.
2. **A property is camelCase.** Pulumi's bridged providers rename every
   Terraform attribute on the way through (``instance_type`` ->
   ``instanceType``), and every match set in the pricesheet is written in
   Terraform's names.
3. **A change is a step**, not a difference between a prior tree and a planned
   one.

This module translates those three and nothing else. What it will not do is
guess: a type this table does not name is carried through **unpriced**, with its
Pulumi token intact, so the estimate says "I did not price this" rather than
pricing the wrong thing. That is the issue's own instruction, and it is the
right one -- a mapping is not derivable. ``aws:ec2/instance:Instance`` is
``aws_instance``, but ``aws:rds/instance:Instance`` is ``aws_db_instance`` and
``aws:s3/bucket:Bucket`` is ``aws_s3_bucket``. Any rule that gets the first also
gets the second wrong, and a wrongly-priced resource is worse than an unpriced
one because nothing about it looks wrong.

**Only bridged providers are mapped, and that is a deliberate line.** A bridged
provider is a Terraform provider wrapped by ``pulumi-terraform-bridge``, so its
resource has the same properties under renamed keys -- which is exactly what
makes a mapping meaningful. ``pulumi-azure-native`` is not bridged: it is
generated from the Azure ARM specification and its ``azure-native:compute:
VirtualMachine`` is a different shape from ``azurerm_linux_virtual_machine``,
sharing neither property names nor structure. Mapping it would satisfy the type
check and then match on nothing, or worse, on the wrong thing. It is left
unmapped, and an ``azure-native:`` stack reports its resources as unpriced.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# ── The type table ────────────────────────────────────────────────────

#: Pulumi resource token -> the Terraform type the pricesheet knows it by.
#:
#: Bounded by what can actually be priced: every key here has a recipe under
#: ``pricegen/providers/*/recipes/``. A type with no recipe gains nothing from
#: being mapped -- it would match no product and land in the unpriced bucket
#: either way -- so the table stays the size of the thing it serves.
#:
#: Every entry was taken from the provider's own token, not inferred from the
#: Terraform name. Where the token was not certain it was left out; an absent
#: mapping means "unpriced", which is the outcome the issue asks for.
TERRAFORM_TYPES: dict[str, str] = {
    # ── AWS (pulumi-aws, bridged from hashicorp/aws) ──────────────────
    "aws:ec2/instance:Instance": "aws_instance",
    "aws:ebs/volume:Volume": "aws_ebs_volume",
    "aws:ebs/snapshot:Snapshot": "aws_ebs_snapshot",
    "aws:ec2/eip:Eip": "aws_eip",
    "aws:ec2/natGateway:NatGateway": "aws_nat_gateway",
    "aws:ec2/vpcEndpoint:VpcEndpoint": "aws_vpc_endpoint",
    "aws:rds/instance:Instance": "aws_db_instance",
    "aws:rds/clusterInstance:ClusterInstance": "aws_rds_cluster_instance",
    "aws:elasticache/cluster:Cluster": "aws_elasticache_cluster",
    "aws:dynamodb/table:Table": "aws_dynamodb_table",
    "aws:s3/bucket:Bucket": "aws_s3_bucket",
    "aws:efs/fileSystem:FileSystem": "aws_efs_file_system",
    "aws:ecr/repository:Repository": "aws_ecr_repository",
    "aws:lambda/function:Function": "aws_lambda_function",
    "aws:sns/topic:Topic": "aws_sns_topic",
    "aws:sqs/queue:Queue": "aws_sqs_queue",
    "aws:kinesis/stream:Stream": "aws_kinesis_stream",
    "aws:kms/key:Key": "aws_kms_key",
    "aws:secretsmanager/secret:Secret": "aws_secretsmanager_secret",
    "aws:route53/zone:Zone": "aws_route53_zone",
    "aws:cloudfront/distribution:Distribution": "aws_cloudfront_distribution",
    "aws:apigateway/restApi:RestApi": "aws_api_gateway_rest_api",
    "aws:apigatewayv2/api:Api": "aws_apigatewayv2_api",
    # The v2 load balancer (`aws_lb`) is reachable under two module names in
    # pulumi-aws: `lb` is the current one and `alb` is the historical alias
    # kept for compatibility. Both are the same resource, so both map.
    "aws:lb/loadBalancer:LoadBalancer": "aws_lb",
    "aws:alb/loadBalancer:LoadBalancer": "aws_lb",
    # The classic load balancer (`aws_elb`) is a different resource, priced by
    # a different recipe. Its token lives in its own module.
    "aws:elb/loadBalancer:LoadBalancer": "aws_elb",
    # ── Azure (pulumi-azure, bridged from hashicorp/azurerm) ──────────
    # NOT pulumi-azure-native, which is generated from ARM rather than bridged
    # and shares no property shape with azurerm -- see the module docstring.
    "azure:compute/linuxVirtualMachine:LinuxVirtualMachine": "azurerm_linux_virtual_machine",
    "azure:compute/windowsVirtualMachine:WindowsVirtualMachine": "azurerm_windows_virtual_machine",
    "azure:compute/managedDisk:ManagedDisk": "azurerm_managed_disk",
    "azure:network/publicIp:PublicIp": "azurerm_public_ip",
    "azure:storage/account:Account": "azurerm_storage_account",
    "azure:containerservice/registry:Registry": "azurerm_container_registry",
    "azure:containerservice/kubernetesCluster:KubernetesCluster": "azurerm_kubernetes_cluster",
    "azure:postgresql/flexibleServer:FlexibleServer": "azurerm_postgresql_flexible_server",
    # ── GCP (pulumi-gcp, bridged from hashicorp/google) ───────────────
    "gcp:compute/instance:Instance": "google_compute_instance",
    "gcp:compute/disk:Disk": "google_compute_disk",
    "gcp:compute/address:Address": "google_compute_address",
    "gcp:container/cluster:Cluster": "google_container_cluster",
    "gcp:sql/databaseInstance:DatabaseInstance": "google_sql_database_instance",
    "gcp:storage/bucket:Bucket": "google_storage_bucket",
    "gcp:dns/managedZone:ManagedZone": "google_dns_managed_zone",
    "gcp:pubsub/topic:Topic": "google_pubsub_topic",
}


def terraform_type(token: str) -> str | None:
    """The Terraform type a Pulumi token prices as, or None if it does not.

    None is a real answer, not a failure: it routes the resource to the
    estimate's unpriced bucket with its own token shown, which is what tells an
    operator their estimate is partial and what it is missing.
    """
    return TERRAFORM_TYPES.get(token)


# ── Property names ────────────────────────────────────────────────────

#: Where a camelCase word begins: a lower-case letter or digit followed by an
#: upper-case one. Deliberately not `(?<!^)(?=[A-Z])`, which would split a run
#: of capitals letter by letter.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def snake_case(name: str) -> str:
    """A Pulumi property name as Terraform spells it.

    ``instanceType`` -> ``instance_type``; ``vpcId`` -> ``vpc_id``;
    ``ipv6AddressCount`` -> ``ipv6_address_count``.

    **Idempotent on a name that is already snake_case** -- there is no
    boundary to find in ``instance_type``, so it comes back unchanged. That
    matters more than it looks: it means this is safe to apply whether the
    document was written by a bridged provider (camelCase) or by something
    that already speaks Terraform's names, and one code path serves both
    rather than a guess about which arrived.
    """
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _terraform_names(value: Any) -> Any:
    """Rewrite every key of a nested value into Terraform's spelling.

    Recurses through dicts and lists because a match set is built from the
    flattened tree -- ``default_node_pool.vm_size`` is as much a match key as
    ``instance_type``, and a nested block renamed on only its outer key matches
    nothing.

    User-controlled maps (a resource's ``tags``) have their keys rewritten too,
    which is meaningless but harmless: no pricing recipe matches on a tag, and
    telling a tag map from a property map would mean carrying a schema this
    module deliberately does not have.
    """
    if isinstance(value, dict):
        return {snake_case(str(k)): _terraform_names(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_terraform_names(v) for v in value]
    return value


# ── Steps as a diff ───────────────────────────────────────────────────

#: Which side of the diff each Pulumi step operation puts a resource on, as
#: ``(in_prior, in_planned)``. The cost engine reads a plan as two trees and
#: labels what is only in the planned one ``add``, only in the prior one
#: ``remove``, and in both ``noop`` -- so this table is the whole translation
#: from Pulumi's verbs to that shape.
#:
#: **A replacement is in both, and that is parity rather than an approximation.**
#: A replaced resource exists before the run and after it, so its monthly cost
#: is unchanged; a Terraform plan says the same thing by carrying the same
#: address in ``prior_state`` and ``planned_values``. The engine emits a
#: replacement as up to three steps (``create-replacement``, ``replace``,
#: ``delete-replaced``) for one URN, and because the sides are merged per URN
#: the first and last union to exactly "in both" without needing to be
#: recognised as a pair.
_OP_SIDES: dict[str, tuple[bool, bool]] = {
    "same": (True, True),
    "update": (True, True),
    "replace": (True, True),
    "refresh": (True, True),
    "read": (True, True),
    "read-replacement": (True, True),
    "create": (False, True),
    "create-replacement": (False, True),
    "import": (False, True),
    "import-replacement": (False, True),
    "delete": (True, False),
    "delete-replaced": (True, False),
    "discard": (True, False),
    "discard-replaced": (True, False),
    "remove-pending-replace": (True, False),
}

#: An operation a newer Pulumi invented reads as "in both": the resource is
#: still counted in the total, and the delta does not move on a verb whose
#: direction nobody here can vouch for. Inventing a direction would put a made-up
#: number in front of the one line of the estimate people read.
_UNKNOWN_OP_SIDES = (True, True)

#: Pulumi's own namespace: the stack itself, provider instances, stack
#: references. Never a billable resource, and excluding them keeps the unpriced
#: bucket a list of things an operator could act on.
_BUILTIN_PREFIX = "pulumi:"


def resource_name(urn: str) -> str:
    """The resource's own name: everything after the third ``::`` in its URN.

    Split from the LEFT on a fixed count, because a resource name may itself
    contain ``::`` and Pulumi's own ``URN.Name()`` rejoins everything from the
    fourth component onward for that reason.

    The same rule as ``runner.phases.pulumi_preview._resource_name``, and
    deliberately a second copy rather than an import: the cost engine is used by
    the API, which ships no runner package, so a dependency in that direction
    would be a crash-looping import rather than a red test.
    """
    parts = urn.split("::", 3)
    return parts[3] if len(parts) == 4 else ""


def _state(metadata: dict[str, Any], *keys: str) -> dict[str, Any]:
    """The first of ``keys`` (``new``/``old``) carrying a state object."""
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _values(state: dict[str, Any]) -> dict[str, Any]:
    """A resource's priceable attributes, in Terraform's spelling.

    ``inputs`` is what the program declared, the analogue of a plan's
    ``change.after``. ``outputs`` is the provider's full returned state and is
    merged UNDERNEATH, because a preview's inputs carry only what was written
    down: an ``aws:ebs/volume:Volume`` with no explicit ``type`` is a gp3 volume
    that says so nowhere in its inputs, and gp3 is what it will be billed as.
    Inputs win on conflict -- they are what this run is proposing.

    Both sides are already redacted by the time they reach the log: Pulumi's
    engine replaces every property marked secret with the literal ``"[secret]"``
    before writing an event, unless the CLI was given ``--show-secrets``, which
    a preview never is. A redacted value simply matches no product.
    """
    merged: dict[str, Any] = {}
    outputs = state.get("outputs")
    if isinstance(outputs, dict):
        merged.update(outputs)
    inputs = state.get("inputs")
    if isinstance(inputs, dict):
        merged.update(inputs)
    return _terraform_names(merged)


def _resource(urn: str, rtype: str, values: dict[str, Any]) -> dict[str, Any]:
    """One resource in the shape ``terrapod.services.cost.tf`` reads.

    The address is the URN. Terraform's address is ``aws_instance.web``; Pulumi's
    identity is the URN, it is unique, and it is what the diff keys on -- so it
    is what an operator is shown against a line of the estimate.
    """
    return {
        "address": urn,
        "type": rtype,
        "name": resource_name(urn),
        "mode": "managed",
        "values": values,
    }


def plan_json(steps: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """A preview's resource steps as a document the cost engine can read.

    ``steps`` are the ``resourcePreEvent.metadata`` objects from the engine's
    event log, in order. The result is shaped like ``terraform show -json`` of a
    plan -- ``planned_values`` against ``prior_state`` -- because that is the one
    input the engine takes, and translating into it is far less code than
    teaching the engine a second dialect.

    Unmapped types are carried with **empty values and their Pulumi token as the
    type**. They match no product, so they arrive in the estimate's unpriced
    bucket named as the operator wrote them -- which is the honest report of a
    partial estimate. Carrying their values instead would achieve nothing except
    to make this document scale with the whole stack.
    """
    prior: dict[str, dict[str, Any]] = {}
    planned: dict[str, dict[str, Any]] = {}

    for metadata in steps:
        if not isinstance(metadata, dict):
            continue
        urn = str(metadata.get("urn") or "")
        if not urn:
            continue
        token = str(metadata.get("type") or "")
        if token.startswith(_BUILTIN_PREFIX):
            continue

        mapped = terraform_type(token)
        if mapped is None:
            # Not priceable. Keep a real provider resource so the unpriced bucket
            # is complete, but drop component resources: a Pulumi program is
            # built out of them, they are never billed, and every one listed as
            # unpriced is a line an operator cannot act on. The mapped check runs
            # FIRST so a priceable resource is never lost to this filter, whatever
            # the engine did or did not say about `custom`.
            if not _state(metadata, "new", "old").get("custom"):
                continue
            rtype, new_values, old_values = token, {}, {}
        else:
            rtype = mapped
            new_values = _values(_state(metadata, "new", "old"))
            old_values = _values(_state(metadata, "old", "new"))

        op = str(metadata.get("op") or "")
        in_prior, in_planned = _OP_SIDES.get(op, _UNKNOWN_OP_SIDES)
        if in_planned:
            planned[urn] = _resource(urn, rtype, new_values)
        if in_prior:
            prior[urn] = _resource(urn, rtype, old_values)

    return {
        # Named for what it is rather than borrowed from a Terraform release:
        # nothing downstream reads it, and claiming a Terraform format version
        # this document does not implement would be the wrong kind of accurate.
        "format_version": "1.0",
        "planned_values": {"root_module": {"resources": list(planned.values())}},
        "prior_state": {"values": {"root_module": {"resources": list(prior.values())}}},
    }
