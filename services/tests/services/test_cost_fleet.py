"""End-to-end fleet/group cost resolution (#1029).

A fleet resource bills as N units whose priceable shape lives on a nested block
or a referenced resource. These drive real plans through ``estimate`` against a
small sheet and assert each fleet prices as ``unit × count`` — by pricing a
standalone unit of the same type in the same run and comparing, so the assertion
never depends on the hours-per-month constant.
"""

from __future__ import annotations

import io

from terrapod.services.cost.engine import estimate

# m5.large $0.096/hr, Standard_D2s_v5 $0.096/hr, n2-standard-4 $0.194236/hr.
_SHEET = "\n".join(
    [
        "service,product_family,match_set,pricing_match_set,price,price_type,ccy",
        "AmazonEC2,Compute Instance,type=aws_instance&values.instance_type=m5.large,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&os=linux&start_usage_amount=0&end_usage_amount=Inf,0.0960000000,t,USD",
        "Virtual Machines,Virtual Machines,type=azurerm_linux_virtual_machine&values.size=Standard_D2s_v5,service_provider=azure&purchase_option=on_demand&region=eastus&service_class=instance&os=linux&start_usage_amount=0&end_usage_amount=Inf,0.096,t,USD",
        "Compute Engine,Compute Instance,type=google_compute_instance&values.machine_type=n2-standard-4,service_provider=gcp&purchase_option=on_demand&region=us-central1&service_class=instance&start_usage_amount=0&end_usage_amount=Inf,0.1942360000,t,USD",
    ]
)


def _r(rtype, resname, **values):
    # `resname` is the TF resource name; a `name` **values attr (e.g. a launch
    # template's own name) is distinct and passes through into values.
    return {
        "address": f"{rtype}.{resname}",
        "type": rtype,
        "name": resname,
        "values": values,
    }


def _state(*resources):
    return {"values": {"root_module": {"resources": list(resources)}}}


def _run(*resources):
    est = estimate(_state(*resources), io.StringIO(_SHEET))
    by = {r.address: r for r in est.resources}
    unpriced = {u.address for u in est.unpriced}
    return by, unpriced


def _cost(by, addr):
    return by[addr].monthly_max


def test_asg_prices_as_launch_template_instance_times_capacity():
    by, unpriced = _run(
        _r("aws_instance", "solo", instance_type="m5.large", region="us-east-1"),
        _r("aws_launch_template", "web", id="lt-1", instance_type="m5.large"),
        _r(
            "aws_autoscaling_group",
            "web",
            region="us-east-1",
            desired_capacity=3,
            launch_template=[{"id": "lt-1"}],
        ),
    )
    assert "aws_autoscaling_group.web" in by
    assert _cost(by, "aws_autoscaling_group.web") == _cost(by, "aws_instance.solo") * 3
    # the launch template itself is not a separate priced line
    assert "aws_launch_template.web" not in by


def test_asg_launch_template_by_name():
    by, _ = _run(
        _r("aws_instance", "solo", instance_type="m5.large", region="us-east-1"),
        _r("aws_launch_template", "web", name="web-lt", instance_type="m5.large"),
        _r(
            "aws_autoscaling_group",
            "web",
            region="us-east-1",
            desired_capacity=2,
            launch_template=[{"name": "web-lt"}],
        ),
    )
    assert _cost(by, "aws_autoscaling_group.web") == _cost(by, "aws_instance.solo") * 2


def test_asg_mixed_instances_policy_splits_capacity():
    # 4 capacity split across 2 override types → 2 each.
    by, _ = _run(
        _r("aws_instance", "solo", instance_type="m5.large", region="us-east-1"),
        _r(
            "aws_autoscaling_group",
            "mix",
            region="us-east-1",
            desired_capacity=4,
            mixed_instances_policy=[
                {
                    "launch_template": [
                        {"override": [{"instance_type": "m5.large"}, {"instance_type": "m5.large"}]}
                    ]
                }
            ],
        ),
    )
    # both overrides are m5.large → 2 + 2 = 4 units total
    assert _cost(by, "aws_autoscaling_group.mix") == _cost(by, "aws_instance.solo") * 4


