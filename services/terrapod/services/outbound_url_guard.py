"""Refuse outbound requests to addresses a user should not be able to reach.

Terrapod makes HTTP requests to URLs its users supply — notification webhooks
and run-task callbacks. Without a guard that is a server-side request forgery
primitive: the API server sits inside the cluster, so a URL naming an
internal-only service reaches something the user has no network path to, and
both callers store part of the response where the same user can read it. That
makes it a *full-read* SSRF rather than a blind one, which is the difference
between probing and exfiltration (GHSA-q5m2-x8wm-34q9).

**Resolve, then judge every answer.** Checking the hostname's text is not a
control: `evil.test` resolving to 127.0.0.1 passes any string check ever
written. So the name is resolved and *every* returned address must be
acceptable — a host answering with one public and one private address is
refused, because which one a later connection picks is not ours to choose.

**What remains, stated rather than implied.** The resolution here and the
connection the caller then makes are two separate lookups, so a name that
changes answer in between is not covered (DNS rebinding). Closing that needs the
connection pinned to the address that was judged, which means a custom transport
and a rethink of TLS verification; it is a materially smaller hole than the one
this closes, and worth doing separately rather than claiming it is done.
Redirects are the other half: `httpx` does not follow them by default and the
two callers do not enable them, so a permitted URL cannot bounce to a forbidden
one. That is pinned by a source test rather than a runtime assertion —
`test_outbound_sinks.py` reads the call sites — because checking an attribute on
the client at request time is satisfied by any mock and so proves nothing where
it would actually run.

**Private space is allowed by default, and that is the considered position.**
Terrapod is self-hosted and authenticated, and a webhook receiver on the same
private network is the ordinary case — an in-cluster Alertmanager, a corporate
endpoint on RFC1918. Refusing it by default would import a threat model from
products whose tenants are strangers, and would break working deployments to
prevent something an IaC orchestrator's threat model already accepts: a user who
can run anything here already has a runner executing arbitrary Terraform.

So this guard is narrow on purpose. It refuses the two ranges nobody's intended
use is served by — loopback and link-local — and leaves the rest to
`block_private_addresses` for operators who do not extend that trust, with
`allowed_hosts` / `allowed_cidrs` to carve out specifics either way. A guard
operators must disable wholesale to get their job done protects nobody.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

#: The only schemes worth allowing. `file://`, `gopher://` and friends turn a
#: request into a local read or a protocol-smuggling primitive; nothing Terrapod
#: delivers is anything but HTTP.
ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedURLError(ValueError):
    """The URL must not be requested.

    A `ValueError` so callers that already translate one into a 422 need no new
    branch. The message names the reason and the address, because an operator
    whose legitimate endpoint is refused needs to know which allow-list to add
    it to.
    """


def _is_forbidden(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, block_private: bool
) -> str | None:
    """Why this address is out of bounds, or None if it is fine.

    **Two tiers, and the split is the whole design.**

    Always refused: loopback and link-local. Neither has ever been a legitimate
    webhook target. Loopback addresses the API server's own surfaces from inside
    its own trust boundary, and link-local holds 169.254.169.254 — cloud
    instance metadata. No "we trust our users" argument covers those, because
    nobody's intended use is served by allowing them.

    Refused only on request: private space. That is where a self-hosted
    platform's real webhook receivers live — an in-cluster Alertmanager, a
    corporate endpoint on RFC1918 — so refusing it by default breaks the
    ordinary case to prevent something a trusted internal user has little reason
    to do and, on an agent-mode deployment, could largely do anyway by queueing
    a run. Operators who do not extend that trust set
    `outbound_requests.block_private_addresses`.
    """
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        # 169.254.169.254 lives here — AWS/GCP/Azure instance metadata.
        return "link-local (cloud instance metadata lives here)"
    if ip.is_unspecified:
        return "unspecified"
    if block_private and ip.is_private:
        return "private (outbound_requests.block_private_addresses is on)"
    # An IPv4 address wearing an IPv6 costume still routes to the IPv4 target,
    # so it is judged as what it actually reaches rather than as what it looks
    # like — ::ffff:127.0.0.1 is loopback however it is spelled.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_forbidden(mapped, block_private=block_private)
    return None


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Exact, case-insensitive hostname match. No wildcards.

    A wildcard in an SSRF allow-list is a way to accidentally permit far more
    than intended; if an operator needs a range they can say so in CIDR terms,
    where the breadth is visible.
    """
    h = host.strip().lower().rstrip(".")
    return any(h == a.strip().lower().rstrip(".") for a in allowed_hosts if a.strip())


def _ip_allowed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allowed_cidrs: list[str]
) -> bool:
    for raw in allowed_cidrs:
        raw = raw.strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            # Misconfiguration must not silently widen the guard, and must not
            # take the feature down either: skip the entry and say so.
            logger.warning("ignoring unparseable allowed CIDR", cidr=raw)
            continue
        if ip.version == net.version and ip in net:
            return True
    return False


async def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address the name answers with.

    Through the running loop's resolver so a slow or hostile DNS server cannot
    stall the event loop — the same reason nothing else here blocks.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise BlockedURLError(f"could not resolve {host!r}: {exc}") from exc

    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = info[4][0]
        try:
            out.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    if not out:
        raise BlockedURLError(f"{host!r} resolved to no usable address")
    return out


async def validate_outbound_url(url: str) -> None:
    """Raise `BlockedURLError` unless this URL is safe to request.

    Called before every request to a user-supplied address. Returns None so the
    caller reads as an assertion; the exception carries the reason.
    """
    from terrapod.config import settings

    cfg = settings.outbound_requests

    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURLError(
            f"scheme {parts.scheme or '(none)'!r} is not allowed — "
            f"use one of: {', '.join(sorted(ALLOWED_SCHEMES))}"
        )

    host = parts.hostname
    if not host:
        raise BlockedURLError("the URL has no host")

    # An operator naming a host outright vouches for it, so it is not resolved
    # and judged — that is the whole point of the escape hatch, and it is also
    # the only way to permit a name whose address legitimately changes.
    if _host_allowed(host, cfg.allowed_hosts):
        return

    for ip in await _resolve(host):
        if _ip_allowed(ip, cfg.allowed_cidrs):
            continue
        reason = _is_forbidden(ip, block_private=cfg.block_private_addresses)
        if reason:
            raise BlockedURLError(
                f"{host!r} resolves to {ip} which is {reason}. Terrapod refuses "
                f"outbound requests to addresses a user could not otherwise reach. "
                f"If this endpoint is meant to be reachable, add it to "
                f"outbound_requests.allowed_hosts or outbound_requests.allowed_cidrs."
            )
