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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services.notification_service import deliver_generic
from terrapod.services.outbound_url_guard import BlockedURLError, validate_outbound_url

pytestmark = pytest.mark.asyncio

_PROXY = "http://proxy.corp.test:3128"


@pytest.fixture(autouse=True)
def _no_proxy_environment(monkeypatch):
    """Every test starts with no proxy configured, whatever the shell running
    the suite exports — the guard reads the proxy environment (#1636)."""
    for name in ("http", "https", "all", "no"):
        monkeypatch.delenv(f"{name}_proxy", raising=False)
        monkeypatch.delenv(f"{name.upper()}_PROXY", raising=False)


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
            ("2002:7f00:1::", "loopback"),  # 6to4 carrying 127.0.0.1
            ("2002:a9fe:a9fe::", "link-local"),  # 6to4 carrying the metadata address
            ("64:ff9b::7f00:1", "loopback"),  # NAT64 carrying 127.0.0.1
            ("64:ff9b::a9fe:a9fe", "link-local"),  # NAT64 carrying the metadata address
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

    async def test_a_6to4_address_is_judged_by_the_ipv4_it_reaches(self) -> None:
        """`2002:7f00:1::` is 127.0.0.1 in a 6to4 wrapper. It reads as private
        space rather than loopback, so until this it was refused only on a
        deployment that had turned private blocking on — which is not what that
        switch is for."""
        with _resolves_to("2002:7f00:1::"):
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://webhook.test/hook")

    async def test_a_nat64_address_is_judged_by_the_ipv4_it_reaches(self) -> None:
        """`64:ff9b::a9fe:a9fe` reaches cloud instance metadata through a NAT64
        gateway. Every `is_*` flag on it is False, so it reads as an ordinary
        global address and nothing else in the guard catches it."""
        with _resolves_to("64:ff9b::a9fe:a9fe"):
            with pytest.raises(BlockedURLError, match="link-local"):
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

    async def test_localhost_is_refused_without_asking_the_resolver(self) -> None:
        """Loopback by definition (RFC 6761), with or without a proxy."""
        with patch("terrapod.services.outbound_url_guard._resolve", new=AsyncMock()) as resolve:
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://localhost:8000/x")
        resolve.assert_not_called()


def _unresolvable():
    """Resolution as on a restricted network: the API pod cannot resolve
    external names, and the guard's lookup fails."""
    return patch(
        "terrapod.services.outbound_url_guard._resolve",
        new=AsyncMock(
            side_effect=BlockedURLError(
                "could not resolve 'hooks.example.com': [Errno -2] Name or service not known"
            )
        ),
    )


