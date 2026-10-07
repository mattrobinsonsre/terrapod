"""The inventory merge, pinned against the measurements recorded on #1967.

The precedence cases below are not invented: #1967 records them as measured on
ansible-core 2.18.3 over three source types at once, in both orders. They are
reproduced here so that a future change to `merge` has to disagree with ansible
out loud rather than quietly.
"""

from __future__ import annotations

import pytest

from terrapod.services.inventory_resolution import (
    DERIVED_GROUPS,
    HostEntry,
    InventoryValidationError,
    ResolvedInventory,
    SourceResolution,
    limit_matches,
    merge,
    to_ansible_inventory,
    validate_declared_vars,
    validate_group_name,
    validate_host_name,
)


def _source(label: str, *hosts: HostEntry) -> SourceResolution:
    return SourceResolution(label=label, hosts=hosts)


class TestMergeTakesAnsiblesSemantics:
    """hosts union, groups union, later source wins per variable."""

    def test_hosts_are_unioned_across_sources(self):
        resolved = merge(
            [
                _source("a", HostEntry("host1"), HostEntry("host2")),
                _source("b", HostEntry("host3"), HostEntry("host4")),
            ]
        )

        assert sorted(resolved.hosts) == ["host1", "host2", "host3", "host4"]

    def test_a_group_named_by_two_sources_holds_the_hosts_from_both(self):
        # #1967's measurement: `web` merged from the YAML and the INI gave
        # host1 host2 host4.
        resolved = merge(
            [
                _source(
                    "yaml",
                    HostEntry("host1", groups=("web",)),
                    HostEntry("host2", groups=("web",)),
                ),
                _source("ini", HostEntry("host4", groups=("web",))),
            ]
        )

        assert resolved.groups["web"] == ["host1", "host2", "host4"]

    def test_the_later_source_wins_a_conflicting_variable(self):
        resolved = merge(
            [
                _source("a", HostEntry("h", vars={"who": "from_A"})),
                _source("b", HostEntry("h", vars={"who": "from_B"})),
            ]
        )

        assert resolved.hosts["h"]["who"] == "from_B"

    def test_and_wins_in_the_other_order_too(self):
        """The half that makes it precedence rather than a coincidence."""
        resolved = merge(
            [
                _source("b", HostEntry("h", vars={"who": "from_B"})),
                _source("a", HostEntry("h", vars={"who": "from_A"})),
            ]
        )

        assert resolved.hosts["h"]["who"] == "from_A"

    def test_a_non_conflicting_variable_survives_the_later_source(self):
        resolved = merge(
            [
                _source("a", HostEntry("h", vars={"only_in_A": 1, "who": "from_A"})),
                _source("b", HostEntry("h", vars={"who": "from_B"})),
            ]
        )

        assert resolved.hosts["h"] == {"only_in_A": 1, "who": "from_B"}

    def test_group_membership_crosses_source_boundaries(self):
        # `net` drew host2 from one source and switch1 from another.
        resolved = merge(
            [
                _source("yaml", HostEntry("host2", groups=("web", "net"))),
                _source("script", HostEntry("switch1", groups=("net",))),
            ]
        )

        assert resolved.groups["net"] == ["host2", "switch1"]

    def test_one_source_is_a_degenerate_case_not_a_special_case(self):
        resolved = merge([_source("terraform", HostEntry("h", groups=("web",)))])

        assert resolved.hosts == {"h": {}}
        assert resolved.groups == {"web": ["h"]}

    def test_no_sources_resolves_to_nothing_rather_than_failing(self):
        resolved = merge([])

        assert resolved.hosts == {}
        assert resolved.groups == {}
        assert resolved.host_count == 0

    def test_group_members_are_sorted_so_a_snapshot_is_byte_stable(self):
        """Two identical resolutions must not produce differing snapshots."""
        one = merge([_source("a", HostEntry("b", groups=("g",)), HostEntry("a", groups=("g",)))])
        two = merge([_source("a", HostEntry("a", groups=("g",)), HostEntry("b", groups=("g",)))])

        assert one.groups == two.groups == {"g": ["a", "b"]}

    def test_provenance_records_which_sources_named_a_host(self):
        resolved = merge(
            [
                _source("terraform", HostEntry("h")),
                _source("git", HostEntry("h")),
            ]
        )

        assert resolved.provenance["h"] == ["terraform", "git"]


