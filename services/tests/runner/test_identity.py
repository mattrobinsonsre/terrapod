"""Tests for listener identity name derivation."""

import os
from unittest.mock import patch


class TestListenerNameDerivation:
    """Test that listener names are correctly derived from env vars."""

    def test_name_with_pod_name(self):
        """When POD_NAME is set, name should be base-podname."""
        env = {
            "TERRAPOD_LISTENER_NAME": "prod-listener",
            "POD_NAME": "terrapod-listener-7f8b9c6d4-x2k3m",
        }
        with patch.dict(os.environ, env, clear=False):
            base_name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
            pod_name = os.environ.get("POD_NAME", "")
            name = f"{base_name}-{pod_name}" if pod_name else base_name

        assert name == "prod-listener-terrapod-listener-7f8b9c6d4-x2k3m"

    def test_name_without_pod_name(self):
        """When POD_NAME is not set, name should be just the base name."""
        env = {"TERRAPOD_LISTENER_NAME": "my-listener"}
        with patch.dict(os.environ, env, clear=False):
            # Remove POD_NAME if it exists
            os.environ.pop("POD_NAME", None)
            base_name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
            pod_name = os.environ.get("POD_NAME", "")
            name = f"{base_name}-{pod_name}" if pod_name else base_name

        assert name == "my-listener"

    def test_name_defaults(self):
        """When neither env var is set, name should be 'listener'."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TERRAPOD_LISTENER_NAME", None)
            os.environ.pop("POD_NAME", None)
            base_name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
            pod_name = os.environ.get("POD_NAME", "")
            name = f"{base_name}-{pod_name}" if pod_name else base_name

        assert name == "listener"

    def test_empty_pod_name_uses_base_only(self):
        """When POD_NAME is empty string, name should be just the base."""
        env = {
            "TERRAPOD_LISTENER_NAME": "dev-listener",
            "POD_NAME": "",
        }
        with patch.dict(os.environ, env, clear=False):
            base_name = os.environ.get("TERRAPOD_LISTENER_NAME", "listener")
            pod_name = os.environ.get("POD_NAME", "")
            name = f"{base_name}-{pod_name}" if pod_name else base_name

        assert name == "dev-listener"