def test_eks_node_group_self_contained():
    by, _ = _run(
        _r("aws_instance", "solo", instance_type="m5.large", region="us-east-1"),
        _r(
            "aws_eks_node_group",
            "ng",
            region="us-east-1",
            instance_types=["m5.large"],
            scaling_config=[{"desired_size": 5}],
        ),
    )
    assert _cost(by, "aws_eks_node_group.ng") == _cost(by, "aws_instance.solo") * 5


def test_aks_default_node_pool():
    by, _ = _run(
        _r("azurerm_linux_virtual_machine", "solo", size="Standard_D2s_v5", location="eastus"),
        _r(
            "azurerm_kubernetes_cluster",
            "aks",
            location="eastus",
            default_node_pool=[{"vm_size": "Standard_D2s_v5", "node_count": 3}],
        ),
    )
    assert (
        _cost(by, "azurerm_kubernetes_cluster.aks")
        == _cost(by, "azurerm_linux_virtual_machine.solo") * 3
    )


def test_vmss_and_extra_node_pool():
    by, _ = _run(
        _r("azurerm_linux_virtual_machine", "solo", size="Standard_D2s_v5", location="eastus"),
        _r(
            "azurerm_linux_virtual_machine_scale_set",
            "ss",
            location="eastus",
            sku="Standard_D2s_v5",
            instances=4,
        ),
        _r(
            "azurerm_kubernetes_cluster_node_pool",
            "np",
            location="eastus",
            vm_size="Standard_D2s_v5",
            node_count=2,
        ),
    )
    solo = _cost(by, "azurerm_linux_virtual_machine.solo")
    assert _cost(by, "azurerm_linux_virtual_machine_scale_set.ss") == solo * 4
    assert _cost(by, "azurerm_kubernetes_cluster_node_pool.np") == solo * 2


def test_gke_node_pool_and_gcp_mig():
    by, _ = _run(
        _r("google_compute_instance", "solo", machine_type="n2-standard-4", zone="us-central1-a"),
        _r(
            "google_container_node_pool",
            "np",
            location="us-central1",
            node_count=3,
            node_config=[{"machine_type": "n2-standard-4"}],
        ),
        _r("google_compute_instance_template", "tpl", id="tpl-1", machine_type="n2-standard-4"),
        _r(
            "google_compute_instance_group_manager",
            "mig",
            region="us-central1",
            target_size=6,
            version=[{"instance_template": "tpl-1"}],
        ),
    )
    solo = _cost(by, "google_compute_instance.solo")
    assert _cost(by, "google_container_node_pool.np") == solo * 3
    assert _cost(by, "google_compute_instance_group_manager.mig") == solo * 6


def test_emr_master_plus_core_groups():
    by, _ = _run(
        _r("aws_instance", "solo", instance_type="m5.large", region="us-east-1"),
        _r(
            "aws_emr_cluster",
            "emr",
            region="us-east-1",
            master_instance_group=[{"instance_type": "m5.large", "instance_count": 1}],
            core_instance_group=[{"instance_type": "m5.large", "instance_count": 3}],
        ),
    )
    # 1 master + 3 core = 4 units
    assert _cost(by, "aws_emr_cluster.emr") == _cost(by, "aws_instance.solo") * 4


def test_deferred_fleet_is_unpriced_not_crash():
    # A recognised-but-deferred fleet (no unit recipe yet) stays unpriced.
    by, unpriced = _run(
        _r("aws_msk_cluster", "kafka", region="us-east-1"),
    )
    assert "aws_msk_cluster.kafka" not in by
    assert "aws_msk_cluster.kafka" in unpriced


def test_unresolvable_asg_reference_is_unpriced():
    # launch template not in the plan → can't resolve unit → unpriced, no crash.
    by, unpriced = _run(
        _r(
            "aws_autoscaling_group",
            "orphan",
            region="us-east-1",
            desired_capacity=2,
            launch_template=[{"id": "lt-missing"}],
        ),
    )
    assert "aws_autoscaling_group.orphan" in unpriced
