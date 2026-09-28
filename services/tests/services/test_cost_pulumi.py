"""Pricing a Pulumi preview: the type table and the translation (#1569).

A Pulumi run produced no cost estimate. These cover the two things that had to
be true for one to exist and to be trustworthy: that a mapped resource reaches
the pricesheet's vocabulary intact, and that an unmapped one is reported as
UNPRICED rather than guessed at.

**The fixtures are shaped from Pulumi's own event types**, the same source
`test_pulumi_policy_input.py` used -- `apitype.StepEventMetadata` and
`StepEventStateMetadata` -- because the whole design turns on facts about them:
that a delete carries `old` and no `new`, that state is a property map in the
bridge's camelCase spelling, and that a replacement arrives as several steps
for one URN.
"""

from __future__ import annotations

import io
import json

from terrapod.services.cost import estimate
from terrapod.services.cost.pulumi import (
    TERRAFORM_TYPES,
    plan_json,
    resource_name,
    snake_case,
    terraform_type,
)

URN = "urn:pulumi:dev::shop::aws:ec2/instance:Instance::web"


def _step(op="create", urn=URN, type_="aws:ec2/instance:Instance", **state_keys):
    """One `resourcePreEvent.metadata`, in the engine's real shape."""
    metadata = {"op": op, "urn": urn, "type": type_}
    metadata.update(state_keys)
    return metadata


def _state(inputs=None, outputs=None, custom=True):
    return {
        "type": "aws:ec2/instance:Instance",
        "urn": URN,
        "custom": custom,
        "inputs": inputs if inputs is not None else {"instanceType": "t3.micro"},
        "outputs": outputs or {},
    }


def _addresses(doc, side):
    if side == "planned":
        resources = doc["planned_values"]["root_module"]["resources"]
    else:
        resources = doc["prior_state"]["values"]["root_module"]["resources"]
    return {r["address"] for r in resources}


def _by_address(doc, side, address):
    if side == "planned":
        resources = doc["planned_values"]["root_module"]["resources"]
    else:
        resources = doc["prior_state"]["values"]["root_module"]["resources"]
    return next(r for r in resources if r["address"] == address)


class TestTheTypeTable:
    def test_a_mapped_token_becomes_the_type_the_pricesheet_knows(self):
        assert terraform_type("aws:ec2/instance:Instance") == "aws_instance"

    def test_the_table_is_not_a_rule_that_could_have_been_derived(self):
        # The reason this is a table at all. Three tokens that a naive
        # transformation of the last segment gets wrong in three different ways:
        # the module is not the prefix, the resource is renamed, and the Pulumi
        # name is shorter than the Terraform one.
        assert terraform_type("aws:ec2/instance:Instance") == "aws_instance"
        assert terraform_type("aws:rds/instance:Instance") == "aws_db_instance"
        assert terraform_type("aws:s3/bucket:Bucket") == "aws_s3_bucket"

    def test_an_unmapped_token_says_so_rather_than_guessing(self):
        # The issue's own instruction: leave the rest unpriced. None is the
        # answer that routes a resource to the unpriced bucket.
        assert terraform_type("aws:ecs/service:Service") is None
        assert terraform_type("aws:iam/role:Role") is None

    def test_azure_native_is_not_mapped_because_it_is_not_a_bridged_provider(self):
        # azure-native is generated from ARM, not bridged from azurerm: its
        # resource shares neither property names nor structure with the
        # Terraform type, so a mapping would type-check and then price nothing
        # -- or worse, the wrong thing.
        assert terraform_type("azure-native:compute:VirtualMachine") is None
        assert not any(t.startswith("azure-native:") for t in TERRAFORM_TYPES)

    def test_every_token_is_a_pulumi_token_and_every_target_a_terraform_type(self):
        for token, tf_type in TERRAFORM_TYPES.items():
            assert token.count(":") == 2 and "/" in token, token
            assert tf_type.islower() and ":" not in tf_type, tf_type


