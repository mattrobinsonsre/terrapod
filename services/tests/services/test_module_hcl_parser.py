"""Unit tests for module HCL parser."""

import io
import tarfile

from terrapod.services.module_hcl_parser import (
    MAX_INTERFACE_ERROR_LENGTH,
    extract_module_interface,
    extract_module_interface_from_file,
    extract_module_interface_result,
    extract_module_interface_result_from_file,
)


def _make_tarball(files: dict[str, str]) -> bytes:
    """Create an in-memory gzipped tarball with the given path->content mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestExtractModuleInterface:
    def test_extracts_basic_variable(self):
        tarball = _make_tarball(
            {
                "variables.tf": """
variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC"
  default     = "10.0.0.0/16"
}
""",
            }
        )
        result = extract_module_interface(tarball)
        assert len(result["inputs"]) == 1
        inp = result["inputs"][0]
        assert inp["name"] == "vpc_cidr"
        assert inp["type"] == "string"
        assert inp["type_schema"] == {"type": "string"}
        assert inp["description"] == "CIDR block for the VPC"
        assert inp["default"] == "10.0.0.0/16"
        assert inp["required"] is False
        assert inp["sensitive"] is False

    def test_extracts_required_variable(self):
        tarball = _make_tarball(
            {
                "variables.tf": """
variable "region" {
  type        = string
  description = "AWS region"
}
""",
            }
        )
        result = extract_module_interface(tarball)
        inp = result["inputs"][0]
        assert inp["name"] == "region"
        assert inp["required"] is True
        assert inp["default"] is None

    def test_extracts_sensitive_variable(self):
        tarball = _make_tarball(
            {
                "variables.tf": """
variable "db_password" {
  type      = string
  sensitive = true
}
""",
            }
        )
        result = extract_module_interface(tarball)
        inp = result["inputs"][0]
        assert inp["sensitive"] is True

    def test_extracts_output(self):
        tarball = _make_tarball(
            {
                "outputs.tf": """
output "vpc_id" {
  value       = module.vpc.id
  description = "ID of the created VPC"
}
""",
            }
        )
        result = extract_module_interface(tarball)
        assert len(result["outputs"]) == 1
        out = result["outputs"][0]
        assert out["name"] == "vpc_id"
        assert out["description"] == "ID of the created VPC"
        assert out["sensitive"] is False

    def test_extracts_sensitive_output(self):
        tarball = _make_tarball(
            {
                "outputs.tf": """
output "secret" {
  value     = var.secret
  sensitive = true
}
""",
            }
        )
        result = extract_module_interface(tarball)
        assert result["outputs"][0]["sensitive"] is True

    def test_multiple_files(self):
        tarball = _make_tarball(
            {
                "variables.tf": """
variable "name" {
  type = string
}
""",
                "outputs.tf": """
output "id" {
  value = aws_instance.main.id
}
""",
                "main.tf": """
