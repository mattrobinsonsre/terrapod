"""Tests for drift_ignore_classifier (#482).

The classifier decides whether a drift run still counts as drift after
applying the workspace's `drift_ignore_rules`. These tests pin the
behavioural contract end-to-end against realistic `tofu show -json`
plan shapes: exact-match rules, attribute globs, address globs,
brackets, whole-resource suppression, create/delete safety, partial
suppression, and the "no rules → identity" path.
"""

from __future__ import annotations

from terrapod.services.drift_ignore_classifier import classify_drift


def _update_plan(address: str, before: dict, after: dict) -> dict:
    """Build a minimal plan-JSON envelope with one update resource_change."""
    return {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".")[-2] if "." in address else address,
                "name": address.split(".")[-1],
                "change": {
                    "actions": ["update"],
                    "before": before,
                    "after": after,
                },
            }
        ]
    }


class TestRuleMatching:
    def test_exact_attribute_match_suppresses_drift(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"tags": {"Environment": "dev", "Owner": "alice"}},
            {"tags": {"Environment": "prod", "Owner": "alice"}},
        )
        still_drifted, suppressed = classify_drift(plan, ["aws_iam_role.foo.tags.Environment"])
        assert still_drifted is False
        assert suppressed and suppressed[0]["address"] == "aws_iam_role.foo"

    def test_unmatched_attribute_remains_drift(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"tags": {"Environment": "dev", "Owner": "alice"}},
            {"tags": {"Environment": "prod", "Owner": "bob"}},
        )
        still_drifted, suppressed = classify_drift(plan, ["aws_iam_role.foo.tags.Environment"])
        # Owner changed too; not in rules → still drift.
        assert still_drifted is True

    def test_glob_segment_matches_index(self):
        plan = _update_plan(
            "aws_autoscaling_group.workers[0]",
            {"desired_capacity": 3},
            {"desired_capacity": 5},
        )
        still_drifted, suppressed = classify_drift(
            plan, ["aws_autoscaling_group.workers[*].desired_capacity"]
        )
        assert still_drifted is False
        assert suppressed[0]["paths"] == ["desired_capacity"]

    def test_glob_across_module_path(self):
        """User's motivating case from the issue: argocd_cluster ca_data drift."""
        plan = _update_plan(
            "module.eks_legacy[0].argocd_cluster.eks[0]",
            {"config": {"tls_client_config": {"ca_data": "OLD"}}},
            {"config": {"tls_client_config": {"ca_data": "NEW"}}},
        )
        rule = "module.eks*.argocd_cluster.eks*.config.tls_client_config.ca_data"
        still_drifted, _ = classify_drift(plan, [rule])
        assert still_drifted is False

        plan2 = _update_plan(
            "module.eks[0].argocd_cluster.eks",
            {"config": {"tls_client_config": {"ca_data": "OLD"}}},
            {"config": {"tls_client_config": {"ca_data": "NEW"}}},
        )
        still_drifted2, _ = classify_drift(plan2, [rule])
        assert still_drifted2 is False, (
            "Wildcard rule must cover both eks[0] and unsuffixed eks variants"
        )

    def test_single_segment_glob_does_not_cross_dots(self):
        """`*` is segment-local: `module.*.foo` MUST NOT match
        `module.a.b.foo` (two segments under module).
        """
        plan = _update_plan(
            "module.a.b.aws_instance.foo",
            {"ami": "ami-old"},
            {"ami": "ami-new"},
        )
        still_drifted, _ = classify_drift(plan, ["module.*.aws_instance.foo.ami"])
        assert still_drifted is True  # rule's `*` only crosses one segment


