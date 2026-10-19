"""The four resource settings' one rule (#2154).

Small surface, and the reason the rule lives in one place is the same as
parallelism's: four paths write it -- workspace create, workspace update, the
autodiscovery rule template and bulk update -- times two pairs, the
orchestrator's and the Ansible execution environment's.

The rule is deliberately NARROWER than Kubernetes accepts, and that is the part
worth testing rather than assuming. `job_template._double_resource` computes the
limit as 2x the request; faced with a quantity it cannot parse it logs a warning
and returns the value unchanged, so a perfectly valid Kubernetes quantity it does
not recognise yields a limit EQUAL to the request. That is a silent halving of
the headroom every other workspace gets, visible nowhere, and the write boundary
is the only place it can be caught.

The orchestrator's pair was written UNVALIDATED until #2154 -- in `_FIELD_MAP`
and absent from `_SETTING_RULES`, the exact shape that ledger's own invariant
warns about -- so "half a cpu" stored fine and surfaced only as the Kubernetes
API server refusing the Job, naming neither the workspace nor the field.
"""

from __future__ import annotations

import pytest

from terrapod.runner.job_template import _double_resource
from terrapod.services.workspace_settings import (
    effective_ee_resources,
    validate_resource_quantity,
)


class TestAccepted:
    @pytest.mark.parametrize(
        "value", ["1", "4", "16", "500m", "1500m", "256Mi", "2Gi", "512Ki", "3Ti"]
    )
    def test_quantities_the_limit_rule_can_double(self, value: str) -> None:
        assert validate_resource_quantity(value, "resource-cpu") == value

    def test_empty_means_inherit_and_is_preserved(self) -> None:
        """Not a rejection and not a substitution. `""` is a value meaning "the
        deployment default", so the write boundary must store it as given --
        turning it into a concrete size here would remove the only way to say
        "follow the deployment"."""
        assert validate_resource_quantity("", "ee-resource-cpu") == ""

    def test_none_is_treated_as_empty(self) -> None:
        assert validate_resource_quantity(None, "ee-resource-cpu") == ""


class TestRefused:
    @pytest.mark.parametrize("value", ["0.5", "1.5", "2.0"])
    def test_decimals(self, value: str) -> None:
        """Valid Kubernetes, and `_double_resource` cannot double them."""
        with pytest.raises(ValueError, match="not a resource quantity"):
            validate_resource_quantity(value, "resource-cpu")

    @pytest.mark.parametrize("value", ["1G", "512M", "2T", "100k"])
    def test_decimal_si_suffixes(self, value: str) -> None:
        """Also valid Kubernetes: `1G` is 10^9 bytes where `1Gi` is 2^30."""
        with pytest.raises(ValueError, match="not a resource quantity"):
            validate_resource_quantity(value, "resource-memory")

    @pytest.mark.parametrize("value", [" 1", "1 ", "  500m  "])
    def test_surrounding_whitespace_rather_than_stripping_it(self, value: str) -> None:
        """Refused, not trimmed. A value a Terraform provider sends must come
        back byte for byte or the apply fails with "Provider produced
        inconsistent result after apply" -- so the write boundary validates and
        never transforms."""
        with pytest.raises(ValueError, match="whitespace"):
            validate_resource_quantity(value, "resource-cpu")

    @pytest.mark.parametrize("value", ["abc", "1Gib", "-1", "1e3", "Gi", "m"])
    def test_values_that_are_not_quantities(self, value: str) -> None:
        with pytest.raises(ValueError, match="not a resource quantity"):
            validate_resource_quantity(value, "resource-cpu")

    @pytest.mark.parametrize("value", [1, 2.5, True, [], {}])
    def test_non_strings_are_422_rather_than_500(self, value: object) -> None:
        """A non-string reaching `.strip()` is a 500 blaming the server for the
        caller's payload. Every sibling attribute answers 422; this one must
        too."""
        with pytest.raises(ValueError, match="must be a string"):
            validate_resource_quantity(value, "resource-cpu")

    def test_the_message_names_the_field(self) -> None:
        """Four keys share this rule, so a message that did not say which one
        was wrong would be the least useful part of a 422."""
        with pytest.raises(ValueError, match="ee-resource-memory"):
            validate_resource_quantity("0.5", "ee-resource-memory")


class TestTheRuleMatchesWhatTheLimitCanDouble:
    """The gate's whole justification, asserted rather than described.

    Everything the validator accepts must come back from `_double_resource`
    CHANGED -- if it came back equal, the limit would equal the request and the
    workspace would silently get no headroom.
    """

    @pytest.mark.parametrize("value", ["1", "4", "500m", "1500m", "256Mi", "2Gi", "512Ki", "3Ti"])
    def test_every_accepted_value_really_doubles(self, value: str) -> None:
        assert _double_resource(value) != value, (
            f"{value!r} is accepted by the validator but `_double_resource` "
            "returns it unchanged, so its limit equals its request"
        )

    @pytest.mark.parametrize("value", ["0.5", "1G", "512M"])
    def test_every_refused_value_really_would_not(self, value: str) -> None:
        """The other direction, which is what makes the refusals justified
        rather than merely conservative."""
        assert _double_resource(value) == value, (
            f"{value!r} is refused but `_double_resource` handles it — the "
            "validator is narrower than it needs to be"
        )


class TestTheResolver:
    def test_a_workspace_value_wins(self) -> None:
        assert effective_ee_resources("2", "4Gi") == ("2", "4Gi")

    def test_empty_takes_the_deployment_default(self) -> None:
        from terrapod.config import settings

        assert effective_ee_resources("", "") == (
            settings.default_ee_resource_cpu,
            settings.default_ee_resource_memory,
        )

    def test_none_takes_the_deployment_default(self) -> None:
        from terrapod.config import settings

        assert effective_ee_resources(None, None) == (
            settings.default_ee_resource_cpu,
            settings.default_ee_resource_memory,
        )

    def test_the_two_resolve_INDEPENDENTLY(self) -> None:
        """A workspace that sized only its CPU must keep the default memory.

        The plausible bug is an `if not cpu and not memory` guard that falls
        back to both together, which would silently discard the half the
        operator did set.
        """
        from terrapod.config import settings

        assert effective_ee_resources("2", "") == ("2", settings.default_ee_resource_memory)
        assert effective_ee_resources("", "4Gi") == (settings.default_ee_resource_cpu, "4Gi")
