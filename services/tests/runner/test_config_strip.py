"""Golden test for the entrypoint's `cloud`/`backend` block-stripping
(#344).

The runner entrypoint strips ``cloud {}`` and top-level ``backend "x" {}``
blocks from uploaded ``.tf`` files to prevent recursive backend use. We
need to guarantee this logic does **not** mangle a
``data "terraform_remote_state"`` block, because cross-workspace state
reads (#344) depend on those data sources surviving init.

The test reads the real awk program from ``docker/runner-entrypoint.sh``
(so a future change to the strip pattern is exercised) and runs it
against a fixture containing both kinds of blocks.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _find_entrypoint() -> Path:
    """Locate ``docker/runner-entrypoint.sh`` from either layout:
    - Local dev: <repo-root>/docker/runner-entrypoint.sh
      (this file at services/tests/runner/test_config_strip.py)
    - Docker test image: /app/docker/runner-entrypoint.sh
      (copied in by Dockerfile.test alongside services/)
    """
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "docker", here.parents[2] / "docker"):
        path = cand / "runner-entrypoint.sh"
        if path.is_file():
            return path
    pytest.skip("runner-entrypoint.sh not reachable from the test environment")


def _extract_strip_awk() -> str:
    """Pull the literal awk program out of the entrypoint script."""
    text = _find_entrypoint().read_text()
    match = re.search(
        r"# Strip cloud \{\} and backend \{\} blocks.*?awk '\n(.*?)\n\s*'",
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(
            "Could not locate the strip-awk program in runner-entrypoint.sh — "
            "the surrounding comment/structure has changed; update this test."
        )
    return match.group(1)


def _run_awk(program: str, source: str) -> str:
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("awk not available in test environment")
    result = subprocess.run(
        [awk, program],
        input=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_strip_removes_cloud_and_backend_blocks() -> None:
    program = _extract_strip_awk()
    source = (
        "cloud {\n"
        '  organization = "default"\n'
        '  workspaces { name = "w" }\n'
        "}\n"
        "\n"
        "terraform {\n"
        '  backend "remote" {\n'
        '    hostname = "terrapod.local"\n'
        '    organization = "default"\n'
        '    workspaces { name = "w" }\n'
        "  }\n"
        "}\n"
        "\n"
        'resource "null_resource" "n" {}\n'
    )
    out = _run_awk(program, source)
    assert "cloud {" not in out
    assert 'backend "remote"' not in out
    assert 'resource "null_resource" "n" {}' in out


def test_strip_preserves_terraform_remote_state_data_source() -> None:
    """The #344 critical case: a `data "terraform_remote_state"` block
    has a nested ``backend = "remote"`` *argument* (assignment, not a
    `{}` block) and a nested ``config { ... }`` block. The strip
    pattern must not consume any of it."""
    program = _extract_strip_awk()
    source = (
        'data "terraform_remote_state" "shared" {\n'
        '  backend = "remote"\n'
        "  config = {\n"
        '    hostname     = "terrapod.local"\n'
        '    organization = "default"\n'
        '    workspaces   = { name = "shared-network" }\n'
        "  }\n"
        "}\n"
        "\n"
        'output "vpc_id" {\n'
        "  value = data.terraform_remote_state.shared.outputs.vpc_id\n"
        "}\n"
    )
    out = _run_awk(program, source)
    # Every meaningful line of the data source survives intact.
    for required in (
        'data "terraform_remote_state" "shared" {',
        'backend = "remote"',
        "config = {",
        'hostname     = "terrapod.local"',
        'workspaces   = { name = "shared-network" }',
        "data.terraform_remote_state.shared.outputs.vpc_id",
    ):
        assert required in out, f"strip mangled line: {required!r}"


def test_strip_handles_both_in_one_file() -> None:
    """Mixed: a `cloud {}` block (must be stripped) and a
    `data "terraform_remote_state"` block (must survive) in the same
    file. Belt-and-braces against a regression where the strip's
    depth-tracking accidentally consumes following blocks."""
    program = _extract_strip_awk()
    source = (
        "cloud {\n"
        '  organization = "default"\n'
        '  workspaces { name = "consumer" }\n'
        "}\n"
        "\n"
        'data "terraform_remote_state" "shared" {\n'
        '  backend = "remote"\n'
        "  config = {\n"
        '    hostname     = "terrapod.local"\n'
        '    organization = "default"\n'
        '    workspaces   = { name = "producer" }\n'
        "  }\n"
        "}\n"
    )
    out = _run_awk(program, source)
    assert "cloud {" not in out
    assert 'data "terraform_remote_state" "shared" {' in out
    assert 'backend = "remote"' in out
    assert "config = {" in out
