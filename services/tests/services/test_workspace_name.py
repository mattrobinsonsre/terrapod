"""The workspace-name contract (#1299).

A workspace name is not cosmetic — it is the key the `cloud {}` block matches
on, the `/app/{org}/{name}` redirect target, an entry in the DR state index,
and part of VCS status contexts. A name that violates the format is a
workspace some of those surfaces cannot address.

The rule was previously private to `tfe_v2`, and the newest workspace-creating
path (undelete/restore) checked only "non-empty string". These pin the shared
rule so the next caller inherits it rather than reinventing a weaker one.
"""

import pytest

from terrapod.services.workspace_name import (
    MAX_WORKSPACE_NAME_LENGTH,
    validate_workspace_name,
)


class TestAccepts:
    @pytest.mark.parametrize(
        "name",
        [
            "prod",
            "core-dev-eu1",
            "web_app",
            "a",
            "0",
            "9lives",
            "A-Z_0-9",
        ],
    )
    def test_valid_names(self, name):
        assert validate_workspace_name(name) == name

    def test_surrounding_whitespace_is_trimmed_not_rejected(self):
        assert validate_workspace_name("  prod  ") == "prod"

    def test_exactly_at_the_limit(self):
        name = "a" * MAX_WORKSPACE_NAME_LENGTH
        assert validate_workspace_name(name) == name


class TestRejects:
    @pytest.mark.parametrize(
        ("name", "why"),
        [
            ("", "empty"),
            ("   ", "whitespace only"),
            ("-leading-hyphen", "must start alphanumeric"),
            ("_leading-underscore", "must start alphanumeric"),
            ("has space", "space"),
            ("../etc/passwd", "path traversal"),
            ("dots.are.not.allowed", "dot"),
            ("emoji-✨", "non-ascii"),
            ("slash/in/name", "slash"),
            ("percent%20encoded", "percent"),
        ],
    )
    def test_invalid_names(self, name, why):
        with pytest.raises(ValueError):
            validate_workspace_name(name)

    def test_over_the_limit(self):
        with pytest.raises(ValueError, match=str(MAX_WORKSPACE_NAME_LENGTH)):
            validate_workspace_name("a" * (MAX_WORKSPACE_NAME_LENGTH + 1))

    def test_none_is_rejected_rather_than_crashing(self):
        with pytest.raises(ValueError):
            validate_workspace_name(None)  # type: ignore[arg-type]


def test_the_error_says_what_the_rule_is():
    """The message is user-facing (it becomes a 422 detail), so it has to
    explain the rule rather than merely assert a failure."""
    with pytest.raises(ValueError) as e:
        validate_workspace_name("has space")
    msg = str(e.value)
    assert "letter or number" in msg
    assert "hyphens" in msg