class TestHclBlockListShape:
    """HCL nested blocks serialize as single-element lists in `tofu show
    -json`. A natural attribute-path rule must match the indexed shape.

    This is the exact shape that the v0.36.0 pre-release live smoke
    surfaced: `module.eks[0].argocd_cluster.eks` reports a change at
    config[0].tls_client_config[0].ca_data, and the operator's rule
    `...config.tls_client_config.ca_data` (no indices) must still match.
    Synthetic plan — no production data.
    """

    def test_block_indices_optional_in_rule(self):
        plan = _update_plan(
            "module.eks[0].argocd_cluster.eks",
            {"config": [{"tls_client_config": [{"ca_data": "OLD"}]}]},
            {"config": [{"tls_client_config": [{"ca_data": "NEW"}]}]},
        )
        # Operator writes the natural HCL path with no [0] block indices.
        rule = "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data"
        still_drifted, suppressed = classify_drift(plan, [rule])
        assert still_drifted is False, (
            "Rule without explicit block indices must match the "
            "plan-JSON single-element-list block shape"
        )
        assert suppressed[0]["address"] == "module.eks[0].argocd_cluster.eks"

    def test_legacy_module_variant_same_rule(self):
        plan = _update_plan(
            "module.eks_legacy[0].argocd_cluster.eks[0]",
            {"config": [{"tls_client_config": [{"ca_data": "OLD"}]}]},
            {"config": [{"tls_client_config": [{"ca_data": "NEW"}]}]},
        )
        rule = "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data"
        still_drifted, _ = classify_drift(plan, [rule])
        assert still_drifted is False

    def test_explicit_bracket_star_also_works(self):
        """Operators who DO write `config[*]` get the same result —
        the index-tolerant fallback doesn't break explicit brackets."""
        plan = _update_plan(
            "module.eks[0].argocd_cluster.eks",
            {"config": [{"tls_client_config": [{"ca_data": "OLD"}]}]},
            {"config": [{"tls_client_config": [{"ca_data": "NEW"}]}]},
        )
        rule = "module.eks*.argocd_cluster.*.config[*].tls_client_config[*].ca_data"
        still_drifted, _ = classify_drift(plan, [rule])
        assert still_drifted is False

    def test_string_key_index_not_stripped(self):
        """Numeric block indices are stripped for matching; string-key
        (for_each) indices are NOT, so a bare rule can't over-match
        across for_each instances."""
        plan = _update_plan(
            'aws_instance.web["prod"]',
            {"ami": "ami-old"},
            {"ami": "ami-new"},
        )
        # Rule without the ["prod"] key must NOT match — the key is
        # semantically meaningful and stays in the candidate.
        still_drifted, _ = classify_drift(plan, ["aws_instance.web.ami"])
        assert still_drifted is True


class TestWholeResourceRules:
    def test_bare_address_matches_any_attribute(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"name": "old", "description": "x"},
            {"name": "new", "description": "y"},
        )
        # Bare address with no attribute suffix — silences any change.
        still_drifted, suppressed = classify_drift(plan, ["aws_iam_role.foo"])
        assert still_drifted is False
        assert suppressed[0]["address"] == "aws_iam_role.foo"

    def test_per_attribute_rule_does_not_silence_delete(self):
        """A `replicas` rule must not silence a delete — the operator
        wants to know if their resource is being destroyed."""
        plan = {
            "resource_changes": [
                {
                    "address": "kubernetes_deployment.api",
                    "type": "kubernetes_deployment",
                    "name": "api",
                    "change": {
                        "actions": ["delete"],
                        "before": {"spec": [{"replicas": 5}]},
                        "after": None,
                    },
                }
            ]
        }
        still_drifted, _ = classify_drift(plan, ["kubernetes_deployment.api.spec[0].replicas"])
        assert still_drifted is True, (
            "Per-attribute rule must NOT silence a delete; only a bare-"
            "address rule may silence whole-resource lifecycle changes"
        )

    def test_bare_address_rule_silences_delete(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_lambda_function.scratch",
                    "type": "aws_lambda_function",
                    "name": "scratch",
                    "change": {
                        "actions": ["delete"],
                        "before": {"function_name": "scratch"},
                        "after": None,
                    },
                }
            ]
        }
        still_drifted, suppressed = classify_drift(plan, ["aws_lambda_function.scratch"])
        assert still_drifted is False
        assert suppressed[0]["address"] == "aws_lambda_function.scratch"


