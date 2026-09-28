"""A Pulumi workspace's state views show its resources and outputs (#1568).

The issue said these views "render empty" for Pulumi. They do not — they
**404**, because `state_graph_service` resolved the workspace through
`tfe_v2._get_workspace_by_id`, which applies `_engine_filter` and so serves
Terraform only. Correct on the TFE-compatible surface, wrong for a native
Terrapod view, and the reason a Pulumi user met an error rather than a blank.

The graph builder returns the same shape as Terraform's, deliberately: one
component renders it, and a second shape would mean a second renderer to keep
in step.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.state_graph_service import (
    build_graph_from_deployment,
    stack_outputs,
    terraform_outputs,
)

STACK_URN = "urn:pulumi:dev::shop::pulumi:pulumi:Stack::shop-dev"
VPC = "urn:pulumi:dev::shop::aws:ec2/vpc:Vpc::main"
SUBNET = "urn:pulumi:dev::shop::aws:ec2/subnet:Subnet::web"
GROUP = "urn:pulumi:dev::shop::my:mod:Group::net"


def _deployment(resources: list[dict]) -> dict:
    return {"manifest": {"time": "2026-09-28T00:00:00Z"}, "resources": resources}


def _stack(outputs: dict | None = None) -> dict:
    return {"urn": STACK_URN, "type": "pulumi:pulumi:Stack", "outputs": outputs or {}}


class TestTheGraphIsBuiltFromURNs:
    def test_each_resource_becomes_a_node_addressed_by_its_urn(self) -> None:
        """The URN is the id because it is what `dependencies` and `parent`
        reference — the same reason Terraform's resource address is the id."""
        graph = build_graph_from_deployment(
            _deployment(
                [
                    _stack(),
                    {"urn": VPC, "type": "aws:ec2/vpc:Vpc", "custom": True},
                ]
            )
        )
        assert [n["id"] for n in graph["nodes"]] == [VPC]
        assert graph["nodes"][0]["name"] == "main"
        assert graph["nodes"][0]["type"] == "aws:ec2/vpc:Vpc"

    def test_the_root_stack_is_not_drawn(self) -> None:
        """It is the stack itself rather than infrastructure, and it is the
        parent of everything — so drawing it adds one node that every other
        node points at, which says nothing and makes the layout a hub."""
        graph = build_graph_from_deployment(_deployment([_stack()]))
        assert graph["nodes"] == []

    def test_the_provider_comes_from_the_type(self) -> None:
        """Not from the `provider` URN, which is absent on component resources
        and on anything using the default provider implicitly."""
        graph = build_graph_from_deployment(
            _deployment([{"urn": VPC, "type": "aws:ec2/vpc:Vpc", "custom": True}])
        )
        assert graph["nodes"][0]["provider"] == "aws"

    def test_a_component_is_distinguished_from_a_real_resource(self) -> None:
        """Pulumi's own custom/component split, which is the nearest true thing
        to Terraform's managed/data rather than a pretence of it."""
        graph = build_graph_from_deployment(
            _deployment(
                [
                    {"urn": VPC, "type": "aws:ec2/vpc:Vpc", "custom": True},
                    {"urn": GROUP, "type": "my:mod:Group"},
                ]
            )
        )
        modes = {n["id"]: n["mode"] for n in graph["nodes"]}
        assert modes[VPC] == "managed"
        assert modes[GROUP] == "component"

    def test_dependencies_become_edges(self) -> None:
        graph = build_graph_from_deployment(
            _deployment(
                [
                    {"urn": VPC, "type": "aws:ec2/vpc:Vpc", "custom": True},
                    {
                        "urn": SUBNET,
                        "type": "aws:ec2/subnet:Subnet",
                        "custom": True,
                        "dependencies": [VPC],
                    },
                ]
            )
        )
        assert graph["edges"] == [{"source": SUBNET, "target": VPC, "kind": "depends-on"}]
        indeg = {n["id"]: n["indeg"] for n in graph["nodes"]}
        assert indeg[VPC] == 1

    def test_a_parent_that_is_a_real_resource_is_an_edge(self) -> None:
        graph = build_graph_from_deployment(
            _deployment(
                [
                    {"urn": GROUP, "type": "my:mod:Group"},
                    {
                        "urn": SUBNET,
                        "type": "aws:ec2/subnet:Subnet",
                        "custom": True,
                        "parent": GROUP,
                    },
                ]
            )
        )
        assert {"source": SUBNET, "target": GROUP, "kind": "depends-on"} in graph["edges"]

    def test_the_parent_edge_to_the_root_stack_is_not(self) -> None:
        """Every resource parents to the root stack, so keeping that edge would
        connect the whole graph to one point and tell the reader nothing."""
        graph = build_graph_from_deployment(
            _deployment(
                [
                    _stack(),
                    {
                        "urn": VPC,
                        "type": "aws:ec2/vpc:Vpc",
                        "custom": True,
                        "parent": STACK_URN,
                    },
                ]
            )
        )
        assert graph["edges"] == []

    def test_a_dependency_on_something_absent_is_dropped(self) -> None:
        graph = build_graph_from_deployment(
            _deployment(
                [
                    {
                        "urn": SUBNET,
                        "type": "aws:ec2/subnet:Subnet",
                        "custom": True,
                        "dependencies": ["urn:pulumi:dev::shop::aws:ec2/vpc:Vpc::gone"],
                    }
                ]
            )
        )
        assert graph["edges"] == []

    def test_an_empty_or_malformed_deployment_is_an_empty_graph(self) -> None:
        for doc in ({}, {"resources": None}, {"resources": ["junk"]}):
            assert build_graph_from_deployment(doc)["nodes"] == []

    def test_it_truncates_rather_than_returning_an_unusable_payload(self) -> None:
        from terrapod.services.state_graph_service import MAX_NODES

        many = [
            {"urn": f"urn:pulumi:dev::shop::aws:ec2/vpc:Vpc::v{i}", "type": "a:b:C", "custom": True}
            for i in range(MAX_NODES + 25)
        ]
        graph = build_graph_from_deployment(_deployment(many))
        assert len(graph["nodes"]) == MAX_NODES
        assert graph["meta"]["truncated"] is True
        assert graph["meta"]["total_resources"] == MAX_NODES + 25

    def test_the_shape_matches_the_terraform_builders(self) -> None:
        """One renderer draws both, so the two builders must agree on shape."""
        from terrapod.services.state_graph_service import build_graph_from_state

        pulumi = build_graph_from_deployment(
            _deployment([{"urn": VPC, "type": "aws:ec2/vpc:Vpc", "custom": True}])
        )
        terraform = build_graph_from_state(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "aws_vpc",
                        "name": "main",
                        "instances": [{}],
                    }
                ]
            }
        )
        assert pulumi.keys() == terraform.keys()
        assert pulumi["nodes"][0].keys() == terraform["nodes"][0].keys()
        assert pulumi["meta"].keys() == terraform["meta"].keys()