resource "aws_instance" "main" {
  ami = "ami-123"
}
""",
            }
        )
        result = extract_module_interface(tarball)
        assert len(result["inputs"]) == 1
        assert len(result["outputs"]) == 1

    def test_ignores_nested_tf_files(self):
        tarball = _make_tarball(
            {
                "variables.tf": 'variable "top" { type = string }',
                "modules/sub/variables.tf": 'variable "nested" { type = string }',
            }
        )
        result = extract_module_interface(tarball)
        assert len(result["inputs"]) == 1
        assert result["inputs"][0]["name"] == "top"

    def test_reads_dot_slash_prefixed_entries_as_root(self):
        """`tar -czf m.tgz -C dir .` -- the documented command -- prefixes every
        entry with `./`. Those are root files, not nested ones (#1707)."""
        tarball = _make_tarball(
            {
                "./variables.tf": 'variable "region" { type = string }',
                "./outputs.tf": 'output "id" { value = "x" }',
                "./modules/sub/variables.tf": 'variable "nested" { type = string }',
            }
        )
        result = extract_module_interface(tarball)
        assert [i["name"] for i in result["inputs"]] == ["region"]
        assert [o["name"] for o in result["outputs"]] == ["id"]

    def test_returns_empty_on_no_tf_files(self):
        tarball = _make_tarball({"README.md": "# Module"})
        result = extract_module_interface(tarball)
        assert result == {"inputs": [], "outputs": []}

    def test_returns_empty_on_malformed_hcl(self):
        tarball = _make_tarball({"variables.tf": "this is not { valid hcl ["})
        result = extract_module_interface(tarball)
        assert result == {"inputs": [], "outputs": []}

    def test_complex_type(self):
        tarball = _make_tarball(
            {
                "variables.tf": """
variable "tags" {
  type        = map(string)
  description = "Resource tags"
  default     = {}
}
""",
            }
        )
        result = extract_module_interface(tarball)
        inp = result["inputs"][0]
        assert inp["name"] == "tags"
        assert inp["required"] is False
        assert inp["type"] == "map(string)"
        assert inp["type_schema"] == {"type": "object", "additionalProperties": {"type": "string"}}

    def test_type_schema_for_list(self):
        tarball = _make_tarball(
            {
                "variables.tf": """
variable "simple" {
  type = string
}
variable "items" {
  type = list(number)
}
""",
            }
        )
        result = extract_module_interface(tarball)
        simple = next(i for i in result["inputs"] if i["name"] == "simple")
        items = next(i for i in result["inputs"] if i["name"] == "items")
        assert simple["type_schema"] == {"type": "string"}
        assert items["type_schema"] == {"type": "array", "items": {"type": "number"}}
        assert items["type"] == "list(number)"


class TestInterfaceFailureIsReported:
    """#1707: a failed parse must be distinguishable from a module with no
    variables. The catalog builds its form from the interface, so a swallowed
    failure became an item with no inputs and no error anywhere."""

    def test_a_clean_parse_reports_no_error(self):
        tarball = _make_tarball({"variables.tf": 'variable "region" {}'})
        result = extract_module_interface_result(tarball)
        assert result["error"] is None
        assert [i["name"] for i in result["inputs"]] == ["region"]

    def test_a_module_with_no_variables_reports_no_error(self):
        result = extract_module_interface_result(_make_tarball({"main.tf": ""}))
        assert result == {"inputs": [], "outputs": [], "error": None}

    def test_a_corrupt_archive_reports_a_reason(self):
        result = extract_module_interface_result(b"this is not a gzip tarball")
        assert result["inputs"] == [] and result["outputs"] == []
        assert result["error"] == (
            "The module archive could not be read as a gzip-compressed tar file."
        )

    def test_a_truncated_archive_reports_a_reason(self):
        tarball = _make_tarball({"variables.tf": 'variable "region" {}\n' * 2000})
        result = extract_module_interface_result(tarball[: len(tarball) // 2])
        assert result["error"] is not None

    def test_a_corrupt_archive_on_disk_reports_a_reason(self, tmp_path):
        path = tmp_path / "module.tar.gz"
        path.write_bytes(b"\x1f\x8bnot really gzip")
        result = extract_module_interface_result_from_file(str(path))
        assert result["error"] == (
            "The module archive could not be read as a gzip-compressed tar file."
        )
        assert str(tmp_path) not in result["error"]

    def test_a_missing_file_does_not_leak_its_path(self, tmp_path):
        missing = tmp_path / "secret-dir" / "gone.tar.gz"
        result = extract_module_interface_result_from_file(str(missing))
        assert result["error"] is not None
        assert "secret-dir" not in result["error"]

    def test_an_unparseable_tf_file_is_named_with_its_position(self):
        tarball = _make_tarball(
            {
                "good.tf": 'variable "region" {}',
                "broken.tf": 'variable "x" {\n  type = \n}',
            }
        )
        result = extract_module_interface_result(tarball)
        assert result["error"] == "broken.tf: invalid HCL at line 2, column 10"
        # The files that did parse still contribute their declarations.
        assert [i["name"] for i in result["inputs"]] == ["region"]

    def test_the_reason_never_carries_the_source_text(self):
        tarball = _make_tarball({"main.tf": 'variable "x" { default = "hunter2-secret" ['})
        result = extract_module_interface_result(tarball)
        assert result["error"] is not None
        assert "hunter2" not in result["error"]
        assert "Token" not in result["error"]

    def test_a_dot_slash_entry_is_named_without_the_prefix(self):
        # `tar -czf m.tgz -C dir .` names entries `./main.tf`.
        result = extract_module_interface_result(_make_tarball({"./main.tf": "}}}"}))
        assert result["error"].startswith("main.tf: invalid HCL")

    def test_every_broken_file_is_reported(self):
        tarball = _make_tarball({"a.tf": "}}}", "b.tf": "{{{"})
        result = extract_module_interface_result(tarball)
        assert "a.tf:" in result["error"]
        assert "b.tf:" in result["error"]

    def test_the_reason_is_bounded(self):
        files = {f"file_{n:03d}_with_a_long_name.tf": "}}}" for n in range(60)}
        result = extract_module_interface_result(_make_tarball(files))
        assert len(result["error"]) <= MAX_INTERFACE_ERROR_LENGTH
        assert result["error"].endswith("...")

    def test_the_existing_functions_keep_their_shape(self, tmp_path):
        tarball = _make_tarball({"broken.tf": "}}}"})
        path = tmp_path / "m.tar.gz"
        path.write_bytes(tarball)
        assert extract_module_interface(tarball) == {"inputs": [], "outputs": []}
        assert extract_module_interface_from_file(str(path)) == {"inputs": [], "outputs": []}
