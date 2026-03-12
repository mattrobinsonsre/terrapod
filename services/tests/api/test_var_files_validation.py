"""Tests for var-files input validation."""

import pytest
from fastapi import HTTPException


class TestValidateVarFiles:
    """Unit tests for _validate_var_files."""

    def test_empty_list(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        assert _validate_var_files([]) == []

    def test_valid_paths(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        result = _validate_var_files(["envs/dev.tfvars", "variables.auto.tfvars"])
        assert result == ["envs/dev.tfvars", "variables.auto.tfvars"]

    def test_rejects_non_list(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="must be a list"):
            _validate_var_files("not-a-list")

    def test_rejects_non_string_entries(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="entries must be strings"):
            _validate_var_files([123])

    def test_rejects_empty_entries(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="non-empty"):
            _validate_var_files(["  "])

    def test_rejects_path_traversal_dotdot(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="invalid path"):
            _validate_var_files(["../secret.tfvars"])

    def test_rejects_absolute_path(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="invalid path"):
            _validate_var_files(["/etc/passwd"])

    def test_rejects_shell_metacharacters(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="invalid characters"):
            _validate_var_files(["$(whoami).tfvars"])

    def test_rejects_semicolon(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="invalid characters"):
            _validate_var_files(["a.tfvars; rm -rf /"])

    def test_rejects_too_many_entries(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        with pytest.raises(HTTPException, match="maximum 20"):
            _validate_var_files([f"file{i}.tfvars" for i in range(21)])

    def test_strips_whitespace(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        result = _validate_var_files(["  envs/dev.tfvars  "])
        assert result == ["envs/dev.tfvars"]

    def test_allows_hyphens_and_underscores(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        result = _validate_var_files(["my-vars_file.tfvars"])
        assert result == ["my-vars_file.tfvars"]

    def test_allows_spaces_in_paths(self):
        from terrapod.api.routers.tfe_v2 import _validate_var_files

        result = _validate_var_files(["my vars/dev.tfvars"])
        assert result == ["my vars/dev.tfvars"]