class TestStackOutputs:
    def test_they_come_from_the_root_stack_resource(self) -> None:
        assert stack_outputs(
            _deployment([_stack({"url": "https://shop.example", "port": 443})])
        ) == {"url": "https://shop.example", "port": 443}

    def test_a_secret_is_reported_as_present_but_not_revealed(self) -> None:
        """The same bargain Terrapod strikes everywhere else with a sensitive
        value: the reader learns the output exists, and `state:read` does not
        quietly become a way to read secrets."""
        from terrapod.services.pulumi_state_service import SECRET_SIG, SECRET_SIG_KEY

        sealed = {SECRET_SIG_KEY: SECRET_SIG, "ciphertext": "v1:abc:def"}
        out = stack_outputs(_deployment([_stack({"password": sealed, "open": "fine"})]))
        assert out["password"] == "(sensitive)"
        assert "v1:abc:def" not in str(out)
        assert out["open"] == "fine"

    def test_a_deployment_with_no_stack_resource_has_no_outputs(self) -> None:
        assert stack_outputs(_deployment([{"urn": VPC, "type": "aws:ec2/vpc:Vpc"}])) == {}

    def test_malformed_outputs_are_not_a_crash(self) -> None:
        assert stack_outputs(_deployment([{"urn": STACK_URN, "type": "pulumi:pulumi:Stack"}])) == {}