class TestAnsibleRendering:
    def test_every_host_appears_in_hostvars_including_var_less_ones(self):
        """The omission trap #1967 records, which our output must not carry.

        `ansible-inventory --list` leaves a host with no vars out of
        `_meta.hostvars` entirely -- measured, `switch1` was in group `net` and
        absent from hostvars. Anything enumerating the host set from hostvars
        therefore loses it.
        """
        resolved = merge(
            [
                _source(
                    "a",
                    HostEntry("has_vars", vars={"x": 1}, groups=("net",)),
                    HostEntry("switch1", groups=("net",)),
                )
            ]
        )

        rendered = to_ansible_inventory(resolved)

        assert set(rendered["_meta"]["hostvars"]) == {"has_vars", "switch1"}
        assert rendered["_meta"]["hostvars"]["switch1"] == {}

    def test_hosts_in_no_declared_group_land_in_ungrouped(self):
        resolved = merge([_source("a", HostEntry("lonely"), HostEntry("grouped", groups=("web",)))])

        rendered = to_ansible_inventory(resolved)

        assert rendered["ungrouped"] == {"hosts": ["lonely"]}
        assert rendered["web"] == {"hosts": ["grouped"]}
        assert set(rendered["all"]["children"]) == {"web", "ungrouped"}

    def test_ungrouped_is_omitted_when_every_host_has_a_group(self):
        resolved = merge([_source("a", HostEntry("h", groups=("web",)))])

        rendered = to_ansible_inventory(resolved)

        assert "ungrouped" not in rendered
        assert rendered["all"]["children"] == ["web"]

    def test_all_is_always_present_even_for_an_empty_inventory(self):
        """A consumer reading all.children should not have to handle absence."""
        rendered = to_ansible_inventory(ResolvedInventory())

        assert rendered["all"] == {"children": []}
        assert rendered["_meta"] == {"hostvars": {}}


class TestNameValidation:
    @pytest.mark.parametrize("name", ["web", "env_prod", "_internal", "a1"])
    def test_accepts_names_ansible_uses_without_warning(self, name):
        assert validate_group_name(name) == name

    @pytest.mark.parametrize("name", sorted(DERIVED_GROUPS))
    def test_refuses_the_groups_ansible_derives(self, name):
        with pytest.raises(InventoryValidationError, match="derived by ansible"):
            validate_group_name(name)

    @pytest.mark.parametrize("name", ["1web", "web-prod", "web prod", "web.prod", ""])
    def test_refuses_group_names_ansible_would_warn_about(self, name):
        with pytest.raises(InventoryValidationError):
            validate_group_name(name)

    @pytest.mark.parametrize("name", ["host1", "web-01.example.com", "10.0.0.4", "a_b", "HOST"])
    def test_accepts_host_names_that_can_be_targeted(self, name):
        assert validate_host_name(name) == name

    @pytest.mark.parametrize("bad", [",", ":", "!", "&", "~"])
    def test_refuses_host_names_containing_a_limit_operator(self, bad):
        """Each of these makes the host unselectable or changes other matches."""
        with pytest.raises(InventoryValidationError, match="limit's own"):
            validate_host_name(f"web{bad}01")

    def test_refuses_a_leading_exclusion_marker_specifically(self):
        """`!web` would silently exclude `web` from any pattern naming it."""
        with pytest.raises(InventoryValidationError):
            validate_host_name("!web")

    @pytest.mark.parametrize("name", ["has space", " leading", "trailing ", "tab\there"])
    def test_refuses_whitespace_because_limit_splits_on_it(self, name):
        with pytest.raises(InventoryValidationError, match="whitespace"):
            validate_host_name(name)

    def test_var_NAMES_are_only_required_to_be_non_empty_strings(self):
        """Still deliberately laxer than groups, and for the original reason:
        ansible stores a non-identifier name and only warns, and
        `hostvars['h']['odd-name']` works, so refusing one would block a working
        configuration to prevent a warning. Only the VALUE rule tightened."""
        assert validate_declared_vars({"ansible_user": "ec2-user", "odd-name": "x"}) == {
            "ansible_user": "ec2-user",
            "odd-name": "x",
        }

    def test_refuses_an_empty_variable_name(self):
        with pytest.raises(InventoryValidationError):
            validate_declared_vars({"": "v"})

    def test_the_NAME_is_judged_before_the_value(self):
        """An entry that is wrong both ways should say the name is wrong -- that
        is the one the operator fixes first, and the value message would send
        them to a group_vars file they do not need."""
        with pytest.raises(InventoryValidationError, match="names must be non-empty"):
            validate_declared_vars({"": 1})

    @pytest.mark.parametrize("value", [8080, 22.5, True, None, ["a", "b"], {"nested": "object"}])
    def test_refuses_a_non_string_DECLARED_value(self, value):
        """Narrower than ansible's own rule, on purpose.

        A declared item is the flat surface Terraform owns and a Terraform map is
        `map(string)`. More to the point, every Go consumer decodes vars into
        `map[string]string` and `encoding/json` fails the WHOLE unmarshal on one
        non-string value -- so storing `{"role": "frontend", "port": 8080}` made
        the SDK, the provider and the MCP tools report no variables at all for
        that host, with nothing saying why. A 422 naming the key replaces a
        silent whole-set disappearance.
        """
        with pytest.raises(InventoryValidationError, match="must be a string"):
            validate_declared_vars({"port": value})

    def test_the_refusal_points_at_where_richer_values_belong(self):
        """Refusing without saying where to put it would just move the dead end."""
        with pytest.raises(InventoryValidationError, match="group_vars or host_vars"):
            validate_declared_vars({"ports": [80, 443]})

    def test_a_resolved_snapshot_is_NOT_subject_to_this(self):
        """The rule is about the declared surface only. A snapshot carries what
        ansible actually produced, so it must keep rich values -- and the
        snapshot path deliberately does not call this function.
        """
        import inspect

        from terrapod.api.routers import inventory as router

        src = inspect.getsource(router.record_inventory_version)
        assert "validate_declared_vars" not in src, (
            "a snapshot holds ansible's own resolution; flattening it to strings would "
            "lose the shape a configure needs"
        )

    def test_refuses_host_vars_that_are_not_a_mapping(self):
        with pytest.raises(InventoryValidationError, match="mapping"):
            validate_declared_vars(["not", "a", "mapping"])  # type: ignore[arg-type]


