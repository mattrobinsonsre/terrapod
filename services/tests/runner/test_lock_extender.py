"""Tests for terrapod.runner.lock_extender.

Covers:
  * HCL lock-file parsing — find provider blocks, extract source +
    version regardless of how many hash lines, blank lines etc.
  * Mirror response handling — pick the h1: hash for the requested
    platform, skip platforms not in the response, skip uncached
    providers (no h1: in their hashes).
  * Hash splicing — idempotent, preserves existing entries, keeps
    indentation.
  * End-to-end extend_lock_file against a fake mirror.
"""

from __future__ import annotations

import httpx

from terrapod.runner import lock_extender

_SAMPLE_LOCK = """\
# This file is maintained automatically by "tofu init".
# Manual edits may be lost in future updates.

provider "registry.opentofu.org/hashicorp/random" {
  version     = "3.6.0"
  constraints = "3.6.0"
  hashes = [
    "h1:/xwPFz7kMERBIEk8i6UJt2fTvgzMFbwKlcyCvRJO8Ok=",
    "zh:486a1c921eab5c51a480f2eb0ad85173f207c9b7bb215f3893e58bc38d3b7c75",
  ]
}

provider "registry.opentofu.org/hashicorp/null" {
  version = "3.2.0"
  hashes = [
    "h1:NULLFAKEH1FAKE=",
    "zh:00000000aaaaaaaa",
  ]
}
"""


class TestParseLockFile:
    def test_finds_every_provider_block(self) -> None:
        blocks = lock_extender.parse_lock_file(_SAMPLE_LOCK)
        sources = sorted(b.source for b in blocks)
        assert sources == [
            "registry.opentofu.org/hashicorp/null",
            "registry.opentofu.org/hashicorp/random",
        ]

    def test_extracts_version(self) -> None:
        blocks = lock_extender.parse_lock_file(_SAMPLE_LOCK)
        versions = {b.source: b.version for b in blocks}
        assert versions["registry.opentofu.org/hashicorp/random"] == "3.6.0"
        assert versions["registry.opentofu.org/hashicorp/null"] == "3.2.0"

    def test_hostname_namespace_type_split(self) -> None:
        blocks = lock_extender.parse_lock_file(_SAMPLE_LOCK)
        random = next(b for b in blocks if "random" in b.source)
        assert random.hostname == "registry.opentofu.org"
        assert random.namespace == "hashicorp"
        assert random.type_ == "random"

    def test_empty_lock_returns_empty_list(self) -> None:
        assert lock_extender.parse_lock_file("# nothing here\n") == []

    def test_block_without_version_is_skipped(self) -> None:
        # tofu wouldn't generate this, but a malformed lock shouldn't
        # crash the runner.
        broken = """provider "registry.opentofu.org/hashicorp/aws" {
  hashes = [
    "h1:abc",
  ]
}
"""
        assert lock_extender.parse_lock_file(broken) == []