class TestPropertyNames:
    def test_a_bridged_camel_case_name_becomes_terraforms(self):
        # Without this nothing prices at all: a bridged provider renames every
        # Terraform attribute on the way through, and every match set in the
        # pricesheet is written in Terraform's names.
        assert snake_case("instanceType") == "instance_type"
        assert snake_case("ipv6AddressCount") == "ipv6_address_count"

    def test_a_name_already_in_terraforms_spelling_is_unchanged(self):
        # Idempotence is what lets one code path serve both spellings rather
        # than a guess about which arrived.
        assert snake_case("instance_type") == "instance_type"
        assert snake_case("acl") == "acl"

    def test_nested_blocks_are_renamed_all_the_way_down(self):
        # `default_node_pool.vm_size` is as much a match key as `instance_type`,
        # so renaming only the outer key would match nothing.
        doc = plan_json(
            [
                _step(
                    type_="azure:containerservice/kubernetesCluster:KubernetesCluster",
                    urn="urn:pulumi:d::s::azure:containerservice/"
                    "kubernetesCluster:KubernetesCluster::aks",
                    new={
                        "custom": True,
                        "inputs": {"defaultNodePool": {"vmSize": "Standard_D2s_v3"}},
                    },
                )
            ]
        )
        values = doc["planned_values"]["root_module"]["resources"][0]["values"]
        assert values == {"default_node_pool": {"vm_size": "Standard_D2s_v3"}}


class TestWhichSideOfTheDiffAStepLandsOn:
    def test_a_create_is_planned_only(self):
        doc = plan_json([_step(op="create", new=_state())])
        assert _addresses(doc, "planned") == {URN}
        assert _addresses(doc, "prior") == set()

    def test_a_delete_is_prior_only(self):
        # A delete carries `old` and no `new` -- `DeleteStep.New()` returns nil.
        doc = plan_json([_step(op="delete", old=_state())])
        assert _addresses(doc, "planned") == set()
        assert _addresses(doc, "prior") == {URN}

    def test_a_same_is_on_both_sides(self):
        doc = plan_json([_step(op="same", new=_state())])
        assert _addresses(doc, "planned") == _addresses(doc, "prior") == {URN}

    def test_an_update_is_on_both_sides_and_the_planned_side_is_the_new_inputs(self):
        doc = plan_json(
            [
                _step(
                    op="update",
                    old=_state(inputs={"instanceType": "t3.micro"}),
                    new=_state(inputs={"instanceType": "m5.large"}),
                )
            ]
        )
        assert _by_address(doc, "planned", URN)["values"]["instance_type"] == "m5.large"
        assert _by_address(doc, "prior", URN)["values"]["instance_type"] == "t3.micro"

    def test_a_replace_is_on_both_sides(self):
        doc = plan_json([_step(op="replace", old=_state(), new=_state())])
        assert _addresses(doc, "planned") == _addresses(doc, "prior") == {URN}

    def test_a_replacement_split_across_steps_is_still_on_both_sides(self):
        # The engine reports a replacement as up to three steps for one URN.
        # Merging the sides per URN unions them to "in both" -- which is what a
        # Terraform plan says about a replacement too -- without this code ever
        # having to recognise the steps as a set.
        doc = plan_json(
            [
                _step(op="create-replacement", new=_state(inputs={"instanceType": "m5.large"})),
                _step(op="replace", old=_state(), new=_state(inputs={"instanceType": "m5.large"})),
                _step(op="delete-replaced", old=_state()),
            ]
        )
        assert _addresses(doc, "planned") == _addresses(doc, "prior") == {URN}
        assert _by_address(doc, "planned", URN)["values"]["instance_type"] == "m5.large"
        assert _by_address(doc, "prior", URN)["values"]["instance_type"] == "t3.micro"

    def test_an_operation_from_a_newer_pulumi_reads_as_unchanged(self):
        # It is still counted in the total, and the delta does not move on a
        # verb whose direction nobody here can vouch for.
        doc = plan_json([_step(op="teleport", new=_state())])
        assert _addresses(doc, "planned") == _addresses(doc, "prior") == {URN}


