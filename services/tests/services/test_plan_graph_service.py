"""Tests for plan_graph_service — plan JSON → impact graph derivation (#761)."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import plan_graph_service

# A minimal but representative plan: a singleton CA that a for_each'd cert
# depends on, a mix of actions, and an unconnected no-op resource.
_PLAN = {
    "terraform_version": "1.12.3",
    "resource_changes": [
        {
            "address": "tls_private_key.ca",
            "type": "tls_private_key",
            "name": "ca",
            "provider_name": "registry.terraform.io/hashicorp/tls",
            "change": {"actions": ["delete", "create"]},  # replace
        },
        {
            "address": 'tls_cert.svc["api"]',
            "type": "tls_cert",
            "name": "svc",
            "provider_name": "registry.terraform.io/hashicorp/tls",
            "change": {"actions": ["create"]},
        },
        {
            "address": 'tls_cert.svc["web"]',
            "type": "tls_cert",
            "name": "svc",
            "provider_name": "registry.terraform.io/hashicorp/tls",
            "change": {"actions": ["delete"]},
        },
        {
            "address": "random_pet.deploy",
            "type": "random_pet",
            "name": "deploy",
            "provider_name": "registry.terraform.io/hashicorp/random",
            "change": {"actions": ["no-op"]},
        },
    ],
    "configuration": {
        "root_module": {
            "resources": [
                {
                    "address": "tls_cert.svc",
                    "expressions": {
                        "key": {
                            "references": [
                                "tls_private_key.ca.private_key_pem",
                                "tls_private_key.ca",
                            ]
                        }
                    },
                },
            ]
        }
    },
}


class TestDeriveGraph:
    def test_actions_and_counts(self):
        g = plan_graph_service.derive_graph(json.dumps(_PLAN).encode())
        assert len(g["nodes"]) == 4
        assert g["meta"]["counts"] == {
            "create": 1,
            "update": 0,
            "replace": 1,
            "delete": 1,
            "noop": 1,
        }
        assert g["meta"]["terraform_version"] == "1.12.3"
        by_id = {n["id"]: n for n in g["nodes"]}
        assert by_id["tls_private_key.ca"]["action"] == "replace"
        assert by_id['tls_cert.svc["api"]']["action"] == "create"
        assert by_id['tls_cert.svc["web"]']["action"] == "delete"
        assert by_id['tls_cert.svc["api"]']["key"] == "api"
        assert by_id["random_pet.deploy"]["provider"] == "random"

    def test_edges_expanded_across_for_each(self):
        g = plan_graph_service.derive_graph(json.dumps(_PLAN).encode())
        edges = {(e["source"], e["target"]) for e in g["edges"]}
        # both for_each instances of the cert depend on the singleton CA
        assert ('tls_cert.svc["api"]', "tls_private_key.ca") in edges
        assert ('tls_cert.svc["web"]', "tls_private_key.ca") in edges
        assert len(edges) == 2  # the no-op resource is disconnected

    def test_empty_plan_is_empty_graph(self):
        g = plan_graph_service.derive_graph(b'{"resource_changes": []}')
        assert g["nodes"] == []
        assert g["edges"] == []
        assert all(v == 0 for v in g["meta"]["counts"].values())


class TestGetImpactGraph:
    async def test_none_when_no_json_output(self):
        run = MagicMock(has_json_output=False)
        assert await plan_graph_service.get_impact_graph(run) is None

    @patch("terrapod.services.plan_graph_service.get_storage")
    async def test_none_when_object_missing(self, mock_storage):
        run = MagicMock(has_json_output=True, workspace_id=uuid.uuid4(), id=uuid.uuid4())
        st = mock_storage.return_value
        st.exists = AsyncMock(return_value=False)
        assert await plan_graph_service.get_impact_graph(run) is None

    @patch("terrapod.services.plan_graph_service.get_storage")
    async def test_derives_from_stored_plan(self, mock_storage):
        run = MagicMock(has_json_output=True, workspace_id=uuid.uuid4(), id=uuid.uuid4())
        st = mock_storage.return_value
        st.exists = AsyncMock(return_value=True)
        st.get = AsyncMock(return_value=json.dumps(_PLAN).encode())
        g = await plan_graph_service.get_impact_graph(run)
        assert g is not None
        assert len(g["nodes"]) == 4
        assert len(g["edges"]) == 2


# A modular plan: root calls `net` + `app`; `app` nests `inner`. Exercises
# cross-module `var` binding (app.svc -> net.vpc), output binding
# (module.net.vpc_id -> net.random_id.vpc), for_each fan-out across a module
# boundary, and nested-module recursion (inner.name binds var.seed -> app.svc).
_MODULAR_PLAN = {
    "terraform_version": "1.12.3",
    "resource_changes": [
        {
            "address": "module.net.random_id.vpc",
            "type": "random_id",
            "name": "vpc",
            "change": {"actions": ["create"]},
        },
        {
            "address": 'module.app.null_resource.svc["api"]',
            "type": "null_resource",
            "name": "svc",
            "change": {"actions": ["create"]},
        },
        {
            "address": 'module.app.null_resource.svc["web"]',
            "type": "null_resource",
            "name": "svc",
            "change": {"actions": ["create"]},
        },
        {
            "address": "module.app.module.inner.random_pet.name",
            "type": "random_pet",
            "name": "name",
            "change": {"actions": ["create"]},
        },
    ],
    "configuration": {
        "root_module": {
            "module_calls": {
                "net": {
                    "module": {
                        "resources": [{"address": "random_id.vpc", "expressions": {}}],
                        "outputs": {
                            "vpc_id": {
                                "expression": {"references": ["random_id.vpc.hex", "random_id.vpc"]}
                            }
                        },
                    }
                },
                "app": {
                    "expressions": {
                        "vpc_id": {"references": ["module.net.vpc_id", "module.net"]},
                        "services": {"references": ["local.services"]},
                    },
                    "module": {
                        "resources": [
                            {
                                "address": "null_resource.svc",
                                "for_each_expression": {"references": ["var.services"]},
                                "expressions": {"triggers": {"references": ["var.vpc_id"]}},
                            }
                        ],
                        "module_calls": {
                            "inner": {
                                "expressions": {"seed": {"references": ["null_resource.svc"]}},
                                "module": {
                                    "resources": [
                                        {
                                            "address": "random_pet.name",
                                            "expressions": {
                                                "keepers": {"references": ["var.seed"]}
                                            },
                                        }
                                    ],
                                    "outputs": {},
                                },
                            }
                        },
                    },
                },
            }
        }
    },
}


class TestModularGraph:
    def test_nodes_carry_module_path(self):
        g = plan_graph_service.derive_graph(json.dumps(_MODULAR_PLAN).encode())
        mods = {n["id"]: n["module"] for n in g["nodes"]}
        assert mods["module.net.random_id.vpc"] == "net"
        assert mods['module.app.null_resource.svc["api"]'] == "app"
        assert mods["module.app.module.inner.random_pet.name"] == "app.inner"

    def test_cross_module_var_binding(self):
        # app.svc depends (via var.vpc_id -> module.net.vpc_id) on net.random_id.vpc,
        # fanned out to every for_each instance from the singleton source.
        g = plan_graph_service.derive_graph(json.dumps(_MODULAR_PLAN).encode())
        edges = {(e["source"], e["target"]) for e in g["edges"]}
        assert ('module.app.null_resource.svc["api"]', "module.net.random_id.vpc") in edges
        assert ('module.app.null_resource.svc["web"]', "module.net.random_id.vpc") in edges

    def test_nested_module_var_binds_to_parent_resource(self):
        # inner.random_pet.name references var.seed, bound to the PARENT module's
        # null_resource.svc — proving recursion + parent-scope var resolution.
        g = plan_graph_service.derive_graph(json.dumps(_MODULAR_PLAN).encode())
        edges = {(e["source"], e["target"]) for e in g["edges"]}
        assert (
            "module.app.module.inner.random_pet.name",
            'module.app.null_resource.svc["api"]',
        ) in edges
        assert (
            "module.app.module.inner.random_pet.name",
            'module.app.null_resource.svc["web"]',
        ) in edges

    def test_no_spurious_self_or_cross_edges(self):
        g = plan_graph_service.derive_graph(json.dumps(_MODULAR_PLAN).encode())
        assert all(e["source"] != e["target"] for e in g["edges"])
        # exactly the four dependency edges above, nothing else
        assert len(g["edges"]) == 4


class TestModuleOf:
    def test_module_paths(self):
        assert plan_graph_service._module_of("aws_instance.web") == ""
        assert plan_graph_service._module_of("module.vpc.aws_subnet.this[0]") == "vpc"
        assert plan_graph_service._module_of("module.eks.module.ng.aws_x.y") == "eks.ng"

    def test_node_carries_module(self):
        import json as _json

        g = plan_graph_service.derive_graph(_json.dumps(_PLAN).encode())
        # every _PLAN resource is a root resource → module ""
        assert all(n["module"] == "" for n in g["nodes"])
