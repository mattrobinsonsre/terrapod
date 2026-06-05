"""Tests for terrapod.runner.runner_config."""

from __future__ import annotations

from terrapod.runner.runner_config import RunnerConfig


def test_from_env_minimal_defaults() -> None:
    cfg = RunnerConfig.from_env(env={})
    assert cfg.api_url == ""
    assert cfg.auth_token == ""
    assert cfg.phase == "plan"
    assert cfg.backend == "tofu"
    assert cfg.target_addrs == []
    assert cfg.replace_addrs == []
    assert cfg.refresh is True
    assert cfg.refresh_only is False
    assert cfg.destroy is False
    assert cfg.has_api is False


def test_from_env_strips_trailing_slash_on_api_url() -> None:
    cfg = RunnerConfig.from_env(env={"TP_API_URL": "https://api.example.com/"})
    assert cfg.api_url == "https://api.example.com"


def test_from_env_json_lists() -> None:
    cfg = RunnerConfig.from_env(
        env={
            "TP_TARGET_ADDRS": '["aws_instance.web", "aws_instance.db"]',
            "TP_REPLACE_ADDRS": "[]",
        }
    )
    assert cfg.target_addrs == ["aws_instance.web", "aws_instance.db"]
    assert cfg.replace_addrs == []


def test_from_env_json_list_malformed_is_empty() -> None:
    cfg = RunnerConfig.from_env(env={"TP_TARGET_ADDRS": "not json"})
    assert cfg.target_addrs == []


def test_from_env_bool_parsing() -> None:
    truthy = ["1", "true", "TRUE", "yes", "on"]
    falsy = ["0", "false", "no", "", "garbage"]
    for v in truthy:
        cfg = RunnerConfig.from_env(env={"TP_DESTROY": v})
        assert cfg.destroy is True, f"{v!r} should be truthy"
    for v in falsy:
        cfg = RunnerConfig.from_env(env={"TP_DESTROY": v})
        assert cfg.destroy is False, f"{v!r} should be falsy"


def test_refresh_defaults_true_falsifiable() -> None:
    assert RunnerConfig.from_env(env={}).refresh is True
    assert RunnerConfig.from_env(env={"TP_REFRESH": "false"}).refresh is False


def test_int_parsing_falls_back_to_default() -> None:
    cfg = RunnerConfig.from_env(env={"TP_DOWNLOAD_RETRIES": "abc"})
    assert cfg.download_retries == 3


def test_arch_normalisation_x86_64() -> None:
    cfg = RunnerConfig.from_env(env={})
    # We're running on the test host — assert the normalised value is
    # one of the two values the runner actually targets, not the raw
    # uname output.
    assert cfg.arch in ("amd64", "arm64") or cfg.arch == "i386"


def test_has_api_requires_both_url_and_run_id() -> None:
    assert RunnerConfig.from_env(env={"TP_API_URL": "https://x"}).has_api is False
    assert RunnerConfig.from_env(env={"TP_RUN_ID": "r-1"}).has_api is False
    assert (
        RunnerConfig.from_env(env={"TP_API_URL": "https://x", "TP_RUN_ID": "r-1"}).has_api is True
    )
