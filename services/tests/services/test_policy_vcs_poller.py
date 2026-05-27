"""Unit tests for the policy VCS poller's .rego extraction logic."""

import io
import tarfile

from terrapod.services.policy_vcs_poller import _extract_rego_files


def _make_tarball(files: dict[str, str]) -> bytes:
    """Create an in-memory gzipped tarball with the given path->content mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestExtractRegoFiles:
    def test_extracts_from_policy_path(self):
        archive = _make_tarball({
            "repo-abc123/policies/deny_s3.rego": "package terrapod\ndeny contains msg if { false\n  msg := \"no\" }",
            "repo-abc123/policies/warn_tags.rego": "package terrapod\nwarn contains msg if { false }",
            "repo-abc123/policies/sub/nested.rego": "package terrapod\n# nested should be excluded",
            "repo-abc123/README.md": "# Policies",
        })
        result = _extract_rego_files(archive, "policies")
        assert "deny_s3" in result
        assert "warn_tags" in result
        assert "nested" not in result
        assert "README" not in result

    def test_extracts_from_root_when_path_empty(self):
        archive = _make_tarball({
            "repo-abc123/main.rego": "package terrapod\ndeny contains msg if { false }",
            "repo-abc123/sub/other.rego": "package terrapod\n# sub should be excluded",
        })
        result = _extract_rego_files(archive, "")
        assert "main" in result
        assert "other" not in result

    def test_strips_rego_extension_for_name(self):
        archive = _make_tarball({
            "repo-abc123/my-policy.rego": "package terrapod\ndeny contains msg if { false }",
        })
        result = _extract_rego_files(archive, "")
        assert "my-policy" in result
        assert "my-policy.rego" not in result

    def test_ignores_non_rego_files(self):
        archive = _make_tarball({
            "repo-abc123/policies/valid.rego": "package terrapod\ndeny contains msg if { false }",
            "repo-abc123/policies/README.md": "# docs",
            "repo-abc123/policies/data.json": "{}",
        })
        result = _extract_rego_files(archive, "policies")
        assert "valid" in result
        assert len(result) == 1

    def test_handles_trailing_slash_in_path(self):
        archive = _make_tarball({
            "repo-abc123/policies/test.rego": "package terrapod\ndeny contains msg if { false }",
        })
        result = _extract_rego_files(archive, "policies/")
        assert "test" in result

    def test_empty_archive_returns_empty(self):
        archive = _make_tarball({})
        result = _extract_rego_files(archive, "policies")
        assert result == {}

    def test_no_matching_path_returns_empty(self):
        archive = _make_tarball({
            "repo-abc123/other-dir/test.rego": "package terrapod\ndeny contains msg if { false }",
        })
        result = _extract_rego_files(archive, "policies")
        assert result == {}
