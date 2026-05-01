"""Tests for `terrapod.logging_config`.

Why these tests exist
---------------------
Uvicorn ships its own `uvicorn`, `uvicorn.access`, and `uvicorn.error`
loggers with non-JSON `StreamHandler`s installed by default. Without
intervention, every request produces a plaintext line like
`INFO:     10.x.y.z - "GET /path" 200 OK` interleaved with our
structlog JSON, which breaks log ingestion pipelines that expect
one-record-per-line JSON. `configure_logging` clears those handlers
and forces propagation so the records flow through our root JSON
formatter instead.

Regressing this would mean log shippers silently dropping uvicorn
events as malformed JSON — exactly the kind of breakage that's
invisible until someone goes looking.
"""

from __future__ import annotations

import io
import json
import logging

from terrapod.logging_config import configure_logging


def _install_uvicorn_default_handlers() -> None:
    """Mimic uvicorn's startup-time logger setup so the test starts
    from the same state production does — uvicorn loggers with their
    own handlers and `propagate=False`, which would otherwise emit
    plaintext lines that bypass our root handler."""
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.addHandler(logging.StreamHandler(io.StringIO()))
        lg.propagate = False
        lg.setLevel(logging.INFO)


def test_configure_logging_redirects_uvicorn_loggers_through_root():
    _install_uvicorn_default_handlers()
    configure_logging(json_logs=True, log_level="INFO")

    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        assert lg.handlers == [], (
            f"uvicorn handler not cleared on {name}; logs would emit twice "
            "(once via uvicorn's own plaintext handler, once via root JSON)"
        )
        assert lg.propagate is True, (
            f"{name} not set to propagate; records would never reach the "
            "root JSON handler and would be dropped silently"
        )


def test_uvicorn_access_record_emits_as_json(capsys):
    """Drive the full path: install uvicorn's default handlers, run
    configure_logging, then log an access-shaped record. The captured
    stdout must be valid JSON with our app context — proving the
    handler chain rewires correctly end-to-end, not just at the
    handler-list level."""
    _install_uvicorn_default_handlers()
    configure_logging(json_logs=True, log_level="INFO")

    logging.getLogger("uvicorn.access").info('10.0.0.1 - "GET /health" 200 OK')

    captured = capsys.readouterr().out.strip().splitlines()
    # The last line is ours; earlier lines may be from the
    # configure_logging startup (`logging.basicConfig` smokes one too).
    record = json.loads(captured[-1])
    assert record["app"] == "terrapod-api"
    assert record["logger"] == "uvicorn.access"
    assert record["level"] == "info"
    assert "GET /health" in record["event"]
