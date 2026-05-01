"""Tests for the FastAPI wrapper that translates LabelValidationError → HTTP 422."""

import pytest
from fastapi import HTTPException

from terrapod.api.labels import validate_labels


class TestValidateLabelsWrapper:
    def test_clean_input_returns_dict(self):
        assert validate_labels({"env": "prod"}) == {"env": "prod"}

    def test_none_returns_empty_dict(self):
        assert validate_labels(None) == {}

    def test_reserved_key_translated_to_422(self):
        """LabelValidationError → HTTPException with status 422 and the
        original detail message preserved (so the user sees which key)."""
        with pytest.raises(HTTPException) as exc:
            validate_labels({"status": "live"})
        assert exc.value.status_code == 422
        assert "status" in exc.value.detail
        assert "reserved" in exc.value.detail.lower()

    def test_size_limit_translated_to_422(self):
        with pytest.raises(HTTPException) as exc:
            validate_labels({"k": "x" * 1000})
        assert exc.value.status_code == 422

    def test_other_exceptions_propagate(self):
        """Non-LabelValidationError exceptions must NOT be caught — they
        indicate programmer error and should reach the global handler.
        Today the validator can't raise anything else, but the wrapper
        contract should keep that promise."""

        from unittest.mock import patch

        with patch("terrapod.api.labels._validate_labels", side_effect=RuntimeError("oops")):
            with pytest.raises(RuntimeError):
                validate_labels({"env": "prod"})
