"""Unit tests for the deterministic onboarding polish core (#824 Phase A).

This is the safety-critical piece: it applies the AI's naming decisions to the
raw discovery output while guaranteeing — by construction — that no attribute
value or import id is ever altered. These tests pin that guarantee and every
fail-closed path, so a future change that lets a value drift fails CI loudly.
"""

import pytest

from terrapod.services.onboarding_polish import (
    PolishError,
    ResourcePolish,
    apply_polish,
    assert_values_preserved,
    pair_config_and_imports,
    split_resource_blocks,
)

RAW_CFG = """# __generated__ by OpenTofu
resource "aws_eip" "eipalloc_0ccdb1" {
  domain               = "vpc"
  network_border_group = "eu-west-1"
  tags = {
    Name = "nat-eu-west-1a"
  }
}

resource "aws_eip" "eipalloc_0ffee2" {
  domain = "vpc"
  tags = {
    Name = "nat-eu-west-1b"
  }
}

resource "aws_vpc" "vpc_0abc" {
  cidr_block = "10.0.0.0/16"
  tags = {
    Name = "core"
  }
}
"""

RAW_IMPORTS = """import {
  to = aws_eip.eipalloc_0ccdb1
  id = "eipalloc-0ccdb1"
}

import {
  to = aws_eip.eipalloc_0ffee2
  id = "eipalloc-0ffee2"
}

import {
  to = aws_vpc.vpc_0abc
  id = "vpc-0abc"
}
"""


def _decisions():
    return [
        ResourcePolish(
            "aws_eip.eipalloc_0ccdb1", "nat_eu_west_1a", "Networking", "NAT EIP for AZ a"
        ),
        ResourcePolish("aws_eip.eipalloc_0ffee2", "nat_eu_west_1b", "Networking"),
        ResourcePolish("aws_vpc.vpc_0abc", "core", "Networking"),
    ]


def test_split_resource_blocks_parses_types_and_names():
    blocks = split_resource_blocks(RAW_CFG)
    assert [(b.rtype, b.name) for b in blocks] == [
        ("aws_eip", "eipalloc_0ccdb1"),
        ("aws_eip", "eipalloc_0ffee2"),
        ("aws_vpc", "vpc_0abc"),
    ]


def test_split_rejects_unterminated_block():
    with pytest.raises(PolishError):
        split_resource_blocks('resource "aws_eip" "x" {\n  domain = "vpc"\n')


def test_apply_polish_renames_groups_comments():
    res = apply_polish(RAW_CFG, RAW_IMPORTS, _decisions(), file_header="Onboarded estate")
    assert res.renamed == 3
    assert res.grouped == 1
    assert res.commented == 1
    assert 'resource "aws_eip" "nat_eu_west_1a"' in res.config
    assert "# Networking" in res.config
    assert "# NAT EIP for AZ a" in res.config
    assert "# Onboarded estate" in res.config


def test_values_preserved_including_non_defaults():
    res = apply_polish(RAW_CFG, RAW_IMPORTS, _decisions())
    # The deliberately-non-default attribute survives byte-identical.
    assert 'network_border_group = "eu-west-1"' in res.config
    # And the independent assertion agrees.
    assert_values_preserved(RAW_CFG, res.config)


def test_import_ids_untouched_and_targets_rewritten():
    res = apply_polish(RAW_CFG, RAW_IMPORTS, _decisions())
    # Every real id is preserved verbatim...
    assert 'id = "eipalloc-0ccdb1"' in res.import_blocks
    assert 'id = "eipalloc-0ffee2"' in res.import_blocks
    assert 'id = "vpc-0abc"' in res.import_blocks
    # ...while the `to =` addresses follow the rename in lockstep.
    assert "to = aws_eip.nat_eu_west_1a" in res.import_blocks
    assert "to = aws_eip.nat_eu_west_1b" in res.import_blocks
    assert "to = aws_vpc.core" in res.import_blocks
    # The old machine names are gone entirely from the imports.
    assert "eipalloc_0ccdb1" not in res.import_blocks


def test_empty_decisions_is_identity():
    res = apply_polish(RAW_CFG, RAW_IMPORTS, [])
    assert res.renamed == 0
    assert_values_preserved(RAW_CFG, res.config)
    assert res.import_blocks == RAW_IMPORTS  # nothing to rewrite


