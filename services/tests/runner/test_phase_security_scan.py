"""Tests for terrapod.runner.phases.security_scan (#1036)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx

from terrapod.runner.phases import security_scan as scan
from terrapod.runner.runner_config import RunnerConfig


def _cfg(**overrides) -> RunnerConfig:
    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
    }
    base.update(overrides)
    return RunnerConfig.from_env(env=base)


# ── severity helpers ──────────────────────────────────────────────────────


class TestSeverity:
    def test_unrated_defaults_to_high(self) -> None:
        assert scan._norm_severity(None) == "high"
        assert scan._norm_severity("") == "high"
        assert scan._norm_severity("nonsense") == "high"

    def test_known_normalised(self) -> None:
        assert scan._norm_severity("CRITICAL") == "critical"
        assert scan._norm_severity(" Low ") == "low"

    def test_rank_order(self) -> None:
        assert scan._severity_rank("critical") > scan._severity_rank("high")
        assert scan._severity_rank("high") > scan._severity_rank("medium")
        assert scan._severity_rank("medium") > scan._severity_rank("low")
        # unrated ranks as high
        assert scan._severity_rank(None) == scan._severity_rank("high")


# ── checkov normalisation ──────────────────────────────────────────────────


class TestNormalizeCheckov:
    def test_single_framework_object(self) -> None:
        out = json.dumps(
            {
                "check_type": "terraform_plan",
                "results": {
                    "failed_checks": [
                        {
                            "check_id": "CKV_AWS_18",
                            "check_name": "Ensure S3 has access logging",
                            "severity": None,
                            "resource": "aws_s3_bucket.b",
                            "file_path": "/plan.json",
                            "file_line_range": [10, 20],
                            "guideline": "https://x",
                        }
                    ]
                },
            }
        )
        findings = scan._normalize_checkov(out)
        assert len(findings) == 1
        f = findings[0]
        assert f["engine"] == "checkov"
        assert f["rule_id"] == "CKV_AWS_18"
        assert f["severity"] == "high"  # None → high
        assert f["resource"] == "aws_s3_bucket.b"
        assert f["line"] == 10

    def test_list_of_frameworks(self) -> None:
        out = json.dumps(
            [
                {"results": {"failed_checks": [{"check_id": "CKV_1", "severity": "HIGH"}]}},
                {"results": {"failed_checks": [{"check_id": "CKV_2", "severity": "LOW"}]}},
            ]
        )
        findings = scan._normalize_checkov(out)
        assert {f["rule_id"] for f in findings} == {"CKV_1", "CKV_2"}
        assert {f["severity"] for f in findings} == {"high", "low"}

    def test_empty_results(self) -> None:
        out = json.dumps({"results": {"failed_checks": []}})
        assert scan._normalize_checkov(out) == []


# ── trivy normalisation ────────────────────────────────────────────────────


class TestNormalizeTrivy:
    def test_misconfigurations_mapped(self) -> None:
        out = json.dumps(
            {
                "Results": [
                    {
                        "Target": "plan.json",
                        "Misconfigurations": [
                            {
                                "ID": "AVD-AWS-0107",
                                "Title": "No public ingress",
                                "Severity": "CRITICAL",
                                "CauseMetadata": {"StartLine": 5, "Resource": "aws_sg.x"},
                                "PrimaryURL": "https://avd",
                            }
                        ],
                    }
                ]
            }
        )
        findings = scan._normalize_trivy(out, [])
        assert len(findings) == 1
        assert findings[0]["engine"] == "trivy"
        assert findings[0]["rule_id"] == "AVD-AWS-0107"
        assert findings[0]["severity"] == "critical"
        assert findings[0]["line"] == 5

    def test_skip_rules_filter(self) -> None:
        out = json.dumps(
            {
                "Results": [
                    {
                        "Misconfigurations": [
                            {"ID": "AVD-1", "Severity": "HIGH"},
                            {"ID": "AVD-2", "Severity": "HIGH"},
                        ]
                    }
                ]
            }
        )
        findings = scan._normalize_trivy(out, ["AVD-1"])
        assert [f["rule_id"] for f in findings] == ["AVD-2"]


# ── outcome computation ────────────────────────────────────────────────────


class TestComputeOutcome:
    def test_errored_wins(self) -> None:
        outcome, summary = scan.compute_outcome([], "high", errored=True)
        assert outcome == "errored"

    def test_no_findings_passes(self) -> None:
        outcome, summary = scan.compute_outcome([], "high", errored=False)
        assert outcome == "passed"
        assert summary["total"] == 0
        assert summary["blocking"] == 0

    def test_below_threshold_passes(self) -> None:
        findings = [{"severity": "low"}, {"severity": "medium"}]
        outcome, summary = scan.compute_outcome(findings, "high", errored=False)
        assert outcome == "passed"
        assert summary["total"] == 2
        assert summary["blocking"] == 0

    def test_at_or_above_threshold_fails(self) -> None:
        findings = [{"severity": "low"}, {"severity": "high"}, {"severity": "critical"}]
        outcome, summary = scan.compute_outcome(findings, "high", errored=False)
        assert outcome == "failed"
        assert summary["blocking"] == 2  # high + critical
        assert summary["by_severity"]["critical"] == 1

    def test_threshold_critical_narrows(self) -> None:
        findings = [{"severity": "high"}, {"severity": "critical"}]
        outcome, summary = scan.compute_outcome(findings, "critical", errored=False)
        assert outcome == "failed"
        assert summary["blocking"] == 1  # only critical


# ── config fetch ───────────────────────────────────────────────────────────


class TestFetchScanConfig:
    def test_enabled_returned(self) -> None:
        cfg = _cfg()
        payload = {"enabled": True, "enforcement_level": "enforced", "engine": "checkov"}

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path.endswith("/security-scan-config")
            return httpx.Response(200, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert scan.fetch_scan_config(cfg, client=client) == payload

    def test_disabled_returns_none(self) -> None:
        cfg = _cfg()

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"enabled": False, "enforcement_level": "off"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert scan.fetch_scan_config(cfg, client=client) is None

    def test_non_200_retries_then_none(self) -> None:
        cfg = _cfg()
        sleeps: list = []

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert scan.fetch_scan_config(cfg, client=client, sleep=lambda s: sleeps.append(s)) is None
        assert len(sleeps) == 2  # 3 attempts, 2 sleeps

    def test_no_api_returns_none(self) -> None:
        cfg = _cfg(TP_API_URL="")
        assert scan.fetch_scan_config(cfg) is None


# ── results POST ───────────────────────────────────────────────────────────


class TestPostScanResult:
    def test_201_returns_true(self) -> None:
        cfg = _cfg()
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content)
            return httpx.Response(201, json={"recorded": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok = scan.post_scan_result(
            cfg,
            engine="checkov",
            outcome="failed",
            findings=[{"rule_id": "CKV_1"}],
            summary={"total": 1},
            error=None,
            client=client,
        )
        assert ok is True
        assert seen["body"]["engine"] == "checkov"
        assert seen["body"]["outcome"] == "failed"

    def test_4xx_is_final_no_retry(self) -> None:
        cfg = _cfg()
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(422, json={"detail": "bad"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok = scan.post_scan_result(
            cfg,
            engine="checkov",
            outcome="passed",
            findings=[],
            summary={},
            error=None,
            client=client,
            sleep=lambda s: None,
        )
        assert ok is False
        assert calls["n"] == 1  # 4xx not retried

    def test_5xx_retries_then_false(self) -> None:
        cfg = _cfg()
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        ok = scan.post_scan_result(
            cfg,
            engine="checkov",
            outcome="passed",
            findings=[],
            summary={},
            error=None,
            client=client,
            sleep=lambda s: None,
        )
        assert ok is False
        assert calls["n"] == 3


# ── engine subprocess wrappers ─────────────────────────────────────────────


class TestRunCheckov:
    def test_findings_parsed(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text("{}")
        out = json.dumps(
            {"results": {"failed_checks": [{"check_id": "CKV_1", "severity": "HIGH"}]}}
        )
        with patch.object(
            scan.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, out, "")
        ):
            ok, findings, err = scan._run_checkov(plan, [])
        assert ok is True
        assert findings[0]["rule_id"] == "CKV_1"
        assert err is None

    def test_skip_rules_passed_as_flags(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text("{}")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        with patch.object(scan.subprocess, "run", side_effect=fake_run):
            scan._run_checkov(plan, ["CKV_AWS_18", "CKV_AWS_21"])
        assert "--skip-check" in seen["cmd"]
        assert "CKV_AWS_18" in seen["cmd"] and "CKV_AWS_21" in seen["cmd"]

    def test_binary_missing_errors(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text("{}")
        with patch.object(scan.subprocess, "run", side_effect=FileNotFoundError("no checkov")):
            ok, findings, err = scan._run_checkov(plan, [])
        assert ok is False
        assert "not found" in err

    def test_timeout_errors(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text("{}")
        with patch.object(
            scan.subprocess, "run", side_effect=subprocess.TimeoutExpired("checkov", 300)
        ):
            ok, findings, err = scan._run_checkov(plan, [])
        assert ok is False
        assert "timed out" in err

    def test_clean_empty_stdout_rc0_ok(self, tmp_path: Path) -> None:
        plan = tmp_path / "plan.json"
        plan.write_text("{}")
        with patch.object(
            scan.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            ok, findings, err = scan._run_checkov(plan, [])
        assert ok is True
        assert findings == []


# ── orchestrator ───────────────────────────────────────────────────────────


class TestRunAndPost:
    def test_missing_plan_json_posts_errored(self, tmp_path: Path) -> None:
        cfg = _cfg()
        missing = tmp_path / "nope.json"
        posted = {}
        with patch.object(
            scan, "post_scan_result", side_effect=lambda cfg, **kw: posted.update(kw)
        ):
            outcome = scan.run_and_post(cfg, {"engine": "checkov"}, plan_json=missing)
        assert outcome == "errored"
        assert posted["outcome"] == "errored"
        assert "not available" in posted["error"]

    def test_checkov_happy_path_posts(self, tmp_path: Path) -> None:
        cfg = _cfg()
        plan = tmp_path / "plan.json"
        plan.write_text('{"x":1}')
        posted = {}
        with (
            patch.object(
                scan,
                "_run_checkov",
                return_value=(True, [{"severity": "critical", "rule_id": "CKV_1"}], None),
            ),
            patch.object(scan, "post_scan_result", side_effect=lambda cfg, **kw: posted.update(kw)),
        ):
            outcome = scan.run_and_post(
                cfg, {"engine": "checkov", "severity_threshold": "high"}, plan_json=plan
            )
        assert outcome == "failed"
        assert posted["engine"] == "checkov"
        assert posted["summary"]["blocking"] == 1

    def test_both_engines_merge(self, tmp_path: Path) -> None:
        cfg = _cfg()
        plan = tmp_path / "plan.json"
        plan.write_text('{"x":1}')
        posted = {}
        with (
            patch.object(
                scan,
                "_run_checkov",
                return_value=(True, [{"severity": "low", "rule_id": "CKV_1"}], None),
            ),
            patch.object(
                scan,
                "_run_trivy",
                return_value=(True, [{"severity": "low", "rule_id": "AVD-1"}], None),
            ),
            patch.object(scan, "post_scan_result", side_effect=lambda cfg, **kw: posted.update(kw)),
        ):
            outcome = scan.run_and_post(
                cfg, {"engine": "both", "severity_threshold": "high"}, plan_json=plan
            )
        assert outcome == "passed"  # both low, below high
        assert posted["summary"]["total"] == 2

    def test_engine_error_marks_errored(self, tmp_path: Path) -> None:
        cfg = _cfg()
        plan = tmp_path / "plan.json"
        plan.write_text('{"x":1}')
        posted = {}
        with (
            patch.object(scan, "_run_checkov", return_value=(False, [], "checkov exploded")),
            patch.object(scan, "post_scan_result", side_effect=lambda cfg, **kw: posted.update(kw)),
        ):
            outcome = scan.run_and_post(cfg, {"engine": "checkov"}, plan_json=plan)
        assert outcome == "errored"
        assert "checkov exploded" in posted["error"]