class TestWhatIsCarriedAndWhatIsNot:
    def test_an_unmapped_resource_is_carried_with_its_own_token_and_no_values(self):
        # It has to appear, or the estimate silently claims to have priced a
        # stack it only partly understood. It has to keep its Pulumi token, or
        # the operator cannot tell what was missed.
        doc = plan_json(
            [
                _step(
                    op="create",
                    urn="urn:pulumi:d::s::aws:ecs/service:Service::svc",
                    type_="aws:ecs/service:Service",
                    new={"custom": True, "inputs": {"desiredCount": 3}},
                )
            ]
        )
        (res,) = doc["planned_values"]["root_module"]["resources"]
        assert res["type"] == "aws:ecs/service:Service"
        assert res["values"] == {}

    def test_pulumis_own_resources_are_excluded(self):
        # The stack and its provider instances are never billable, and listing
        # them as unpriced gives an operator lines they cannot act on.
        doc = plan_json(
            [
                _step(
                    urn="urn:pulumi:d::s::pulumi:providers:aws::default_6_0_0",
                    type_="pulumi:providers:aws",
                    new={"custom": True},
                ),
                _step(
                    urn="urn:pulumi:d::s::pulumi:pulumi:Stack::s-d",
                    type_="pulumi:pulumi:Stack",
                    new={},
                ),
            ]
        )
        assert _addresses(doc, "planned") == set()

    def test_a_component_resource_is_excluded(self):
        doc = plan_json(
            [_step(urn="urn:pulumi:d::s::awsx:ec2:Vpc::vpc", type_="awsx:ec2:Vpc", new={})]
        )
        assert _addresses(doc, "planned") == set()

    def test_a_priceable_resource_survives_even_when_the_engine_omits_custom(self):
        # The mapped check runs before the component filter deliberately: a
        # resource the pricesheet can price is never dropped over a metadata
        # field, whatever a future engine does or does not emit.
        doc = plan_json([_step(op="create", new={"inputs": {"instanceType": "t3.micro"}})])
        assert _addresses(doc, "planned") == {URN}

    def test_outputs_fill_in_what_the_program_left_unsaid(self):
        # A volume with no explicit type is a gp3 volume that says so nowhere in
        # its inputs -- and gp3 is what it is billed as.
        doc = plan_json(
            [
                _step(
                    urn="urn:pulumi:d::s::aws:ebs/volume:Volume::data",
                    type_="aws:ebs/volume:Volume",
                    new={"custom": True, "inputs": {"size": 100}, "outputs": {"type": "gp3"}},
                )
            ]
        )
        values = doc["planned_values"]["root_module"]["resources"][0]["values"]
        assert values == {"size": 100, "type": "gp3"}

    def test_an_input_beats_an_output_because_it_is_what_the_run_proposes(self):
        doc = plan_json(
            [
                _step(
                    new={
                        "custom": True,
                        "inputs": {"instanceType": "m5.large"},
                        "outputs": {"instanceType": "t3.micro"},
                    }
                )
            ]
        )
        assert _by_address(doc, "planned", URN)["values"]["instance_type"] == "m5.large"

    def test_a_step_without_a_urn_is_dropped(self):
        assert _addresses(plan_json([_step(urn="")]), "planned") == set()

    def test_the_address_is_the_urn_and_the_name_comes_out_of_it(self):
        res = _by_address(plan_json([_step(new=_state())]), "planned", URN)
        assert res["address"] == URN
        assert res["name"] == "web"

    def test_a_name_containing_the_separator_survives(self):
        # Pulumi's own URN.Name() rejoins everything from the fourth component
        # onward, so splitting from the right would return a different resource.
        assert resource_name("urn:pulumi:d::s::aws:ec2/instance:Instance::a::b") == "a::b"

    def test_a_mangled_urn_yields_an_empty_name(self):
        assert resource_name("mangled") == ""

    def test_an_empty_preview_produces_a_valid_empty_document(self):
        doc = plan_json([])
        assert doc["planned_values"]["root_module"]["resources"] == []
        assert doc["prior_state"]["values"]["root_module"]["resources"] == []


