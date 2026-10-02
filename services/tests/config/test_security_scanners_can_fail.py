"""A security scanner that cannot report anything is worse than none at all.

Two findings in this review were of that shape rather than of a missing control:
the secret scan ran on every push with an empty ruleset, and the DAST templates
asserted the *healthy* response, so they reported a finding on every correctly
protected endpoint and stayed silent on a bypassable one. Both were green. Both
had been green for as long as they had existed.

Those are fixed. These are the guards, because the fix in each case is a single
line in a config file that reads as housekeeping and is trivially undone — and
undoing it restores a check that passes while measuring nothing, which is exactly
the state nobody noticed the first time.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve()

#: Responses that mean the control WORKED. A DAST template matching one of these
#: has its polarity inverted: it fires on a protected endpoint and is quiet on an
#: open one. 404 counts — an endpoint that denies before it resolves is behaving.
HEALTHY = {401, 403, 404, 405}


def _root() -> pathlib.Path | None:
    """The checkout root, or None in an image that ships no `pentest/`."""
    for cand in HERE.parents:
        if (cand / "pentest").is_dir() and (cand / "services").is_dir():
            return cand
    return None


ROOT = _root()


class TestTheSecretScanHasRules:
    """`gitleaks` treats a supplied `--config` as the COMPLETE ruleset.

    So a config declaring only an allowlist defines no rules, matches nothing, and
    the required check cannot fail. Dropping `--config` does not rescue it either:
    gitleaks auto-discovers `.gitleaks.toml`.
    """

    def test_the_config_declares_a_ruleset(self):
        if ROOT is None:
            pytest.skip("pentest/ is not shipped in this image")
        cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text())

        extends_defaults = bool(cfg.get("extend", {}).get("useDefault"))
        own_rules = cfg.get("rules") or []
        assert extends_defaults or own_rules, (
            "the gitleaks config defines no rules, so the secret scan matches "
            "nothing and the required check cannot fail — set "
            "`[extend] useDefault = true` or declare `[[rules]]`"
        )

    def test_the_allowlist_does_not_cover_the_whole_tree(self):
        """An allowlist wide enough to match everything is the same no-op wearing
        a different hat."""
        if ROOT is None:
            pytest.skip("pentest/ is not shipped in this image")
        cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
        paths = cfg.get("allowlist", {}).get("paths") or []
        catch_all = [p for p in paths if p.strip() in (".", ".*", ".*?", "^.*$", "/")]
        assert not catch_all, f"an allowlist entry matches the entire repository: {catch_all}"


class TestTheDastTemplatesDetectTheVulnerability:
    """A matcher names the condition that IS the finding, not the one that is fine.

    Reported as inverted polarity across six templates: they matched 401/403, so a
    passing scan meant "every endpoint I probed is protected" only by coincidence —
    the same pass is what an entirely unauthenticated deployment produces.
    """

    def _templates(self) -> list[pathlib.Path]:
        d = ROOT / "pentest" / "nuclei" / "terrapod-templates"
        return sorted(d.glob("*.yaml"))

    def test_there_are_templates_to_check(self):
        """Otherwise the test below passes by iterating nothing."""
        if ROOT is None:
            pytest.skip("pentest/ is not shipped in this image")
        assert len(self._templates()) >= 5, (
            "the DAST template directory has moved or emptied, so the polarity "
            "check below is reading nothing"
        )

    def test_no_template_reports_a_protected_response_as_a_finding(self):
        if ROOT is None:
            pytest.skip("pentest/ is not shipped in this image")

        offenders: list[str] = []
        for f in self._templates():
            doc = yaml.safe_load(f.read_text()) or {}
            for req in doc.get("http", []) or []:
                for matcher in req.get("matchers", []) or []:
                    if matcher.get("type") != "status":
                        continue
                    bad = HEALTHY & set(matcher.get("status", []) or [])
                    if bad:
                        offenders.append(f"{f.name}: matches {sorted(bad)}")

        assert not offenders, (
            "these templates treat a response that means the control WORKED as "
            "the finding, so they fire on a protected endpoint and stay silent on "
            "a bypassable one — a scan that passes tells you nothing:\n  "
            + "\n  ".join(offenders)
            + "\n\nMatch the 2xx that proves the request got through instead."
        )
