"""Where a user-supplied URL may point (#1541, GHSA-q5m2-x8wm-34q9).

The guard is deliberately narrow, and the tests are written to pin the *narrowness*
as much as the blocking — a guard that refused private space would break the
ordinary self-hosted case (a webhook to an in-cluster service), and a later
well-meaning tightening is exactly the regression worth catching.

DNS is stubbed throughout. Real resolution would make these tests depend on the
network and on whatever the sandbox's resolver decides `localhost` means today.
"""

from __future__ import annotations

import ipaddress
from unittest.mock import patch

import pytest

from terrapod.config import settings
from terrapod.services.outbound_url_guard import BlockedURLError, validate_outbound_url

pytestmark = pytest.mark.asyncio


def _resolves_to(*addrs: str):
    """Patch resolution so a hostname answers with exactly these addresses."""

    async def _fake(host: str):
        return [ipaddress.ip_address(a) for a in addrs]

    return patch("terrapod.services.outbound_url_guard._resolve", side_effect=_fake)


@pytest.fixture(autouse=True)
def _defaults():
    """Restore the shipped configuration around each test."""
    cfg = settings.outbound_requests
    before = (list(cfg.allowed_hosts), list(cfg.allowed_cidrs), cfg.block_private_addresses)
    yield
    cfg.allowed_hosts, cfg.allowed_cidrs, cfg.block_private_addresses = (
        before[0],
        before[1],
        before[2],
    )


class TestTheAlwaysRefused:
    """Two ranges no legitimate webhook has ever targeted."""

    @pytest.mark.parametrize(
        ("addr", "why"),
        [
            ("127.0.0.1", "loopback"),
            ("::1", "loopback"),
            ("169.254.169.254", "link-local"),  # cloud instance metadata
            ("fe80::1", "link-local"),
            ("0.0.0.0", "unspecified"),
        ],
    )
    async def test_it_refuses(self, addr: str, why: str) -> None:
        with _resolves_to(addr):
            with pytest.raises(BlockedURLError) as exc:
                await validate_outbound_url("https://webhook.test/hook")
        assert why in str(exc.value)

    async def test_an_ipv4_mapped_ipv6_address_is_judged_by_what_it_reaches(self) -> None:
        """`::ffff:127.0.0.1` routes to loopback however it is spelled — judging
        the notation rather than the destination is how these guards get bypassed."""
        with _resolves_to("::ffff:127.0.0.1"):
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://webhook.test/hook")

    async def test_a_hostname_that_looks_public_but_resolves_inward_is_refused(self) -> None:
        """The reason the check resolves rather than reading the string: any
        name can be pointed at 127.0.0.1 by whoever controls its zone."""
        with _resolves_to("127.0.0.1"):
            with pytest.raises(BlockedURLError):
                await validate_outbound_url("https://totally-legitimate.example.com/hook")

    async def test_one_bad_answer_among_good_ones_is_still_refused(self) -> None:
        """Which address a later connection picks is not ours to choose, so a
        host answering with both is not safe on the strength of the good one."""
        with _resolves_to("93.184.216.34", "127.0.0.1"):
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://webhook.test/hook")


class TestPrivateSpaceIsAllowedByDefault:
    """The narrowness, pinned.

    Terrapod is self-hosted: a webhook to an in-cluster service or an RFC1918
    endpoint is the ordinary case. Refusing it by default would break working
    deployments to prevent something an IaC orchestrator's threat model already
    accepts — so if one of these ever starts failing, the default has drifted.
    """

    @pytest.mark.parametrize("addr", ["10.0.0.5", "192.168.1.10", "172.16.4.4", "fd00::1"])
    async def test_it_is_permitted(self, addr: str) -> None:
        with _resolves_to(addr):
            await validate_outbound_url("https://alertmanager.internal/hook")

    async def test_until_an_operator_asks_otherwise(self) -> None:
        settings.outbound_requests.block_private_addresses = True
        with _resolves_to("10.0.0.5"):
            with pytest.raises(BlockedURLError, match="private"):
                await validate_outbound_url("https://alertmanager.internal/hook")


class TestTheEscapeHatches:
    async def test_an_allowed_host_skips_the_check_entirely(self) -> None:
        """Including for an address otherwise always refused — an operator
        naming a host outright has said they mean it."""
        settings.outbound_requests.allowed_hosts = ["metadata.internal"]
        with _resolves_to("169.254.169.254"):
            await validate_outbound_url("https://metadata.internal/x")

    async def test_the_host_match_is_case_insensitive_and_dot_tolerant(self) -> None:
        settings.outbound_requests.allowed_hosts = ["Hook.Example.COM"]
        with _resolves_to("127.0.0.1"):
            await validate_outbound_url("https://hook.example.com./x")

    async def test_an_allowed_cidr_permits_the_address(self) -> None:
        settings.outbound_requests.block_private_addresses = True
        settings.outbound_requests.allowed_cidrs = ["10.1.0.0/16"]
        with _resolves_to("10.1.2.3"):
            await validate_outbound_url("https://internal.test/hook")

    async def test_an_address_outside_the_allowed_cidr_is_still_refused(self) -> None:
        settings.outbound_requests.block_private_addresses = True
        settings.outbound_requests.allowed_cidrs = ["10.1.0.0/16"]
        with _resolves_to("10.9.9.9"):
            with pytest.raises(BlockedURLError):
                await validate_outbound_url("https://internal.test/hook")

    async def test_an_unparseable_cidr_is_skipped_not_honoured(self) -> None:
        """A typo must not silently widen the guard, and must not take webhook
        delivery down either."""
        settings.outbound_requests.allowed_cidrs = ["not-a-cidr"]
        with _resolves_to("127.0.0.1"):
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://webhook.test/hook")


class TestTheUrlItself:
    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1", "ftp://x/y"])
    async def test_only_http_schemes_are_allowed(self, url: str) -> None:
        with pytest.raises(BlockedURLError, match="scheme"):
            await validate_outbound_url(url)

    @pytest.mark.parametrize("url", ["", "   ", "https://", "not a url"])
    async def test_a_url_with_no_host_is_refused(self, url: str) -> None:
        with pytest.raises(BlockedURLError):
            await validate_outbound_url(url)

    async def test_an_ordinary_public_endpoint_is_permitted(self) -> None:
        with _resolves_to("93.184.216.34"):
            await validate_outbound_url("https://hooks.slack.com/services/T/B/x")