# One on-demand t3.micro in us-east-1 @ $0.10/hr x 730h = $73/mo, in the
# Terrapod YAML pricesheet shape pricegen emits.
_SHEET = (
    "schema: terrapod-pricesheet/v1\n"
    "currency: USD\n"
    "products:\n"
    "- service: AmazonEC2\n"
    "  family: Compute\n"
    "  match: type=aws_instance&values.instance_type=t3.micro\n"
    "  pricing: service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1\n"
    "  price: '0.10'\n"
    "  price_type: t\n"
)


class TestThroughTheRealCostEngine:
    """The translation is only worth anything if the engine prices what comes
    out of it, so these drive the real engine rather than asserting on shape."""

    def _estimate(self, steps):
        return estimate(plan_json(steps), pricesheet=io.StringIO(_SHEET)).to_dict()

    def test_a_mapped_resource_is_priced(self):
        result = self._estimate([_step(op="create", new=_state())])
        assert result["resources"][0]["monthly"]["min"] == 73.0
        assert result["resources"][0]["change"] == "add"
        assert result["diff"]["min"] == 73.0

    def test_a_deletion_reads_as_a_saving(self):
        result = self._estimate([_step(op="delete", old=_state())])
        assert result["diff"]["min"] == -73.0

    def test_a_replacement_costs_nothing_extra(self):
        # It exists before the run and after it, so the monthly bill does not
        # move -- which is exactly what a Terraform plan says by carrying the
        # same address on both sides.
        result = self._estimate(
            [
                _step(op="create-replacement", new=_state()),
                _step(op="delete-replaced", old=_state()),
            ]
        )
        assert result["diff"] == {"min": 0.0, "max": 0.0}
        assert result["total"]["min"] == 73.0

    def test_an_unmapped_resource_lands_in_the_unpriced_bucket_not_the_total(self):
        result = self._estimate(
            [
                _step(op="create", new=_state()),
                _step(
                    op="create",
                    urn="urn:pulumi:d::s::aws:ecs/service:Service::svc",
                    type_="aws:ecs/service:Service",
                    new={"custom": True, "inputs": {"desiredCount": 3}},
                ),
            ]
        )
        assert result["total"]["min"] == 73.0
        assert result["unpriced"] == [
            {
                "address": "urn:pulumi:d::s::aws:ecs/service:Service::svc",
                "type": "aws:ecs/service:Service",
                "change": "add",
            }
        ]

    def test_a_preview_with_nothing_priceable_is_an_empty_estimate_not_an_error(self):
        result = self._estimate(
            [
                _step(
                    op="create",
                    urn="urn:pulumi:d::s::aws:iam/role:Role::r",
                    type_="aws:iam/role:Role",
                    new={"custom": True, "inputs": {"name": "r"}},
                )
            ]
        )
        assert result["total"] == {"min": 0.0, "max": 0.0}
        assert result["resources"] == []
        assert len(result["unpriced"]) == 1

    def test_an_empty_preview_is_an_empty_estimate_not_an_error(self):
        result = self._estimate([])
        assert result["total"] == {"min": 0.0, "max": 0.0}
        assert result["resources"] == [] and result["unpriced"] == []

    def test_the_document_is_recognised_as_a_plan_and_not_as_state(self):
        # `detect_type` answers "unknown" when both `planned_values` and a
        # top-level `values` are present, and "state" for the latter alone --
        # either would silently label every resource `noop` and flatten the
        # delta to zero.
        from terrapod.services.cost.tf import detect_type

        assert detect_type(plan_json([_step(new=_state())])) == "plan"

    def test_the_translated_document_is_json_serialisable(self):
        # It is handed to the engine in-process, but a document that cannot be
        # written out cannot be reproduced from a bug report either.
        json.dumps(plan_json([_step(new=_state())]))