class TestLimitPreview:
    @pytest.fixture
    def resolved(self) -> ResolvedInventory:
        return merge(
            [
                _source(
                    "a",
                    HostEntry("host1", groups=("web",)),
                    HostEntry("host2", groups=("web", "net")),
                    HostEntry("host4", groups=("web",)),
                    HostEntry("switch1", groups=("net",)),
                    HostEntry("lonely"),
                )
            ]
        )

    def test_an_empty_limit_is_the_whole_inventory(self, resolved):
        assert limit_matches(resolved, "") == sorted(resolved.hosts)
        assert limit_matches(resolved, "   ") == sorted(resolved.hosts)

    def test_a_group_selects_its_members(self, resolved):
        # #1967's measurement: `web` -> 3 hosts.
        assert limit_matches(resolved, "web") == ["host1", "host2", "host4"]

    def test_exclusion_narrows_a_group(self, resolved):
        # `web:!host4` -> 2.
        assert limit_matches(resolved, "web:!host4") == ["host1", "host2"]

    def test_a_group_spanning_sources_selects_across_them(self, resolved):
        # `net` -> host2 switch1.
        assert limit_matches(resolved, "net") == ["host2", "switch1"]

    def test_all_and_star_both_mean_everything(self, resolved):
        assert limit_matches(resolved, "all") == sorted(resolved.hosts)
        assert limit_matches(resolved, "*") == sorted(resolved.hosts)

    def test_a_bare_host_name_selects_that_host(self, resolved):
        assert limit_matches(resolved, "switch1") == ["switch1"]

    def test_comma_and_colon_both_separate_terms(self, resolved):
        assert limit_matches(resolved, "host1,switch1") == ["host1", "switch1"]
        assert limit_matches(resolved, "host1:switch1") == ["host1", "switch1"]

    def test_intersection_keeps_only_hosts_in_both(self, resolved):
        assert limit_matches(resolved, "web:&net") == ["host2"]

    def test_a_glob_matches_host_and_group_names(self, resolved):
        assert limit_matches(resolved, "host*") == ["host1", "host2", "host4"]

    def test_an_unmatched_term_selects_nothing_rather_than_everything(self, resolved):
        assert limit_matches(resolved, "nosuchgroup") == []

    def test_a_regex_term_is_refused_rather_than_silently_matching_nothing(self, resolved):
        """Showing an empty target set for a limit ansible would expand is worse
        than refusing to preview it."""
        with pytest.raises(InventoryValidationError, match="regular expression"):
            limit_matches(resolved, "~web.*")