class TestTerraformOutputs:
    def test_the_value_is_unwrapped(self) -> None:
        assert terraform_outputs({"outputs": {"vpc_id": {"value": "vpc-1", "type": "string"}}}) == {
            "vpc_id": "vpc-1"
        }

    def test_a_sensitive_output_is_masked(self) -> None:
        out = terraform_outputs({"outputs": {"pw": {"value": "hunter2", "sensitive": True}}})
        assert out["pw"] == "(sensitive)"
        assert "hunter2" not in str(out)

    def test_no_outputs_is_empty_not_an_error(self) -> None:
        assert terraform_outputs({}) == {}


class TestTheRightBuilderIsUsed:
    """The dispatch that makes any of this reachable.

    A Pulumi deployment put through the Terraform builder does not raise: it
    finds no `mode`/`name`/`instances`, skips every resource, and returns an
    empty graph. A silent wrong answer, which is why the engine decides.
    """

    def test_a_pulumi_deployment_through_the_terraform_builder_is_silently_empty(self) -> None:
        from terrapod.services.state_graph_service import build_graph_from_state

        assert (
            build_graph_from_state(
                _deployment([{"urn": VPC, "type": "aws:ec2/vpc:Vpc", "custom": True}])
            )["nodes"]
            == []
        )

    async def test_the_lookup_is_engine_agnostic(self) -> None:
        """Not `tfe_v2._get_workspace_by_id`, which pins to Terraform and is why
        this endpoint 404'd for every Pulumi workspace."""
        import inspect

        from terrapod.services import state_graph_service

        src = inspect.getsource(state_graph_service.derive_state_graph)
        # The CALL, not a mention: the comment above it names the function it
        # replaced, which a bare substring check would match.
        assert "await _native_workspace(" in src
        assert "await _get_workspace_by_id(" not in src


class TestResourceCountsAreRecorded:
    def test_a_deployment_is_counted_when_written(self) -> None:
        from terrapod.services.pulumi_checkpoint_service import _resource_count

        assert _resource_count(_deployment([_stack(), {"urn": VPC, "type": "a:b:C"}])) == 2

    def test_an_uncounted_document_is_none_not_zero(self) -> None:
        """NULL means "not counted" — every version written before this, and
        every Terraform one. A counted zero is a different fact, and reporting
        the two alike is what the hard-coded `resourceCount: 0` did."""
        from terrapod.services.pulumi_checkpoint_service import _resource_count

        assert _resource_count(None) is None
        assert _resource_count({"no": "resources"}) is None
        assert _resource_count(_deployment([])) == 0


class TestListStacksReportsRealCounts:
    async def test_the_count_comes_from_the_newest_state_version(self) -> None:
        import uuid

        from terrapod.api.routers.pulumi_service import _latest_resource_counts

        ws_id = uuid.uuid4()
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = [(ws_id, 7)]
        db.execute.return_value = result
        assert await _latest_resource_counts(db, [ws_id]) == {ws_id: 7}

    async def test_no_workspaces_asks_nothing(self) -> None:
        from terrapod.api.routers.pulumi_service import _latest_resource_counts

        db = AsyncMock()
        assert await _latest_resource_counts(db, []) == {}
        db.execute.assert_not_awaited()


pytestmark = pytest.mark.asyncio
