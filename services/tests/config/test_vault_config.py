"""VaultConfig validation (#1439).

Every branch of these validators is a startup-time fail-fast: a misconfigured
`vault:` block should refuse to boot with a named cause rather than come up and
fail the first run that reaches Vault. These were shipped without tests; each
case below trips exactly one branch.
"""

import pytest
from pydantic import ValidationError

from terrapod.config import VaultConfig, VaultInstanceConfig


def _inst(name="default", **kw):
    kw.setdefault("address", "https://vault.example:8200")
    return VaultInstanceConfig(name=name, **kw)


class TestVaultConfigValidators:
    def test_disabled_needs_no_instances(self):
        # The common case: off, empty — must not raise.
        VaultConfig(enabled=False)

    def test_enabled_with_no_instances_is_rejected(self):
        with pytest.raises(ValidationError, match="no vault.instances"):
            VaultConfig(enabled=True, instances=[])

    def test_duplicate_instance_names_are_rejected(self):
        with pytest.raises(ValidationError, match="duplicate vault instance"):
            VaultConfig(enabled=True, instances=[_inst("a"), _inst("a")])

    def test_more_than_one_default_is_rejected(self):
        with pytest.raises(ValidationError, match="at most one vault instance"):
            VaultConfig(
                enabled=True,
                instances=[_inst("a", default=True), _inst("b", default=True)],
            )

    def test_missing_address_is_rejected(self):
        with pytest.raises(ValidationError, match="requires an address"):
            VaultConfig(enabled=True, instances=[VaultInstanceConfig(name="a", address="")])

    def test_a_valid_multi_instance_config_is_accepted(self):
        cfg = VaultConfig(
            enabled=True,
            instances=[_inst("prod", default=True), _inst("dev")],
        )
        assert cfg.resolve_instance(None).name == "prod"  # the default
        assert cfg.resolve_instance("dev").name == "dev"

    def test_blank_instance_name_is_rejected(self):
        with pytest.raises(ValidationError, match="name is required"):
            VaultInstanceConfig(name="", address="https://v:8200")

    def test_revoke_leases_is_off_by_default(self):
        assert _inst().revoke_leases is False
        assert VaultConfig(enabled=True, instances=[_inst()]).revocation_enabled is False

    def test_revocation_is_enabled_by_any_one_instance(self):
        cfg = VaultConfig(enabled=True, instances=[_inst("a"), _inst("b", revoke_leases=True)])
        assert cfg.revocation_enabled is True
        assert cfg.instance_named("b").revoke_leases is True
        assert cfg.instance_named("missing") is None

    def test_revocation_is_off_while_vault_is_disabled(self):
        cfg = VaultConfig(enabled=False, instances=[_inst(revoke_leases=True)])
        assert cfg.revocation_enabled is False

    def test_invalid_auth_method_is_rejected(self):
        with pytest.raises(ValidationError, match="auth method must be"):
            VaultInstanceConfig(name="a", address="https://v:8200", auth={"method": "nonsense"})


class TestJwtAndTokenPathDefaults:
    """#1650: `jwt` auth and the token file each login reads."""

    def test_jwt_is_accepted_and_defaults_its_audience_and_mount(self):
        inst = _inst("hcp", auth={"method": "jwt", "role": "terrapod"})
        assert inst.auth.method == "jwt"
        assert inst.auth.audience == "vault"
        # The model's mount default is `kubernetes`, which a jwt instance never
        # means; an unset mount follows the method instead.
        assert inst.auth.mount == "jwt"
        assert inst.auth.token_path == "/var/run/secrets/terrapod/vault/hcp/token"

    def test_jwt_keeps_an_explicit_mount_and_audience(self):
        inst = _inst(
            auth={"method": "jwt", "mount": "k8s-prod", "audience": "https://vault.example.com"}
        )
        assert inst.auth.mount == "k8s-prod"
        assert inst.auth.audience == "https://vault.example.com"

    def test_an_explicit_token_path_wins(self):
        inst = _inst(auth={"method": "jwt", "token_path": "/mnt/tok"})
        assert inst.auth.token_path == "/mnt/tok"

    def test_kubernetes_without_an_audience_reads_the_standard_sa_token(self):
        inst = _inst(auth={"method": "kubernetes"})
        assert inst.auth.audience == ""
        assert inst.auth.token_path == "/var/run/secrets/kubernetes.io/serviceaccount/token"

    def test_kubernetes_with_an_audience_reads_the_projected_token(self):
        inst = _inst("k", auth={"method": "kubernetes", "audience": "vault"})
        assert inst.auth.mount == "kubernetes"
        assert inst.auth.token_path == "/var/run/secrets/terrapod/vault/k/token"

    @pytest.mark.parametrize("method", ["approle", "token"])
    def test_credential_methods_read_no_token_file(self, method):
        assert _inst(auth={"method": method}).auth.token_path == ""

    @pytest.mark.parametrize("method", ["oidc", "JWT", "userpass", ""])
    def test_an_unknown_method_is_still_rejected(self, method):
        with pytest.raises(ValidationError, match="auth method must be"):
            _inst(auth={"method": method})


class TestCustomCa:
    def test_a_ca_file_is_accepted(self):
        assert _inst(ca_file="/etc/terrapod/vault-ca/x/ca.crt").ca_file.endswith("ca.crt")

    def test_a_ca_file_and_skip_verify_together_are_rejected(self):
        with pytest.raises(ValidationError, match="both ca_file and tls_skip_verify"):
            _inst(ca_file="/ca.crt", tls_skip_verify=True)

    def test_skip_verify_alone_still_works(self):
        assert _inst(tls_skip_verify=True).tls_skip_verify is True
