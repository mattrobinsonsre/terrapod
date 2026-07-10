"""API route contract gate (#550) — fails CI when a route is removed or renamed.

Terrapod is heading to v1.0.0 with a "no breaking API changes" guarantee. The
route surface is a contract for consumers that can lag the server across version
skew: the `terraform`/`tofu` `cloud` backend + `go-tfe` on `/api/v2/`, and the
runner + listener wire protocol on `/api/terrapod/v1/` (remote clusters, several
minors behind during a rolling upgrade). Removing or renaming any route breaks
one of them.

This test pins the full route set (`terrapod.api.contract.route_signatures`)
against a committed snapshot (`api_route_contract.json`) and fails on any diff:

- A **removed / renamed** route is a **breaking change**. It must NOT be
  accepted by regenerating the snapshot within a major version — it needs a
  MAJOR bump or a documented deprecation window.
- A **new** route is additive and safe; accept it by regenerating the snapshot
  (a conscious, reviewed act, so additions never slip in silently).

Regenerate after an intentional additive change:

    UPDATE_API_CONTRACT=1 pytest tests/api/test_route_contract.py

(or `make test` with that env exported into the container).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from terrapod.api.app import app
from terrapod.api.contract import diff_route_contract, route_signatures

_SNAPSHOT = Path(__file__).parent / "api_route_contract.json"


def test_route_contract_unchanged() -> None:
    current = route_signatures(app)

    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n")
        return

    assert _SNAPSHOT.exists(), (
        f"Route contract snapshot missing at {_SNAPSHOT}. Generate it once with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/api/test_route_contract.py"
    )
    snapshot = json.loads(_SNAPSHOT.read_text())
    removed, added = diff_route_contract(snapshot, current)

    problems: list[str] = []
    if removed:
        problems.append(
            "BREAKING: these API routes were REMOVED or RENAMED. A consumer that "
            "lags the server (terraform/tofu cloud backend, go-tfe, a runner, or a "
            "listener) will break. This requires a MAJOR version bump or a "
            "documented deprecation window — do NOT just regenerate the snapshot:\n"
            "  " + "\n  ".join(removed)
        )
    if added:
        problems.append(
            "New routes were added (additive, non-breaking). Accept them by "
            "regenerating the contract snapshot:\n"
            "  UPDATE_API_CONTRACT=1 pytest tests/api/test_route_contract.py\n"
            "Added:\n  " + "\n  ".join(added)
        )

    assert not problems, "\n\n".join(problems)


def test_diff_detects_removal_and_addition() -> None:
    # Proves the gate actually bites: a dropped signature is flagged as removed
    # (breaking) and a new one as added (additive) — not a trivially-passing test.
    snapshot = ["DELETE /api/v2/x", "GET /api/v2/x", "GET /api/v2/y"]
    current = ["GET /api/v2/x", "GET /api/v2/y", "GET /api/v2/z"]
    removed, added = diff_route_contract(snapshot, current)
    assert removed == ["DELETE /api/v2/x"]
    assert added == ["GET /api/v2/z"]


def test_committed_snapshot_is_nonempty_and_covers_both_surfaces() -> None:
    # Guard against an empty/garbage snapshot silently disabling the gate, and
    # confirm it spans the two frozen prefixes.
    snapshot = json.loads(_SNAPSHOT.read_text())
    assert len(snapshot) > 100
    assert any(s.endswith(" /api/v2/ping") or "/api/v2/" in s for s in snapshot)
    assert any("/api/terrapod/v1/listeners/" in s for s in snapshot)
