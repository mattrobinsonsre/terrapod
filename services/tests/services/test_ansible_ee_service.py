"""Discovering execution environments, and resolving one for a workspace.

The assertions that earn their place are the ones about **not knowing**. The
built-in set is read over the network and the write-time check refuses a line
absent from it, so "the registry did not answer" and "the registry answered and
holds nothing" have opposite consequences: accept, and refuse. A fixture set
built only from the happy path cannot tell those apart, and the permissive one
is the branch an ordinary test never reaches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.config import settings
from terrapod.db.models import DEFAULT_ANSIBLE_LINE
from terrapod.services import ansible_ee_service as ee

REPO = "ghcr.io/mattrobinsonsre/terrapod-ee"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Caching is tested on its own; everywhere else it would only add a mock."""
    monkeypatch.setattr(settings.ansible, "availability_ttl_seconds", 0)
    monkeypatch.setattr(settings.ansible, "ee_repository", REPO)
    monkeypatch.setattr(settings.ansible, "execution_environments", {})
    monkeypatch.setattr(settings.registry, "cache_only", False)


def _repo(upstream: str | None):
    row = AsyncMock()
    row.upstream = upstream
    return row


class TestTheLineFilter:
    def test_patch_tags_are_excluded_not_collapsed(self) -> None:
        """`2.21.5` existing does not mean the moving `2.21` tag does.

        Collapsing it would offer a line that cannot be pulled, which is the
        failure this read exists to prevent.
        """
        assert ee._lines(["2.21.5", "2.20", "latest", "2.21"]) == ["2.21", "2.20"]

    def test_ordering_is_numeric_and_newest_first(self) -> None:
        """Lexical ordering would put 2.9 above 2.21, which a select would show."""
        assert ee._lines(["2.9", "2.21", "2.12"]) == ["2.21", "2.12", "2.9"]

    def test_duplicates_across_local_and_upstream_collapse(self) -> None:
        assert ee._lines(["2.21", "2.21", "2.12"]) == ["2.21", "2.12"]


class TestWhereTheListingComesFrom:
    async def test_a_locally_pushed_repository_needs_no_network(self) -> None:
        """Its rows are the whole truth, not a cached subset of an upstream's."""
        with (
            patch.object(
                ee.registry_service, "get_repository", AsyncMock(return_value=_repo(None))
            ),
            patch.object(ee.registry_service, "list_tags", AsyncMock(return_value=["2.21"])),
            patch.object(ee.pullthrough_service, "upstream_tags", AsyncMock()) as remote,
            patch.object(ee.pullthrough_service, "list_tags_at", AsyncMock()) as direct,
        ):
            assert await ee._builtin_lines(AsyncMock()) == ["2.21"]
        remote.assert_not_awaited()
        direct.assert_not_awaited()

    async def test_a_mirror_unions_local_with_upstream(self, monkeypatch) -> None:
        """A line already cached stays selectable even as upstream gains more."""
        with (
            patch.object(
                ee.registry_service, "get_repository", AsyncMock(return_value=_repo("ghcr.io"))
            ),
            patch.object(ee.registry_service, "list_tags", AsyncMock(return_value=["2.12"])),
            patch.object(
                ee.pullthrough_service, "resolve_upstream", lambda _: ("ghcr.io", "x/terrapod-ee")
            ),
            patch.object(ee.pullthrough_service, "upstream_tags", AsyncMock(return_value=["2.21"])),
        ):
            assert await ee._builtin_lines(AsyncMock()) == ["2.21", "2.12"]

    async def test_a_registry_that_is_not_an_upstream_is_listed_directly(self) -> None:
        """The default case: ghcr.io is not a pull-through upstream unless added.

        Listing it is not the SSRF the upstream allow-list guards against -- the
        host came from this deployment's own Helm values.
        """
        with (
            patch.object(ee.registry_service, "get_repository", AsyncMock(return_value=None)),
            patch.object(ee.pullthrough_service, "resolve_upstream", lambda _: None),
            patch.object(
                ee.pullthrough_service, "list_tags_at", AsyncMock(return_value=["2.21", "2.20"])
            ) as direct,
        ):
            assert await ee._builtin_lines(AsyncMock()) == ["2.21", "2.20"]
        assert direct.await_args.args[0] == "https://ghcr.io"
        assert direct.await_args.args[1] == "mattrobinsonsre/terrapod-ee"

    async def test_sealed_returns_local_rows_as_an_authoritative_answer(self, monkeypatch) -> None:
        """In a sealed deployment what is cached IS what can run.

        So this is a complete answer, not a degraded one -- and the write-time
        refusal is correct to act on it.
        """
        monkeypatch.setattr(settings.registry, "cache_only", True)
        with (
            patch.object(
                ee.registry_service, "get_repository", AsyncMock(return_value=_repo("ghcr.io"))
            ),
            patch.object(ee.registry_service, "list_tags", AsyncMock(return_value=["2.16"])),
            patch.object(ee.pullthrough_service, "list_tags_at", AsyncMock()) as direct,
            patch.object(ee.pullthrough_service, "upstream_tags", AsyncMock()) as remote,
        ):
            assert await ee._builtin_lines(AsyncMock()) == ["2.16"]
        direct.assert_not_awaited()
        remote.assert_not_awaited()


