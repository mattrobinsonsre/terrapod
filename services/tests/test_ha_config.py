"""HAConfig validation (#960 phase 1, #1101).

The default matters more than the validation: almost every install is a single
node, and an `auto` default would take every one of them inert on upgrade.
"""

import pytest
from pydantic import ValidationError

from terrapod.config import HAConfig


class TestDefault:
    def test_defaults_to_leader(self):
        """The 99% case: never probes, never transitions, never goes passive."""
        assert HAConfig().role == "leader"

    def test_probe_timing_is_minutes_scale(self):
        cfg = HAConfig()
        assert cfg.probe_interval_seconds == 60
        assert cfg.probe_threshold == 3


class TestValidation:
    def test_rejects_unknown_role(self):
        with pytest.raises(ValidationError):
            HAConfig(role="primary")

    def test_auto_requires_a_probe_url(self):
        """`auto` with nowhere to probe is incoherent, not a state to sit in."""
        with pytest.raises(ValidationError, match="probe_url"):
            HAConfig(role="auto", node_name="a")

    def test_auto_requires_a_node_name(self):
        """Without a name of its own a node cannot recognise itself in the answer."""
        with pytest.raises(ValidationError, match="node_name"):
            HAConfig(role="auto", probe_url={"internal": "https://x"})

    def test_auto_accepts_either_probe_url(self):
        assert HAConfig(role="auto", node_name="a", probe_url={"internal": "https://i"}).role
        assert HAConfig(role="auto", node_name="a", probe_url={"external": "https://e"}).role

    def test_probe_interval_has_a_floor(self):
        with pytest.raises(ValidationError):
            HAConfig(probe_interval_seconds=1)

    def test_static_roles_need_nothing_else(self):
        assert HAConfig(role="follower").role == "follower"


class TestEffectiveProbeUrl:
    def test_internal_is_preferred(self):
        """Internal avoids hairpin NAT and any CDN/WAF in front of the external name."""
        cfg = HAConfig(
            role="auto", node_name="a", probe_url={"internal": "https://i", "external": "https://e"}
        )
        assert cfg.effective_probe_url == "https://i"

    def test_falls_back_to_external(self):
        cfg = HAConfig(role="auto", node_name="a", probe_url={"external": "https://e"})
        assert cfg.effective_probe_url == "https://e"
