"""#592 — listener-side RunnerConfig parses proxy + CA-bundle from runners.yaml.

These fields are rendered into runners.yaml by the chart and forwarded by the
listener into runner Jobs (proxy env + a per-run CA Secret). This asserts the
YAML→model coercion the listener relies on.
"""

from __future__ import annotations

from terrapod.config import RunnerConfig, RunnerProxyConfig


def test_defaults_proxy_none_ca_off() -> None:
    cfg = RunnerConfig()
    assert cfg.proxy is None
    assert cfg.ca_bundle_enabled is False
    assert cfg.ca_bundle_source_path == ""


def test_proxy_dict_coerced_to_model() -> None:
    cfg = RunnerConfig(
        proxy={
            "http_proxy": "http://proxy:3128",
            "https_proxy": "http://proxy:3128",
            "no_proxy": "localhost,terrapod-api,.svc",
        }
    )
    assert isinstance(cfg.proxy, RunnerProxyConfig)
    assert cfg.proxy.http_proxy == "http://proxy:3128"
    assert cfg.proxy.no_proxy == "localhost,terrapod-api,.svc"


def test_ca_bundle_fields() -> None:
    cfg = RunnerConfig(
        ca_bundle_enabled=True,
        ca_bundle_source_path="/etc/terrapod-ca-src/ca-extra.pem",
    )
    assert cfg.ca_bundle_enabled is True
    assert cfg.ca_bundle_source_path == "/etc/terrapod-ca-src/ca-extra.pem"


def test_partial_proxy_model_defaults_blank() -> None:
    # Only https set — the others default to "" (the listener/job_template
    # skip empty values).
    cfg = RunnerConfig(proxy={"https_proxy": "http://proxy:3128"})
    assert cfg.proxy is not None
    assert cfg.proxy.https_proxy == "http://proxy:3128"
    assert cfg.proxy.http_proxy == ""
    assert cfg.proxy.no_proxy == ""