class TestNotKnowing:
    async def test_an_unreachable_registry_is_none_not_an_empty_list(self) -> None:
        """The distinction the whole return type exists for."""
        with (
            patch.object(ee.registry_service, "get_repository", AsyncMock(return_value=None)),
            patch.object(ee.pullthrough_service, "resolve_upstream", lambda _: None),
            patch.object(ee.pullthrough_service, "list_tags_at", AsyncMock(return_value=None)),
        ):
            assert await ee._builtin_lines(AsyncMock()) is None

    async def test_local_rows_do_not_rescue_an_unknown_upstream(self) -> None:
        """Returning the cached subset would look authoritative while omitting
        every line not yet pulled -- and each omission is a refused write."""
        with (
            patch.object(
                ee.registry_service, "get_repository", AsyncMock(return_value=_repo("ghcr.io"))
            ),
            patch.object(ee.registry_service, "list_tags", AsyncMock(return_value=["2.12"])),
            patch.object(
                ee.pullthrough_service, "resolve_upstream", lambda _: ("ghcr.io", "x/terrapod-ee")
            ),
            patch.object(ee.pullthrough_service, "upstream_tags", AsyncMock(return_value=None)),
        ):
            assert await ee._builtin_lines(AsyncMock()) is None

    async def test_a_reference_naming_no_registry_host_is_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(settings.ansible, "ee_repository", "terrapod-ee")
        with patch.object(ee.registry_service, "get_repository", AsyncMock(return_value=None)):
            assert await ee._builtin_lines(AsyncMock()) is None


class TestCaching:
    async def test_only_a_known_answer_is_cached(self, monkeypatch) -> None:
        """Caching "cannot tell" would hold the permissive state open for the
        whole TTL after the registry came back."""
        monkeypatch.setattr(settings.ansible, "availability_ttl_seconds", 300)
        with (
            patch.object(ee, "_read_cache", AsyncMock(return_value=None)),
            patch.object(ee, "_builtin_lines", AsyncMock(return_value=None)),
            patch.object(ee, "_write_cache", AsyncMock()) as write,
        ):
            assert (await ee.availability(AsyncMock())).lines is None
        write.assert_not_awaited()

    async def test_a_known_answer_is_cached_with_the_configured_ttl(self, monkeypatch) -> None:
        monkeypatch.setattr(settings.ansible, "availability_ttl_seconds", 42)
        with (
            patch.object(ee, "_read_cache", AsyncMock(return_value=None)),
            patch.object(ee, "_builtin_lines", AsyncMock(return_value=["2.21"])),
            patch.object(ee, "_write_cache", AsyncMock()) as write,
        ):
            await ee.availability(AsyncMock())
        write.assert_awaited_once_with(["2.21"], 42)

    async def test_a_zero_ttl_never_reads_the_cache(self, monkeypatch) -> None:
        monkeypatch.setattr(settings.ansible, "availability_ttl_seconds", 0)
        with (
            patch.object(ee, "_read_cache", AsyncMock()) as read,
            patch.object(ee, "_builtin_lines", AsyncMock(return_value=["2.21"])),
        ):
            await ee.availability(AsyncMock())
        read.assert_not_awaited()


