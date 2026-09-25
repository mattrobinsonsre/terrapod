"""Data files and shared evaluation for VCS policy sets (#1842).

Policies could not share anything. Each was evaluated on its own
(`opa eval --data <one policy> --data <context>`), and the VCS sync kept only
`.rego` files that both declared `package terrapod` and defined a `deny` rule.
So a `data.yaml` beside the policies was ignored, a data-only `.rego` was
dropped at sync time, and a helper in one file was invisible to the next —
leaving every policy to inline its own copy of the same allowlist, and the
copies to drift.

Two things here are worth stating because they are easy to "fix" wrongly:

**Shared evaluation reports per SET, not per policy.** Every file shares
`package terrapod`, so evaluated together the `deny` set is their union and OPA
does not say which file produced which message. A per-policy breakdown would
be invented. That is also why the flag is opt-in rather than the default: a set
that wants per-policy results keeps them by leaving it off.

**Support files are synced whatever the flag says.** Gating the sync on it
instead would mean an operator who turns it on sees nothing change until the
next VCS poll, which reads as the feature being broken.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from terrapod.services import policy_vcs_poller as poller


def _tar(files: dict[str, str]) -> bytes:
    """A repo tarball, with the single top-level prefix git archives carry."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"repo-main/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


POLICY = """package terrapod
import rego.v1

deny contains msg if {
    msg := "nope"
}
"""

WARN_ONLY = """package terrapod
import rego.v1

warn contains msg if {
    msg := "hmm"
}
"""

HELPER = """package terrapod

is_approved(x) if {
    x == "ok"
}
"""


class TestClassification:
    def test_a_deny_policy_is_a_policy(self):
        policies, support = poller._classify({"net.rego": POLICY})
        assert "net" in policies, "the key drops the extension, as policy names always have"
        assert support == {}

    def test_a_warn_only_file_is_a_policy_too(self):
        """A set can be advisory-only. The old filter required `deny`, so a
        file that produces only warnings was dropped at sync — the policy
        existed in git, was accepted by the UI, and never ran."""
        policies, support = poller._classify({"style.rego": WARN_ONLY})
        assert "style" in policies
        assert support == {}

    def test_a_deny_less_rego_becomes_a_support_file(self):
        """This is the file that could not exist before: a shared helper. It
        was dropped at sync, so every policy had to inline its own copy."""
        policies, support = poller._classify({"helpers.rego": HELPER})
        assert policies == {}
        assert "helpers.rego" in support, "support keys keep the extension — OPA loads by suffix"

    @pytest.mark.parametrize("name", ["data.yaml", "data.yml", "data.json"])
    def test_data_files_are_support_files(self, name):
        policies, support = poller._classify({name: "approved: []"})
        assert policies == {}
        assert name in support

    def test_a_rego_outside_the_terrapod_package_is_not_a_policy(self):
        """The package check is what stops an unrelated rego file in the same
        directory being treated as a Terrapod policy."""
        policies, support = poller._classify({"other.rego": "package other\n\ndeny := true\n"})
        assert policies == {}
        assert "other.rego" in support

    def test_a_mixed_directory_splits_the_way_the_issue_describes(self):
        policies, support = poller._classify(
            {
                "data.yaml": "approved_cidrs: []",
                "helpers.rego": HELPER,
                "network.rego": POLICY,
                "modules.rego": POLICY,
            }
        )
        assert sorted(policies) == ["modules", "network"]
        assert sorted(support) == ["data.yaml", "helpers.rego"]


class TestExtraction:
    def test_it_takes_data_files_the_old_extractor_ignored(self):
        files, _skipped = poller._extract_policy_files(
            _tar({"policies/net.rego": POLICY, "policies/data.yaml": "a: 1"}), "policies"
        )
        assert sorted(files) == ["data.yaml", "net.rego"]

    def test_unrelated_files_are_left_alone(self):
        """A README or a CI config beside the policies costs nothing."""
        files, _skipped = poller._extract_policy_files(
            _tar({"policies/net.rego": POLICY, "policies/README.md": "hi"}), "policies"
        )
        assert sorted(files) == ["net.rego"]

    def test_test_fixtures_are_not_carried_into_the_evaluation(self):
        """`*_test.rego` defines no `deny`, so it would land as a support file
        and be loaded into a shared evaluation — putting its fixtures into the
        data the real policies see."""
        files, _skipped = poller._extract_policy_files(
            _tar({"policies/net.rego": POLICY, "policies/net_test.rego": "package terrapod"}),
            "policies",
        )
        assert sorted(files) == ["net.rego"]

    def test_subdirectories_are_still_not_descended_into(self):
        files, _skipped = poller._extract_policy_files(
            _tar({"policies/net.rego": POLICY, "policies/nested/deep.rego": POLICY}), "policies"
        )
        assert sorted(files) == ["net.rego"]

    def test_an_oversized_file_is_skipped_not_carried(self):
        """Every applicable run fetches the bundle, so one pathological file
        would be paid for on every run of every matching workspace."""
        big = "x" * (poller._MAX_POLICY_FILE_BYTES + 1)
        files, _skipped = poller._extract_policy_files(
            _tar({"policies/net.rego": POLICY, "policies/huge.json": big}), "policies"
        )
        assert sorted(files) == ["net.rego"]

    def test_a_non_utf8_file_does_not_break_the_sync(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, raw in [
                ("repo-main/policies/net.rego", POLICY.encode()),
                ("repo-main/policies/blob.json", b"\xff\xfe\x00binary"),
            ]:
                info = tarfile.TarInfo(name=name)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
        files, _skipped = poller._extract_policy_files(buf.getvalue(), "policies")
        assert sorted(files) == ["net.rego"], "one bad file must not lose the good ones"