class TestFetchH1Hashes:
    def _block(self) -> lock_extender.ProviderBlock:
        return lock_extender.ProviderBlock(
            source="registry.opentofu.org/hashicorp/random",
            version="3.6.0",
            block_text="",
            block_start=0,
            block_end=0,
        )

    def test_returns_h1_for_requested_platform(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "archives": {
                        "linux_amd64": {
                            "url": "https://example.com/zip",
                            "hashes": [
                                "zh:486a1c921e",
                                "h1:abcdefghij=",
                            ],
                        }
                    }
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        out, _ = lock_extender.fetch_h1_hashes(
            "https://api.example.com",
            "token",
            self._block(),
            ["linux_amd64"],
            client=client,
        )
        assert out == {"linux_amd64": "h1:abcdefghij="}

    def test_skips_platforms_without_h1_in_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "archives": {
                        "linux_amd64": {
                            "url": "https://example.com/zip",
                            # No h1 — only zh.
                            "hashes": ["zh:486a1c921e"],
                        }
                    }
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        out, _ = lock_extender.fetch_h1_hashes(
            "https://api.example.com",
            "token",
            self._block(),
            ["linux_amd64"],
            client=client,
        )
        assert out == {}

    def test_handles_404_silently(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        out, cached = lock_extender.fetch_h1_hashes(
            "https://api.example.com",
            "token",
            self._block(),
            ["linux_amd64"],
            client=client,
        )
        assert out == {}
        assert cached == set()

    def test_returns_cached_platforms_from_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "archives": {},
                    "cached_platforms": ["linux_amd64", "linux_arm64"],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        _, cached = lock_extender.fetch_h1_hashes(
            "https://api.example.com",
            "token",
            self._block(),
            ["linux_amd64"],
            client=client,
        )
        assert cached == {"linux_amd64", "linux_arm64"}


class TestSpliceHashesIntoBlock:
    _BLOCK = """provider "registry.opentofu.org/hashicorp/random" {
  version     = "3.6.0"
  constraints = "3.6.0"
  hashes = [
    "h1:current_arch_hash=",
    "zh:1234",
  ]
}"""

    def test_adds_new_h1_line(self) -> None:
        out = lock_extender.splice_hashes_into_block(self._BLOCK, ["h1:other_arch_hash="])
        assert "h1:current_arch_hash=" in out  # existing kept
        assert "h1:other_arch_hash=" in out  # new added

    def test_idempotent_when_hash_already_present(self) -> None:
        out = lock_extender.splice_hashes_into_block(self._BLOCK, ["h1:current_arch_hash="])
        assert out == self._BLOCK

    def test_preserves_existing_indentation(self) -> None:
        out = lock_extender.splice_hashes_into_block(self._BLOCK, ["h1:new="])
        # Indentation of the new line should match the existing "h1:" entry.
        assert '    "h1:new=",\n' in out

    def test_empty_new_hashes_returns_input_unchanged(self) -> None:
        assert lock_extender.splice_hashes_into_block(self._BLOCK, []) == self._BLOCK


class TestEndToEndExtend:
    def test_extends_every_provider_when_mirror_has_h1(self, tmp_path) -> None:
        lock = tmp_path / ".terraform.lock.hcl"
        lock.write_text(_SAMPLE_LOCK)

        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            # Both providers get a synthetic linux_arm64 h1.
            return httpx.Response(
                200,
                json={
                    "archives": {
                        "linux_arm64": {
                            "url": "https://example.com/zip",
                            "hashes": ["h1:NEW_arm64_h1="],
                        }
                    }
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        seen, extended = lock_extender.extend_lock_file(
            lock,
            api_url="https://api.example.com",
            auth_token="tok",
            other_arch="linux_arm64",
            client=client,
        )
        assert seen == 2
        assert extended == 2

        out = lock.read_text()
        # Both blocks now carry the injected h1.
        assert "hashicorp/random" in out
        assert "hashicorp/null" in out
        assert out.count("h1:NEW_arm64_h1=") == 2
        # Original existing entries preserved.
        assert "h1:/xwPFz7kMERBIEk8i6UJt2fTvgzMFbwKlcyCvRJO8Ok=" in out
        assert "h1:NULLFAKEH1FAKE=" in out
        # Each provider got its own mirror query.
        assert sum(1 for p in seen_paths if "random" in p) == 1
        assert sum(1 for p in seen_paths if "null" in p) == 1
        # Idempotence: re-running adds nothing.
        seen2, extended2 = lock_extender.extend_lock_file(
            lock,
            api_url="https://api.example.com",
            auth_token="tok",
            other_arch="linux_arm64",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert seen2 == 2
        # `extended` counts "had h1 available" — re-runs still count.
        assert extended2 == 2
        # But the file content didn't change.
        assert lock.read_text().count("h1:NEW_arm64_h1=") == 2

    def test_falls_through_when_mirror_returns_no_h1(self, tmp_path) -> None:
        lock = tmp_path / ".terraform.lock.hcl"
        lock.write_text(_SAMPLE_LOCK)

        def handler(request: httpx.Request) -> httpx.Response:
            # No h1 AND the response advertises the requested arch IS
            # supposed to be cached — so this is the "compute failed"
            # case, NOT the deliberate-skip case. Caller should see a
            # real gap and run `providers lock` for it.
            return httpx.Response(
                200,
                json={
                    "archives": {},
                    "cached_platforms": ["linux_amd64", "linux_arm64"],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        seen, extended = lock_extender.extend_lock_file(
            lock,
            api_url="https://api.example.com",
            auth_token="tok",
            other_arch="linux_arm64",
            client=client,
        )
        assert seen == 2
        assert extended == 0
        # File unchanged.
        assert lock.read_text() == _SAMPLE_LOCK

    def test_silently_skips_when_arch_not_in_operators_cache_config(self, tmp_path) -> None:
        """Operator has narrowed mirror's `provider_cache.platforms` to
        a single arch; the lock-extender should NOT log a warning or
        ask the orchestrator to fall back — no apply will ever land
        on the other arch."""
        lock = tmp_path / ".terraform.lock.hcl"
        lock.write_text(_SAMPLE_LOCK)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "archives": {},
                    # other_arch (linux_arm64) NOT in this list — the
                    # operator only cares about linux_amd64.
                    "cached_platforms": ["linux_amd64"],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        seen, extended = lock_extender.extend_lock_file(
            lock,
            api_url="https://api.example.com",
            auth_token="tok",
            other_arch="linux_arm64",
            client=client,
        )
        # seen == handled: no fallback needed (gap == 0)
        assert seen == 2
        assert extended == 2
        assert lock.read_text() == _SAMPLE_LOCK


class TestDetectOtherArch:
    def test_returns_linux_amd64_on_aarch64(self, monkeypatch) -> None:
        monkeypatch.setattr(lock_extender.platform, "machine", lambda: "aarch64")
        assert lock_extender.detect_other_arch() == "linux_amd64"

    def test_returns_linux_arm64_on_x86_64(self, monkeypatch) -> None:
        monkeypatch.setattr(lock_extender.platform, "machine", lambda: "x86_64")
        assert lock_extender.detect_other_arch() == "linux_arm64"

    def test_returns_none_on_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(lock_extender.platform, "machine", lambda: "ppc64le")
        assert lock_extender.detect_other_arch() is None


class TestCliMain:
    def test_no_api_returns_0(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("TP_API_URL", raising=False)
        rc = lock_extender.main(argv=["--lock", str(tmp_path / "x.hcl")])
        assert rc == 0

    def test_unknown_arch_returns_0(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TP_API_URL", "https://api.example.com")
        monkeypatch.setattr(lock_extender.platform, "machine", lambda: "sparc")
        rc = lock_extender.main(argv=["--lock", str(tmp_path / "x.hcl")])
        assert rc == 0
