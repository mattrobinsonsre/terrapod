"""Provenance for a resolved inventory variable (#2413 F8).

**The fixture is ansible's REAL output**, captured from `ansible-inventory
--graph --vars` on core 2.21.6 inside the shipped API image, over an inventory
with nested groups, an `all` group, a host-level override and a value containing
spaces and quotes. A hand-written fixture would hold only the shapes the author
imagined, and the defect this module actually had was invisible to one: the two
ansible sources spell a group differently (`@prod_web` against `prod_web`), so
every attribution came back `here` while every spelling looked right in
isolation.

The fixture's layout, so a reader need not reconstruct it:

    all              region=eu, shared=all-loses
    └── prod         tier=critical, shared=prod-wins
        ├── prod_web role=web, ansible_ssh_common_args=<with spaces + quotes>
        │   ├── web1 host_vars: shared=host-wins, only_here=yes
        │   └── web2 (no host_vars)
        └── prod_db
            └── db1
    staging
        └── stg1
"""

import pytest

from terrapod.services.inventory_provenance import attribute, parse_graph

# Verbatim from the shipped image. Do not tidy the whitespace: the depth of each
# line is the data, and the parser reads it by counting pipes.
REAL_GRAPH = """@all:
  |--@ungrouped:
  |--@prod:
  |  |--@prod_web:
  |  |  |--web1
  |  |  |  |--{ansible_ssh_common_args = -o ProxyCommand="ssh -W %h:%p bastion"}
  |  |  |  |--{only_here = True}
  |  |  |  |--{region = eu}
  |  |  |  |--{role = web}
  |  |  |  |--{shared = host-wins}
  |  |  |  |--{tier = critical}
  |  |  |--web2
  |  |  |  |--{ansible_ssh_common_args = -o ProxyCommand="ssh -W %h:%p bastion"}
  |  |  |  |--{region = eu}
  |  |  |  |--{role = web}
  |  |  |  |--{shared = prod-wins}
  |  |  |  |--{tier = critical}
  |  |  |--{ansible_ssh_common_args = -o ProxyCommand="ssh -W %h:%p bastion"}
  |  |  |--{role = web}
  |  |--@prod_db:
  |  |  |--db1
  |  |  |  |--{region = eu}
  |  |  |  |--{shared = prod-wins}
  |  |  |  |--{tier = critical}
  |  |--{shared = prod-wins}
  |  |--{tier = critical}
  |--@staging:
  |  |--stg1
  |  |  |--{region = eu}
  |  |  |--{shared = all-loses}
  |--{region = eu}
  |--{shared = all-loses}
"""

# From `--list` on the same inventory: bare group names, real JSON types.
CHILDREN = {"all": ["ungrouped", "prod", "staging"], "prod": ["prod_web", "prod_db"]}
GROUPS_OF = {"web1": ["prod_web"], "web2": ["prod_web"], "db1": ["prod_db"], "stg1": ["staging"]}
HOSTVARS = {
    "web1": {
        "ansible_ssh_common_args": '-o ProxyCommand="ssh -W %h:%p bastion"',
        "only_here": True,
        "region": "eu",
        "role": "web",
        "shared": "host-wins",
        "tier": "critical",
    },
    "web2": {
        "ansible_ssh_common_args": '-o ProxyCommand="ssh -W %h:%p bastion"',
        "region": "eu",
        "role": "web",
        "shared": "prod-wins",
        "tier": "critical",
    },
    "stg1": {"region": "eu", "shared": "all-loses"},
}


def _attr(host: str):
    return attribute(
        node=host,
        is_host=True,
        effective=HOSTVARS[host],
        groups_of_node=GROUPS_OF[host],
        at_level=parse_graph(REAL_GRAPH),
        children=CHILDREN,
    )


class TestParseGraph:
    def test_a_group_carries_only_what_is_set_at_it(self) -> None:
        at = parse_graph(REAL_GRAPH)
        assert sorted(at["@all"]) == ["region", "shared"]
        assert sorted(at["@prod"]) == ["shared", "tier"]
        assert sorted(at["@prod_web"]) == ["ansible_ssh_common_args", "role"]
        # `prod_db` sets nothing, and that is a real answer rather than absence.
        assert at["@prod_db"] == {}

    def test_a_host_line_is_its_EFFECTIVE_set_not_its_own_vars(self) -> None:
        # The fact that makes `attribute` necessary: `web2` has no `host_vars`
        # at all and its line still carries five variables.
        at = parse_graph(REAL_GRAPH)
        assert sorted(at["web2"]) == [
            "ansible_ssh_common_args",
            "region",
            "role",
            "shared",
            "tier",
        ]

    def test_a_value_containing_spaces_and_quotes_survives_whole(self) -> None:
        # The ambiguity F8 is about: `formatVars` space-joined `k=v` pairs, so a
        # value with a space was unreadable. This is the real
        # `ansible_ssh_common_args`, which routinely has both.
        at = parse_graph(REAL_GRAPH)
        assert at["@prod_web"]["ansible_ssh_common_args"] == (
            '-o ProxyCommand="ssh -W %h:%p bastion"'
        )

    def test_the_root_is_read_even_though_it_has_no_pipe_prefix(self) -> None:
        assert "@all" in parse_graph(REAL_GRAPH)

    def test_depth_is_counted_not_guessed(self) -> None:
        # `web1` sits at depth 3 and its vars at depth 4; a parser keying on
        # leading SPACES rather than pipes would attribute them to `prod_web`.
        at = parse_graph(REAL_GRAPH)
        assert "only_here" in at["web1"]
        assert "only_here" not in at["@prod_web"]

    def test_empty_input_is_an_empty_answer(self) -> None:
        assert parse_graph("") == {}


