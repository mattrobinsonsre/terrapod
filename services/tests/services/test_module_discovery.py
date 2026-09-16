"""Module discovery (#1584): which directories are proposed, and what they are called."""

import re

import pytest

from terrapod.services.module_discovery import (
    candidate_directories,
    repo_name_from_url,
    suggest_name,
    suggest_provider,
)

# The module-create form's rule, which every suggestion must satisfy.
NAME_RULE = re.compile(r"^[a-z][a-z0-9-]*$")


class TestCandidateDirectories:
    def test_the_root_first_then_submodules_by_path(self):
        paths = [
            "modules/update/main.tf",
            "main.tf",
            "variables.tf",
            "modules/create/main.tf",
            "modules/create/outputs.tf",
            "README.md",
        ]
        assert candidate_directories(paths) == ["", "modules/create", "modules/update"]

    def test_json_configuration_counts_and_variable_files_do_not(self):
        paths = [
            "json-mod/main.tf.json",
            "envs/prod/terraform.tfvars",
            "envs/prod/prod.tfvars.json",
        ]
        assert candidate_directories(paths) == ["json-mod"]

    @pytest.mark.parametrize(
        "path",
        [
            "examples/basic/main.tf",
            "modules/create/examples/complete/main.tf",
            "test/fixtures/main.tf",
            "tests/unit/main.tf",
            "modules/x/testdata/main.tf",
            ".github/workflows/x.tf",
            "modules/create/.terraform/modules/y/main.tf",
        ],
    )
    def test_examples_tests_and_hidden_directories_are_left_out(self, path):
        assert candidate_directories([path]) == []

    def test_a_repository_with_no_terraform_has_no_candidates(self):
        assert candidate_directories(["README.md", "src/app.py"]) == []


class TestSuggestions:
    def test_repo_name_from_url(self):
        assert repo_name_from_url("https://github.com/org/terraform-aws-vpc") == "terraform-aws-vpc"
        assert repo_name_from_url("https://gitlab.com/g/sub/repo.git") == "repo"
        assert repo_name_from_url("https://github.com/org/repo/") == "repo"

    def test_provider_from_the_conventional_repository_name(self):
        assert suggest_provider("terraform-azurerm-management-groups") == "azurerm"
        assert suggest_provider("Terraform-AWS-VPC") == "aws"
        assert suggest_provider("infra-modules") == ""

    @pytest.mark.parametrize(
        "repo,subdirectory,expected",
        [
            ("terraform-azurerm-management-groups", "", "management-groups"),
            ("terraform-azurerm-management-groups", "modules/create", "management-groups-create"),
            ("infra_modules", "network/VPC_Core", "infra-modules-vpc-core"),
            ("9lives", "", "m-9lives"),
            ("___", "", "module"),
        ],
    )
    def test_names(self, repo, subdirectory, expected):
        assert suggest_name(repo, subdirectory) == expected

    def test_every_suggestion_fits_the_create_rule(self):
        for repo in ["terraform-aws-vpc", "Weird.Repo", "1-two", "a" * 200]:
            for sub in ["", "modules/x", "deep/a-b/C_D", "x" * 100]:
                name = suggest_name(repo, sub)
                assert NAME_RULE.match(name), (repo, sub, name)
                assert len(name) <= 64
