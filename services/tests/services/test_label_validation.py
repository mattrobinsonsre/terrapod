"""Tests for shared label validation (size limits + reserved-key check)."""

import pytest
from fastapi import HTTPException

from terrapod.services.label_validation import (
    MAX_LABEL_KEY_LEN,
    MAX_LABEL_VALUE_LEN,
    MAX_LABELS,
    RESERVED_LABEL_KEYS,
    validate_labels,
)


class TestShape:
    def test_none_returns_empty_dict(self):
        assert validate_labels(None) == {}

    def test_empty_dict_returns_empty_dict(self):
        assert validate_labels({}) == {}

    def test_non_dict_input_raises_422(self):
        with pytest.raises(HTTPException) as exc:
            validate_labels(["not", "a", "dict"])
        assert exc.value.status_code == 422

    def test_clean_dict_passes_through(self):
        labels = {"env": "prod", "team": "platform"}
        assert validate_labels(labels) == labels


class TestSizeLimits:
    def test_too_many_labels_rejected(self):
        labels = {f"k{i}": "v" for i in range(MAX_LABELS + 1)}
        with pytest.raises(HTTPException) as exc:
            validate_labels(labels)
        assert exc.value.status_code == 422
        assert str(MAX_LABELS) in exc.value.detail

    def test_max_labels_exactly_passes(self):
        labels = {f"k{i}": "v" for i in range(MAX_LABELS)}
        assert validate_labels(labels) == labels

    def test_long_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_labels({"k" * (MAX_LABEL_KEY_LEN + 1): "v"})
        assert exc.value.status_code == 422
        assert "label key" in exc.value.detail

    def test_long_value_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_labels({"k": "v" * (MAX_LABEL_VALUE_LEN + 1)})
        assert exc.value.status_code == 422
        assert "label value" in exc.value.detail

    def test_non_string_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_labels({123: "v"})
        assert exc.value.status_code == 422

    def test_non_string_value_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_labels({"k": 123})
        assert exc.value.status_code == 422


class TestReservedKeys:
    """Reserved keys are virtual filter fields — labels with those keys
    would collide with filter syntax and are rejected at the API.
    """

    @pytest.mark.parametrize("reserved", sorted(RESERVED_LABEL_KEYS))
    def test_each_reserved_key_rejected(self, reserved):
        with pytest.raises(HTTPException) as exc:
            validate_labels({reserved: "any-value"})
        assert exc.value.status_code == 422
        assert reserved in exc.value.detail
        # Error message must list the full reserved set so admins can
        # learn the restriction without grepping the source.
        for key in RESERVED_LABEL_KEYS:
            assert key in exc.value.detail

    def test_reserved_keys_locked_in(self):
        """The 10 keys are documented (rbac.md) and depended on by the
        frontend filter parser. Lock the set to flag any drift in review."""
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
        """Mixed input with one reserved key still 422s — no partial accept."""
        with pytest.raises(HTTPException):
            validate_labels({"env": "prod", "status": "live"})

    def test_non_reserved_keys_pass(self):
        """Common label conventions (env, team, repo, …) must still work —
        they're how customers organise workspaces today."""
        labels = {
            "env": "prod",
            "team": "platform",
            "repo": "tf-aws-core",
            "scope": "core",
            "region": "eu-west-1",
            "managed-by": "terrapod-provider",
        }
        assert validate_labels(labels) == labels
