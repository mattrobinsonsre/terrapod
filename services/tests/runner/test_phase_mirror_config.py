"""Tests for terrapod.runner.phases.mirror_config."""

from __future__ import annotations

from pathlib import Path

from terrapod.runner.phases import mirror_config


class TestWriteTerraformRc:
    def test_https_writes_full_mirror_block(self, tmp_path) -> None:
        out = tmp_path / "terraform.rc"
        result = mirror_config.write_terraform_rc(
            api_url="https://api.example.com",
            auth_token="tok",
            config_path=out,
        )
        text = out.read_text()
        assert result.mirror_configured is True
        assert result.public_host_redirect is False
        assert 'credentials "api.example.com"' in text
        assert "provider_installation" in text
        assert "https://api.example.com/v1/providers/" in text
        assert 'exclude = ["api.example.com/*/*"]' in text
        assert 'include = ["api.example.com/*/*"]' in text

    def test_http_skips_mirror_block_but_writes_credentials(self, tmp_path) -> None:
        out = tmp_path / "terraform.rc"
        result = mirror_config.write_terraform_rc(
            api_url="http://api.example.com",
            auth_token="tok",
            config_path=out,
        )
        text = out.read_text()
        assert result.mirror_configured is False
        assert 'credentials "api.example.com"' in text
        assert "provider_installation" not in text

    def test_public_host_redirect(self, tmp_path) -> None:
        out = tmp_path / "terraform.rc"
        result = mirror_config.write_terraform_rc(
            api_url="https://api.internal",
            auth_token="tok",
            public_api_url="https://terrapod.example.com",
            config_path=out,
        )
        text = out.read_text()
        assert result.public_host_redirect is True
        # Both native hosts in exclude/include lists.
        assert '"api.internal/*/*"' in text
        assert '"terrapod.example.com/*/*"' in text
        # Public-host redirect block.
        assert 'host "terrapod.example.com"' in text
        # Credentials for both hosts.
        assert 'credentials "api.internal"' in text
        assert 'credentials "terrapod.example.com"' in text
        # tfe.v2 + minors all present (required for
        # data "terraform_remote_state" backend = "remote" on the
        # public host).
        assert '"tfe.v2"       = "https://api.internal/api/v2/"' in text
        assert '"tfe.v2.1"     = "https://api.internal/api/v2/"' in text
        assert '"tfe.v2.2"     = "https://api.internal/api/v2/"' in text

    def test_same_public_and_internal_host_no_redirect(self, tmp_path) -> None:
        out = tmp_path / "terraform.rc"
        result = mirror_config.write_terraform_rc(
            api_url="https://api.example.com",
            auth_token="tok",
            public_api_url="https://api.example.com",
            config_path=out,
        )
        text = out.read_text()
        assert result.public_host_redirect is False
        assert "host " not in text  # no host{} block

    def test_no_api_url_short_circuits(self, tmp_path) -> None:
        out = tmp_path / "terraform.rc"
        result = mirror_config.write_terraform_rc(
            api_url="",
            auth_token="",
            config_path=out,
        )
        assert result.mirror_configured is False
        assert not out.exists()


class TestExportEnv:
    def test_sets_required_env_vars(self) -> None:
        env = mirror_config.export_env(
            config_path=Path("/tmp/terraform.rc"),
            env={"FOO": "bar"},
        )
        assert env["TF_CLI_CONFIG_FILE"] == "/tmp/terraform.rc"
        assert env["TF_REGISTRY_CLIENT_TIMEOUT"] == "30"
        assert env["TF_PROVIDER_DOWNLOAD_RETRY"] == "3"
        assert env["FOO"] == "bar"  # passthrough