class TestResolvingAWorkspacesImage:
    def test_an_empty_version_resolves_to_the_default_line(self) -> None:
        """`""` is a value meaning "the deployment's default", not an absence."""
        assert ee.effective_ee_image(ansible_version="", ee_label=None) == (
            f"{REPO}:{DEFAULT_ANSIBLE_LINE}"
        )

    def test_an_explicit_line_is_used(self) -> None:
        assert ee.effective_ee_image(ansible_version="2.16", ee_label=None) == f"{REPO}:2.16"

    def test_an_operator_label_wins_outright(self, monkeypatch) -> None:
        monkeypatch.setattr(
            settings.ansible, "execution_environments", {"hardened": "registry.internal/ee:1"}
        )
        resolved = ee.effective_ee_image(ansible_version="2.16", ee_label="hardened")
        assert resolved == "registry.internal/ee:1"

    def test_an_unknown_label_raises_rather_than_falling_back(self) -> None:
        """Falling back to a line would silently run something else entirely."""
        with pytest.raises(ee.UnknownExecutionEnvironment, match="hardened"):
            ee.effective_ee_image(ansible_version="2.16", ee_label="hardened")


class TestTheWriteTimeCheck:
    async def test_an_unpublished_line_is_refused_naming_what_is_available(self) -> None:
        with patch.object(
            ee, "availability", AsyncMock(return_value=ee.Availability(["2.21"], {}))
        ):
            with pytest.raises(ee.UnpublishedAnsibleLine, match="2.99.*Available: 2.21"):
                await ee.validate_selection(AsyncMock(), ansible_version="2.99", ee_label=None)

    async def test_an_unknown_available_set_ACCEPTS(self) -> None:
        """The assertion an obvious fixture set omits, because its fixture has a list.

        Refusing here would make an ordinary workspace edit depend on registry
        reachability -- an outage mode invented by a validation check.
        """
        with patch.object(ee, "availability", AsyncMock(return_value=ee.Availability(None, {}))):
            await ee.validate_selection(AsyncMock(), ansible_version="2.99", ee_label=None)

    async def test_an_empty_set_still_refuses(self) -> None:
        """The registry answered and holds nothing. That is knowledge, not doubt."""
        with patch.object(ee, "availability", AsyncMock(return_value=ee.Availability([], {}))):
            with pytest.raises(ee.UnpublishedAnsibleLine):
                await ee.validate_selection(AsyncMock(), ansible_version="2.21", ee_label=None)

    async def test_inheriting_is_never_refused(self) -> None:
        """Nothing a caller could have got wrong, so nothing to check."""
        with patch.object(ee, "availability", AsyncMock()) as avail:
            await ee.validate_selection(AsyncMock(), ansible_version="", ee_label=None)
        avail.assert_not_awaited()

    async def test_a_label_short_circuits_the_line_check(self, monkeypatch) -> None:
        """With a label set, `ansible_version` selects nothing -- so an
        unpublished line alongside one must not be refused."""
        monkeypatch.setattr(settings.ansible, "execution_environments", {"h": "r/ee:1"})
        with patch.object(ee, "availability", AsyncMock()) as avail:
            await ee.validate_selection(AsyncMock(), ansible_version="2.99", ee_label="h")
        avail.assert_not_awaited()

    async def test_an_unknown_label_is_always_refused(self) -> None:
        """Local configuration, so "cannot tell" does not arise."""
        with pytest.raises(ee.UnknownExecutionEnvironment):
            await ee.validate_selection(AsyncMock(), ansible_version="", ee_label="nope")