class TestAttribute:
    def test_a_value_from_all_is_attributed_to_all(self) -> None:
        assert _attr("web1")["region"]["origin"] == "all"

    def test_a_value_from_the_nearest_group_wins_over_a_further_one(self) -> None:
        # `shared` is set at `all` AND at `prod`; `web2` has no override, so
        # ansible's precedence gives `prod` -- and the chain shows both.
        got = _attr("web2")["shared"]
        assert got["origin"] == "prod"
        assert [c["level"] for c in got["chain"]] == ["all", "prod"]

    def test_a_host_override_reads_as_here_AND_keeps_the_chain_it_beat(self) -> None:
        # The case the first implementation got wrong by reporting
        # `unattributable`: a host's own `host_vars` are invisible in the graph,
        # so an unmatched key on a host IS the host's. The chain matters as much
        # as the origin -- "set here, beating prod and all" is the useful answer.
        got = _attr("web1")["shared"]
        assert got["origin"] == "here"
        assert [(c["level"], c["value"]) for c in got["chain"]] == [
            ("all", "all-loses"),
            ("prod", "prod-wins"),
        ]

    def test_a_key_no_group_sets_is_here_with_an_empty_chain(self) -> None:
        got = _attr("web1")["only_here"]
        assert got["origin"] == "here"
        assert got["chain"] == []

    def test_an_intermediate_group_is_attributed_to_itself(self) -> None:
        assert _attr("web1")["role"]["origin"] == "prod_web"
        assert _attr("web1")["ansible_ssh_common_args"]["origin"] == "prod_web"

    def test_a_sibling_branch_does_not_inherit_the_other_branch(self) -> None:
        # `stg1` is under `staging`, so `prod`'s `shared` must not reach it.
        got = _attr("stg1")["shared"]
        assert got["origin"] == "all"
        assert [c["level"] for c in got["chain"]] == ["all"]

    def test_the_reported_level_carries_no_sigil(self) -> None:
        # `@` is how `--graph` spells a group; a reader of the view never meets
        # it, so it is stripped on the way out.
        for entry in _attr("web2")["shared"]["chain"]:
            assert not str(entry["level"]).startswith("@")

    def test_the_effective_value_is_returned_with_its_real_type(self) -> None:
        # `--graph` stringifies (`True`), `--list` does not. The view renders
        # YAML, so it needs the typed value and only the COMPARISON is textual.
        assert _attr("web1")["only_here"]["value"] is True

    @pytest.mark.parametrize("host", ["web1", "web2", "stg1"])
    def test_every_effective_key_gets_an_answer(self, host: str) -> None:
        # A missing key would silently drop a variable from a view whose whole
        # job is to show them all.
        assert set(_attr(host)) == set(HOSTVARS[host])

    def test_the_DEEPEST_level_wins_when_two_set_the_SAME_value(self) -> None:
        """The only case where the walk's DIRECTION is observable.

        Found by mutation, not by reading: flipping the chain walk to
        shallowest-first killed none of the other tests, because every one of
        them has two levels setting DIFFERENT values -- and then the first match
        going either way is the same level. Direction only matters when the
        values coincide, and then a shallowest-first walk credits `all` for
        something `prod` also sets.

        Fixture captured from the shipped image on an inventory where `all` and
        `prod` both say `shared: IDENTICAL`.
        """
        graph = """@all:
  |--@ungrouped:
  |--@prod:
  |  |--web1
  |  |  |--{shared = IDENTICAL}
  |  |--{shared = IDENTICAL}
  |--{shared = IDENTICAL}
"""
        got = attribute(
            node="web1",
            is_host=True,
            effective={"shared": "IDENTICAL"},
            groups_of_node=["prod"],
            at_level=parse_graph(graph),
            children={"all": ["ungrouped", "prod"]},
        )["shared"]
        # Both levels are in the chain, shallowest first...
        assert [c["level"] for c in got["chain"]] == ["all", "prod"]
        # ...and the DEEPEST is the one credited.
        assert got["origin"] == "prod"

    def test_a_group_node_with_an_unexplained_value_says_so(self) -> None:
        # For a GROUP the node itself is in the chain, so nothing matching means
        # this module cannot explain it -- and a wrong attribution in a view
        # whose purpose is attribution is worse than none.
        got = attribute(
            node="prod",
            is_host=False,
            effective={"mystery": "from-somewhere-else"},
            groups_of_node=[],
            at_level=parse_graph(REAL_GRAPH),
            children=CHILDREN,
        )
        assert got["mystery"]["origin"] == "unattributable"
