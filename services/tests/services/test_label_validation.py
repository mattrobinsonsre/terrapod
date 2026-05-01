"""Tests for the pure label validator (size limits + reserved-key check)."""

import pytest

from terrapod.services.label_validation import (
    MAX_LABEL_KEY_LEN,
    MAX_LABEL_VALUE_LEN,
    MAX_LABELS,
    RESERVED_LABEL_KEYS,
    LabelValidationError,
    validate_labels,
)


class TestShape:
    def test_none_returns_empty_dict(self):
        assert validate_labels(None) == {}

    def test_empty_dict_returns_empty_dict(self):
        assert validate_labels({}) == {}

    def test_non_dict_input_raises(self):
        with pytest.raises(LabelValidationError):
            validate_labels(["not", "a", "dict"])

    def test_clean_dict_passes_through(self):
        labels = {"env": "prod", "team": "platform"}
        assert validate_labels(labels) == labels

    def test_value_error_subtype(self):
        """LabelValidationError must be a ValueError so callers using a
        bare `except ValueError` still catch it."""
        with pytest.raises(ValueError):
            validate_labels({"status": "live"})


class TestSizeLimits:
    def test_too_many_labels_rejected(self):
        labels = {f"k{i}": "v" for i in range(MAX_LABELS + 1)}
        with pytest.raises(LabelValidationError) as exc:
            validate_labels(labels)
        assert str(MAX_LABELS) in str(exc.value)

    def test_max_labels_exactly_passes(self):
        labels = {f"k{i}": "v" for i in range(MAX_LABELS)}
        assert validate_labels(labels) == labels

    def test_long_key_rejected(self):
        with pytest.raises(LabelValidationError) as exc:
            validate_labels({"k" * (MAX_LABEL_KEY_LEN + 1): "v"})
        assert "label key" in str(exc.value)

    def test_long_value_rejected(self):
        with pytest.raises(LabelValidationError) as exc:
            validate_labels({"k": "v" * (MAX_LABEL_VALUE_LEN + 1)})
        assert "label value" in str(exc.value)

    def test_non_string_key_rejected(self):
        with pytest.raises(LabelValidationError):
            validate_labels({123: "v"})

    def test_non_string_value_rejected(self):
        with pytest.raises(LabelValidationError):
            validate_labels({"k": 123})


class TestReservedKeys:
    """Reserved keys are virtual filter fields — labels with those keys
    would collide with filter syntax and are rejected by the validator.
    """

    @pytest.mark.parametrize("reserved", sorted(RESERVED_LABEL_KEYS))
    def test_each_reserved_key_rejected(self, reserved):
        with pytest.raises(LabelValidationError) as exc:
            validate_labels({reserved: "any-value"})
        msg = str(exc.value)
        assert reserved in msg
        # Error message must list the full reserved set so admins can
        # learn the restriction without grepping the source.
        for key in RESERVED_LABEL_KEYS:
            assert key in msg

    def test_reserved_keys_locked_in(self):
        """The set is documented in rbac.md and depended on by the frontend
        filter parser. Lock it to flag any drift in review."""
        assert RESERVED_LABEL_KEYS == frozenset(
            {
                "status",
                "pool",
                "mode",
                "backend",
                "owner",
                "drift",
                "version",
                "vcs",
                "locked",
                "branch",
            }
        )

    def test_clean_keys_alongside_reserved_still_rejected(self):
        """Mixed input with one reserved key still rejects — no partial accept."""
        with pytest.raises(LabelValidationError):
            validate_labels({"env": "prod", "status": "live"})

    def test_non_reserved_keys_pass(self):
        """Common label conventions (env, team, repo, …) must still work —
        they're how customers organise workspaces today.
        """
        labels = {
            "env": "prod",
            "team": "platform",
            "repo": "tf-aws-core",
            "scope": "core",
            "region": "eu-west-1",
            "managed-by": "terrapod-provider",
        }
        assert validate_labels(labels) == labels