class TestThroughAnEgressProxy:
    """A proxy resolves and connects, so the guard does not resolve a proxied
    name itself (#1636). On a restricted network the pod may be unable to, and
    before this every delivery that worked on 1.6 was refused."""

    @pytest.mark.parametrize("var", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"])
    async def test_a_proxied_hostname_is_not_resolved(self, monkeypatch, var) -> None:
        monkeypatch.setenv(var, _PROXY)
        with _unresolvable() as resolve:
            await validate_outbound_url("https://hooks.example.com/x")
        resolve.assert_not_called()

    async def test_the_http_proxy_carries_http(self, monkeypatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", _PROXY)
        with _unresolvable() as resolve:
            await validate_outbound_url("http://hooks.example.com/x")
        resolve.assert_not_called()

    async def test_a_proxy_for_another_scheme_does_not_count(self, monkeypatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", _PROXY)
        with _unresolvable() as resolve:
            with pytest.raises(BlockedURLError, match="could not resolve"):
                await validate_outbound_url("https://hooks.example.com/x")
        resolve.assert_awaited_once()

    @pytest.mark.parametrize(
        "no_proxy",
        [
            "hooks.internal",
            ".internal",
            "internal",
            "*.internal",
            "*",
            "other.test, hooks.internal:8443",
            "https://hooks.internal",
        ],
    )
    async def test_a_host_no_proxy_sends_direct_is_still_resolved(
        self, monkeypatch, no_proxy
    ) -> None:
        """The client connects to it directly, so it is resolved and judged —
        including for NO_PROXY forms httpx reads more narrowly than this does."""
        monkeypatch.setenv("HTTPS_PROXY", _PROXY)
        monkeypatch.setenv("NO_PROXY", no_proxy)
        with _resolves_to("127.0.0.1"):
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://hooks.internal/x")

    async def test_lower_case_no_proxy_counts_too(self, monkeypatch) -> None:
        monkeypatch.setenv("https_proxy", _PROXY)
        monkeypatch.setenv("no_proxy", "hooks.internal")
        with _resolves_to("127.0.0.1"):
            with pytest.raises(BlockedURLError, match="loopback"):
                await validate_outbound_url("https://hooks.internal/x")

    async def test_a_no_proxy_suffix_matches_on_a_label_boundary(self, monkeypatch) -> None:
        # `example.com` does not send `badexample.com` direct — httpx agrees.
        monkeypatch.setenv("HTTPS_PROXY", _PROXY)
        monkeypatch.setenv("NO_PROXY", "example.com")
        with _unresolvable() as resolve:
            await validate_outbound_url("https://badexample.com/x")
        resolve.assert_not_called()

    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("https://127.0.0.1/x", "loopback"),
            ("https://[::1]/x", "loopback"),
            ("https://[::ffff:127.0.0.1]/x", "loopback"),
            ("https://[2002:7f00:1::]/x", "loopback"),
            ("https://[64:ff9b::7f00:1]/x", "loopback"),
            ("http://[64:ff9b::a9fe:a9fe]/latest/meta-data", "link-local"),
            ("http://169.254.169.254/latest/meta-data", "link-local"),
            ("https://0.0.0.0/x", "unspecified"),
            # Legacy spellings a proxy would still read as an address.
            ("https://2130706433/x", "loopback"),
            ("https://127.1/x", "loopback"),
            ("http://0xa9fea9fe/latest/meta-data", "link-local"),
            ("https://localhost/x", "loopback"),
            ("https://api.localhost/x", "loopback"),
        ],
    )
    async def test_a_literal_address_is_still_refused(self, monkeypatch, url, why) -> None:
        monkeypatch.setenv("HTTPS_PROXY", _PROXY)
        monkeypatch.setenv("HTTP_PROXY", _PROXY)
        with patch("terrapod.services.outbound_url_guard._resolve", new=AsyncMock()) as resolve:
            with pytest.raises(BlockedURLError, match=why):
                await validate_outbound_url(url)
        resolve.assert_not_called()

    async def test_a_private_literal_follows_the_private_space_settings(self, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", _PROXY)
        await validate_outbound_url("https://10.0.0.5/x")

        settings.outbound_requests.block_private_addresses = True
        with pytest.raises(BlockedURLError, match="private"):
            await validate_outbound_url("https://10.0.0.5/x")

        settings.outbound_requests.allowed_cidrs = ["10.0.0.0/8"]
        await validate_outbound_url("https://10.0.0.5/x")

    async def test_an_allowed_host_still_skips_every_check(self, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", _PROXY)
        settings.outbound_requests.allowed_hosts = ["127.0.0.1"]
        await validate_outbound_url("https://127.0.0.1/x")


def _ok_client() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestDeliveryThroughAnEgressProxy:
    """The failure as reported: a notification the API pod cannot resolve."""

    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_it_is_delivered_through_the_proxy(self, client_cls, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", _PROXY)
        client_cls.return_value = _ok_client()
        with _unresolvable() as resolve:
            result = await deliver_generic("https://hooks.example.com/x", {"msg": "hi"})
        assert result["success"] is True, result
        resolve.assert_not_called()

    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_without_a_proxy_it_is_still_refused(self, client_cls) -> None:
        client_cls.return_value = _ok_client()
        with _unresolvable():
            result = await deliver_generic("https://hooks.example.com/x", {"msg": "hi"})
        assert result["success"] is False
        assert "could not resolve" in result["body"]
        client_cls.assert_not_called()
