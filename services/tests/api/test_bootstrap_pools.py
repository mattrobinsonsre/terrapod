"""Seeding several agent pools from the bootstrap Job (#1411).

bootstrap writes straight to Postgres, which makes it the only way to register a
pool before an API exists — and the listener's readiness probe does not pass
until it has joined one, so on a green-field install the pools must exist before
anything else starts. It handled exactly one, so multi-pool deployments were
running the CLI in a loop from a hand-written Job.

The tests that matter here are the ones about *not* silently doing the wrong
thing: a shared token, a gap in the indices, a partial failure. Each of those is
invisible until a listener fails to join, long after the Job reported success.
"""

from __future__ import annotations

import os

import pytest

from terrapod.cli import bootstrap


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("TERRAPOD_BOOTSTRAP_POOL"):
            monkeypatch.delenv(key, raising=False)


def _set(monkeypatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


class TestReadingTheEnvironment:
    def test_indexed_pools_are_read_in_order(self, monkeypatch) -> None:
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_COUNT="2",
            TERRAPOD_BOOTSTRAP_POOL_0_NAME="pool-a",
            TERRAPOD_BOOTSTRAP_POOL_0_TOKEN="tok-a",
            TERRAPOD_BOOTSTRAP_POOL_1_NAME="pool-b",
            TERRAPOD_BOOTSTRAP_POOL_1_TOKEN="tok-b",
        )
        assert bootstrap._pools_from_environment() == [
            bootstrap.PoolSpec("pool-a", "tok-a"),
            bootstrap.PoolSpec("pool-b", "tok-b"),
        ]

    def test_a_gap_in_the_indices_is_refused(self, monkeypatch) -> None:
        """Scanning to the first gap would silently drop everything after it.

        The people this feature is for hand-write this Job today, so a gap is
        exactly the mistake to expect — and losing pool 3 silently is how a
        listener ends up never joining with nothing to point at.
        """
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_COUNT="3",
            TERRAPOD_BOOTSTRAP_POOL_0_NAME="a",
            TERRAPOD_BOOTSTRAP_POOL_0_TOKEN="1",
            TERRAPOD_BOOTSTRAP_POOL_2_NAME="c",
            TERRAPOD_BOOTSTRAP_POOL_2_TOKEN="3",
        )
        with pytest.raises(SystemExit, match="POOL_1_NAME"):
            bootstrap._pools_from_environment()

    def test_an_empty_token_is_refused_naming_the_pool(self, monkeypatch) -> None:
        """Almost always a Secret that lacks the expected key."""
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_COUNT="1",
            TERRAPOD_BOOTSTRAP_POOL_0_NAME="pool-a",
            TERRAPOD_BOOTSTRAP_POOL_0_TOKEN="",
        )
        with pytest.raises(SystemExit, match="pool-a"):
            bootstrap._pools_from_environment()

    def test_a_non_numeric_count_is_refused(self, monkeypatch) -> None:
        _set(monkeypatch, TERRAPOD_BOOTSTRAP_POOL_COUNT="lots")
        with pytest.raises(SystemExit, match="not a number"):
            bootstrap._pools_from_environment()

    def test_nothing_configured_means_no_pools(self) -> None:
        assert bootstrap._pools_from_environment() == []


class TestBackwardCompatibility:
    """The single-pool form has to keep behaving exactly as it did."""

    def test_the_legacy_pair_still_works(self, monkeypatch) -> None:
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_NAME="legacy",
            TERRAPOD_BOOTSTRAP_POOL_TOKEN="tok",
        )
        assert bootstrap._pools_from_environment() == [bootstrap.PoolSpec("legacy", "tok")]

    def test_a_legacy_pool_with_no_token_still_generates_one(self, monkeypatch) -> None:
        """`token is None` is what routes to generate-and-print, and that path is
        deliberately reachable only from the single-pool form: printing N
        generated tokens into a Job's logs is not a way to hand out credentials."""
        _set(monkeypatch, TERRAPOD_BOOTSTRAP_POOL_NAME="legacy")
        assert bootstrap._pools_from_environment() == [bootstrap.PoolSpec("legacy", None)]

    def test_the_indexed_form_wins_when_both_are_somehow_present(self, monkeypatch) -> None:
        """The chart refuses to render both, but a hand-written Job could set
        them; preferring the explicit list is the predictable resolution."""
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_COUNT="1",
            TERRAPOD_BOOTSTRAP_POOL_0_NAME="listed",
            TERRAPOD_BOOTSTRAP_POOL_0_TOKEN="tok",
            TERRAPOD_BOOTSTRAP_POOL_NAME="legacy",
        )
        assert [p.name for p in bootstrap._pools_from_environment()] == ["listed"]


class TestDuplicateTokens:
    """The trap that only exists once this is a list.

    `agent_pool_tokens.token_hash` is unique across *all* pools and registration
    skips a hash that already exists. Point two pools at the same Secret — a
    copy-paste away — and the second is created, its token skipped as "already
    exists", and it ends up with no join token while the Job reports success.
    """

    def test_two_pools_sharing_a_token_are_refused(self) -> None:
        pools = [
            bootstrap.PoolSpec("pool-a", "same-token"),
            bootstrap.PoolSpec("pool-b", "same-token"),
        ]
        with pytest.raises(SystemExit) as exc:
            bootstrap._reject_duplicate_tokens(pools)
        # Both names, because "a duplicate exists" is not actionable on its own.
        assert "pool-a" in str(exc.value) and "pool-b" in str(exc.value)

    def test_distinct_tokens_are_fine(self) -> None:
        bootstrap._reject_duplicate_tokens(
            [bootstrap.PoolSpec("a", "one"), bootstrap.PoolSpec("b", "two")]
        )

    def test_a_pool_awaiting_a_generated_token_is_not_a_duplicate(self) -> None:
        """Two `None`s are two tokens that do not exist yet, not one shared one."""
        bootstrap._reject_duplicate_tokens(
            [bootstrap.PoolSpec("a", None), bootstrap.PoolSpec("b", None)]
        )