class TestPartialSuppression:
    def test_two_changes_one_ignored_remains_drift(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"tags": {"Environment": "dev"}, "description": "old"},
            {"tags": {"Environment": "prod"}, "description": "new"},
        )
        still_drifted, _ = classify_drift(plan, ["aws_iam_role.foo.tags.Environment"])
        assert still_drifted is True

    def test_multiple_resources_independent(self):
        """One resource fully suppressed, another not — drift remains
        because *some* resource still has un-ignored changes."""
        plan = {
            "resource_changes": [
                {
                    "address": "aws_iam_role.foo",
                    "type": "aws_iam_role",
                    "name": "foo",
                    "change": {
                        "actions": ["update"],
                        "before": {"tags": {"x": "1"}},
                        "after": {"tags": {"x": "2"}},
                    },
                },
                {
                    "address": "aws_instance.bar",
                    "type": "aws_instance",
                    "name": "bar",
                    "change": {
                        "actions": ["update"],
                        "before": {"ami": "old"},
                        "after": {"ami": "new"},
                    },
                },
            ]
        }
        still_drifted, suppressed = classify_drift(plan, ["aws_iam_role.foo.tags.x"])
        assert still_drifted is True
        # The foo resource WAS suppressed; bar wasn't → reported as
        # partial in logs but overall status is drifted.
        suppressed_addresses = {s["address"] for s in suppressed}
        assert "aws_iam_role.foo" in suppressed_addresses


class TestNoOpAndReadActions:
    def test_no_op_action_never_drifts(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_iam_role.foo",
                    "type": "aws_iam_role",
                    "name": "foo",
                    "change": {"actions": ["no-op"], "before": {}, "after": {}},
                }
            ]
        }
        still_drifted, suppressed = classify_drift(plan, [])
        assert still_drifted is False
        assert suppressed == []

    def test_read_action_never_drifts(self):
        plan = {
            "resource_changes": [
                {
                    "address": "data.aws_ami.x",
                    "type": "aws_ami",
                    "name": "x",
                    "change": {"actions": ["read"], "before": None, "after": {}},
                }
            ]
        }
        still_drifted, _ = classify_drift(plan, [])
        assert still_drifted is False


class TestEmptyRules:
    def test_no_rules_with_changes_is_drifted(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"name": "old"},
            {"name": "new"},
        )
        still_drifted, suppressed = classify_drift(plan, [])
        assert still_drifted is True
        assert suppressed == []

    def test_no_rules_with_no_changes_is_not_drifted(self):
        plan = {"resource_changes": []}
        still_drifted, suppressed = classify_drift(plan, [])
        assert still_drifted is False
        assert suppressed == []

    def test_empty_string_rule_skipped_gracefully(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"name": "old"},
            {"name": "new"},
        )
        # Whitespace / empty entries should be ignored by the compiler,
        # not cause spurious matches.
        still_drifted, _ = classify_drift(plan, ["", "   "])
        assert still_drifted is True


class TestDiffPathWalking:
    def test_nested_dict_path_built_correctly(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"a": {"b": {"c": 1}}},
            {"a": {"b": {"c": 2}}},
        )
        still_drifted, _ = classify_drift(plan, ["aws_iam_role.foo.a.b.c"])
        assert still_drifted is False

    def test_list_index_bracket_form(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"statements": [{"effect": "Allow"}, {"effect": "Deny"}]},
            {"statements": [{"effect": "Allow"}, {"effect": "Allow"}]},
        )
        # statements[1].effect changed; rule must cover index 1.
        still_drifted, _ = classify_drift(plan, ["aws_iam_role.foo.statements[*].effect"])
        assert still_drifted is False

    def test_unequal_list_length_emits_paths_at_diff_indices(self):
        plan = _update_plan(
            "aws_iam_role.foo",
            {"statements": [{"x": 1}]},
            {"statements": [{"x": 1}, {"y": 2}]},
        )
        still_drifted, _ = classify_drift(plan, ["aws_iam_role.foo.statements[*]"])
        assert still_drifted is False
