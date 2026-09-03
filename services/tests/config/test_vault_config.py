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

    def test_invalid_auth_method_is_rejected(self):
        with pytest.raises(ValidationError, match="auth method must be"):
            VaultInstanceConfig(name="a", address="https://v:8200", auth={"method": "nonsense"})
