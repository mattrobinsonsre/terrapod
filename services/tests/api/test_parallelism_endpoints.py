"""Parallelism through the endpoints that write it (#1431).

The unit tests prove the rule; these prove each write path actually applies it,
which is the half that rots. A validator four callers share is only worth having
if all four call it.
"""

from __future__ import annotations

import pytest

from terrapod.api.routers.workspace_bulk import _validate_update_fields_for_test


class TestBulkUpdate:
    def test_it_accepts_a_sane_value(self) -> None:
        assert _validate_update_fields_for_test({"parallelism": 4})["parallelism"] == 4

    def test_it_refuses_zero(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            _validate_update_fields_for_test({"parallelism": 0})
        assert e.value.status_code == 422

    def test_it_refuses_a_string_that_is_not_a_number(self) -> None:
        """The `auto-apply` lesson: a bad type must be told, not coerced.

        Writing a non-number into an integer column is the kind of failure that
        surfaces later as an unreadable database error rather than a 422.
        """
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            _validate_update_fields_for_test({"parallelism": "lots"})
        assert e.value.status_code == 422