def test_partial_decisions_default_missing_to_identity():
    # Only rename one; the other two keep their names, still valid + preserved.
    res = apply_polish(RAW_CFG, RAW_IMPORTS, [ResourcePolish("aws_vpc.vpc_0abc", "core")])
    assert res.renamed == 1
    assert 'resource "aws_vpc" "core"' in res.config
    assert 'resource "aws_eip" "eipalloc_0ccdb1"' in res.config  # untouched
    assert_values_preserved(RAW_CFG, res.config)


def test_rejects_unknown_address():
    with pytest.raises(PolishError):
        apply_polish(RAW_CFG, RAW_IMPORTS, [ResourcePolish("aws_eip.does_not_exist", "x")])


def test_rejects_invalid_identifier():
    with pytest.raises(PolishError):
        apply_polish(RAW_CFG, RAW_IMPORTS, [ResourcePolish("aws_eip.eipalloc_0ccdb1", "1bad name")])


def test_rejects_name_collision_within_type():
    with pytest.raises(PolishError):
        apply_polish(
            RAW_CFG,
            RAW_IMPORTS,
            [
                ResourcePolish("aws_eip.eipalloc_0ccdb1", "dup"),
                ResourcePolish("aws_eip.eipalloc_0ffee2", "dup"),
            ],
        )


def test_same_name_across_different_types_is_allowed():
    # `core` for aws_vpc and `core` for aws_eip is fine — different types.
    res = apply_polish(
        RAW_CFG,
        RAW_IMPORTS,
        [
            ResourcePolish("aws_vpc.vpc_0abc", "core"),
            ResourcePolish("aws_eip.eipalloc_0ccdb1", "core"),
        ],
    )
    assert 'resource "aws_vpc" "core"' in res.config
    assert 'resource "aws_eip" "core"' in res.config


def test_assert_values_preserved_catches_drift():
    # A polished config where a value was changed must be caught.
    tampered = RAW_CFG.replace('cidr_block = "10.0.0.0/16"', 'cidr_block = "10.1.0.0/16"')
    with pytest.raises(PolishError):
        assert_values_preserved(RAW_CFG, tampered)


def test_pair_config_and_imports_interleaves_and_preserves():
    res = apply_polish(RAW_CFG, RAW_IMPORTS, _decisions())
    paired = pair_config_and_imports(res.config, res.import_blocks)
    # Every import block now sits directly above its resource block.
    for addr, name in [
        ("aws_eip.nat_eu_west_1a", "nat_eu_west_1a"),
        ("aws_eip.nat_eu_west_1b", "nat_eu_west_1b"),
        ("aws_vpc.core", "core"),
    ]:
        rtype = addr.split(".")[0]
        assert f"to = {addr}\n" in paired
        # the resource opener appears immediately after the import's closing brace
        assert f'}}\nresource "{rtype}" "{name}" {{' in paired
    # Group header + comments preserved; ids untouched; nothing lost.
    assert "# Networking" in paired and "# NAT EIP for AZ a" in paired
    assert 'id = "eipalloc-0ccdb1"' in paired
    assert paired.count("import {") == 3
    assert paired.count("resource ") == 3


def test_pair_config_and_imports_no_imports_returns_config():
    assert pair_config_and_imports(RAW_CFG, "") == RAW_CFG
    assert pair_config_and_imports("", RAW_IMPORTS) == ""


def test_pair_config_and_imports_keeps_unmatched_import():
    # An import whose resource isn't in the config is appended, never dropped.
    cfg = 'resource "aws_eip" "a" {\n  domain = "vpc"\n}\n'
    imp = (
        'import {\n  to = aws_eip.a\n  id = "eipalloc-a"\n}\n\n'
        'import {\n  to = aws_eip.ghost\n  id = "eipalloc-ghost"\n}\n'
    )
    paired = pair_config_and_imports(cfg, imp)
    assert 'id = "eipalloc-a"' in paired and 'id = "eipalloc-ghost"' in paired
    assert paired.count("import {") == 2


def test_comment_sanitised_to_single_line():
    res = apply_polish(
        RAW_CFG,
        RAW_IMPORTS,
        [ResourcePolish("aws_vpc.vpc_0abc", "core", comment="line one\nline two")],
    )
    # Only the first line survives; no injected newline breaks the block.
    assert "# line one" in res.config
    assert "line two" not in res.config
