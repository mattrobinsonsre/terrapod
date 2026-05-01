"""FastAPI helper that translates label-validation errors to HTTP 422.

The pure validator lives in `terrapod.services.label_validation` and is
web-framework-agnostic. Routers should import the wrapper here so they
get a single-call surface that raises `HTTPException` directly.
"""

from fastapi import HTTPException

from terrapod.services.label_validation import (
    LabelValidationError,
)
from terrapod.services.label_validation import (
    validate_labels as _validate_labels,
)


def validate_labels(labels: dict | None) -> dict:
    """Validate labels and translate any `LabelValidationError` to HTTP 422.

    Routers calling this get the same shape as the underlying validator
    (clean dict in, clean dict out) but with HTTP semantics on failure.
    Any other exception type (programmer error, etc.) propagates
    untranslated so the global exception handler can log it as a 500.
    """
    try:
        return _validate_labels(labels)
    except LabelValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
