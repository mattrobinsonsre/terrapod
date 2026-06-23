"""Tests for terrapod.runner.phases.tfvars — generating terrapod.auto.tfvars
from workspace terraform variables.

Covers the formatting contract (hcl→raw HCL expression, non-hcl→quoted string)
AND a real parse by `tofu`/`terraform` — the coverage that was missing when we
discovered (only by running the engines by hand) that complex variable handling
behaves differently for typed vs untyped variables and across engines. A tfvars
file parses identically on both, which is exactly why the runner uses it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from terrapod.runner.phases import tfvars

# ── Formatting contract ────────────────────────────────────────────────


class TestRenderTfvars:
    def test_non_hcl_is_quoted_string(self):
        body = tfvars.render_tfvars([{"key": "name", "value": "smokeapp", "hcl": False}])
        assert 'name = "smokeapp"' in body

    def test_non_hcl_string_that_looks_like_hcl_stays_string(self):
        # A literal "[a, b]" must NOT become a list.
        body = tfvars.render_tfvars([{"key": "s", "value": "[a, b]", "hcl": False}])
        assert 's = "[a, b]"' in body

    def test_hcl_value_is_raw(self):
        body = tfvars.render_tfvars(
            [
                {"key": "ports", "value": "[80, 443]", "hcl": True},
                {"key": "cfg", "value": '{"enabled": true}', "hcl": True},
            ]
        )
        assert "ports = [80, 443]" in body
        assert 'cfg = {"enabled": true}' in body

    def test_quotes_are_escaped(self):
        body = tfvars.render_tfvars([{"key": "k", "value": 'a "quoted" v', "hcl": False}])
        # json.dumps escapes the inner quotes into a valid HCL string literal.
        assert r'k = "a \"quoted\" v"' in body

    def test_keyless_skipped(self):
        body = tfvars.render_tfvars([{"value": "x", "hcl": False}, {"key": "k", "value": "1"}])
        assert "k = " in body
        assert body.count(" = ") == 1


class TestWriteAutoTfvars:
    def test_writes_file(self, tmp_path: Path):
        p = tfvars.write_auto_tfvars(tmp_path, [{"key": "k", "value": "v", "hcl": False}])
        assert p is not None and p.exists()
        assert p.name == "terrapod.auto.tfvars"
        assert 'k = "v"' in p.read_text()

    def test_empty_writes_nothing(self, tmp_path: Path):
        assert tfvars.write_auto_tfvars(tmp_path, []) is None
        assert not (tmp_path / "terrapod.auto.tfvars").exists()


# ── Real engine parse (the gap-closer) ─────────────────────────────────

_ENGINES = [e for e in ("tofu", "terraform") if shutil.which(e)]

_VARS = [
    {"key": "name", "value": "smokeapp", "hcl": False},
    {"key": "ports", "value": "[8080, 9090]", "hcl": True},
    {"key": "config", "value": '{"enabled": true, "timeout": 15}', "hcl": True},
    {"key": "tags", "value": "{}", "hcl": True},
    {"key": "secret", "value": "s3cr3t", "hcl": False},
]

# A consuming config with the *typed* variables a real module declares, plus an
# UNTYPED one (the catalog wrapper's case) — both must receive the right value
# from the generated tfvars file.
_MAIN_TF = """
variable "name"    { type = string }
variable "ports"   { type = list(number) }
variable "config"  { type = object({ enabled = bool, timeout = number }) }
variable "tags"    { type = map(string) }
variable "secret" {
  type      = string
  sensitive = true
}
variable "untyped" {}
output "name"    { value = var.name }
output "ports"   { value = var.ports }
output "config"  { value = var.config }
output "untyped" { value = var.untyped }
"""


@pytest.mark.skipif(not _ENGINES, reason="no tofu/terraform binary available")
@pytest.mark.parametrize("engine", _ENGINES)
def test_generated_tfvars_parses_on_engine(engine: str, tmp_path: Path):
    (tmp_path / "main.tf").write_text(_MAIN_TF)
    # Add an untyped var fed a JSON object via the tfvars file.
    vars_ = [*_VARS, {"key": "untyped", "value": '{"a": 1, "b": [2, 3]}', "hcl": True}]
    tfvars.write_auto_tfvars(tmp_path, vars_)

    res = subprocess.run(  # noqa: S603
        [engine, "plan", "-input=false", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, f"{engine} plan failed:\n{res.stdout}\n{res.stderr}"
    out = res.stdout
    # Structured values landed as their real types (not strings).
    assert "name" in out and "smokeapp" in out
    assert "8080" in out and "9090" in out  # list(number)
    assert "enabled" in out and "timeout" in out  # object
    # The untyped variable also received a structured object from the tfvars.
    assert '"untyped"' in out or "untyped" in out


@pytest.mark.skipif(not _ENGINES, reason="no tofu/terraform binary available")
@pytest.mark.parametrize("engine", _ENGINES)
def test_non_hcl_string_stays_string_on_engine(engine: str, tmp_path: Path):
    """A non-hcl value that looks like a list must remain a string."""
    (tmp_path / "main.tf").write_text(
        'variable "s" { type = string }\noutput "s" { value = var.s }\n'
    )
    tfvars.write_auto_tfvars(tmp_path, [{"key": "s", "value": "[not, a, list]", "hcl": False}])
    res = subprocess.run(  # noqa: S603
        [engine, "plan", "-input=false", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, f"{engine}: {res.stderr}"
    # Rendered as a quoted string in the tfvars, accepted by a string variable.
    assert json.dumps("[not, a, list]") in (tmp_path / "terrapod.auto.tfvars").read_text()
