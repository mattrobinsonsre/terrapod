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


class TestBootstrapJoinTokenLimits:
    """A bootstrap join token is bounded (GHSA-93m3-v3h4-4qvw).

    It used to be created with no expiry and no use limit — a permanent,
    unlimited credential, and a listener holding one joins the pool and receives
    every variable the runs it claims resolve. The defence written into the code
    for that was that a listener keeps its certificate on an `emptyDir` and so
    must re-join after every pod replacement; by then that was no longer true
    (`runner/identity.py` keeps it in a Secret in the listener's own namespace),
    so the reason had outlived itself.

    `0` and absent must stay distinguishable. Absent means nobody said, which
    takes the bounded default; `0` means an operator deliberately asked for no
    limit, and quietly tightening that to one use would break the listener they
    set it for.
    """

    def test_the_default_is_one_use_and_a_day(self, monkeypatch) -> None:
        limits = bootstrap._token_limits_from_environment()
        assert limits.max_uses == 1
        assert limits.ttl_seconds == 24 * 60 * 60

    def test_the_dataclass_default_matches_what_the_environment_gives(self) -> None:
        """`_bootstrap_pool` falls back to `TokenLimits()`, so the two must agree.

        Otherwise a caller that passes nothing — a hand-run of the CLI, or a test
        — gets a different bound from the Job, and only one of them is the one
        anybody reviewed.
        """
        assert bootstrap.TokenLimits() == bootstrap._token_limits_from_environment()

    def test_explicit_values_are_read(self, monkeypatch) -> None:
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_TOKEN_MAX_USES="5",
            TERRAPOD_BOOTSTRAP_POOL_TOKEN_TTL_SECONDS="600",
        )
        limits = bootstrap._token_limits_from_environment()
        assert limits.max_uses == 5
        assert limits.ttl_seconds == 600

    def test_zero_is_the_opt_out_and_is_not_confused_with_absent(self, monkeypatch) -> None:
        _set(
            monkeypatch,
            TERRAPOD_BOOTSTRAP_POOL_TOKEN_MAX_USES="0",
            TERRAPOD_BOOTSTRAP_POOL_TOKEN_TTL_SECONDS="0",
        )
        limits = bootstrap._token_limits_from_environment()
        # None is what agent_pool_service already means by "no limit".
        assert limits.max_uses is None
        assert limits.ttl_seconds is None

    def test_each_limit_is_independent(self, monkeypatch) -> None:
        """Unlimited uses with an expiry, and vice versa, are both reasonable."""
        _set(monkeypatch, TERRAPOD_BOOTSTRAP_POOL_TOKEN_MAX_USES="0")
        limits = bootstrap._token_limits_from_environment()
        assert limits.max_uses is None
        assert limits.ttl_seconds == 24 * 60 * 60

    def test_an_empty_value_takes_the_default(self, monkeypatch) -> None:
        """An older chart, or a `null` in values, renders an empty string.

        Reading that as "no limit" would hand back the unbounded token this
        exists to remove, so empty has to mean the same as absent.
        """
        _set(monkeypatch, TERRAPOD_BOOTSTRAP_POOL_TOKEN_MAX_USES="")
        assert bootstrap._token_limits_from_environment().max_uses == 1

    def test_a_non_number_is_refused_rather_than_ignored(self, monkeypatch) -> None:
        _set(monkeypatch, TERRAPOD_BOOTSTRAP_POOL_TOKEN_MAX_USES="two")
        with pytest.raises(SystemExit) as exc:
            bootstrap._token_limits_from_environment()
        assert "TERRAPOD_BOOTSTRAP_POOL_TOKEN_MAX_USES" in str(exc.value)

    def test_a_negative_value_is_refused(self, monkeypatch) -> None:
        """`max_uses=-1` would compare as already-exhausted and refuse every join.

        Silently accepting it produces a pool nothing can join, with a valid-
        looking token row to explain it.
        """
        _set(monkeypatch, TERRAPOD_BOOTSTRAP_POOL_TOKEN_TTL_SECONDS="-1")
        with pytest.raises(SystemExit) as exc:
            bootstrap._token_limits_from_environment()
        assert "TERRAPOD_BOOTSTRAP_POOL_TOKEN_TTL_SECONDS" in str(exc.value)
