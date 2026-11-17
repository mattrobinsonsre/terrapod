"""What a deployment is willing to serve, as opposed to where it fetches from (#2589).

`registry.provider_cache.upstream_registries` restricts which HOSTS we reach.
Nothing restricted WHAT, so a workspace naming any package got it faithfully
fetched, verified and served -- verification working perfectly while an artifact
signed by its own author executes. Verification answers "is this what the
publisher published"; it has no opinion on whether this deployment wanted that
publisher's package at all, and no amount of scanning closes that gap.

**It ships observe-only, and that ordering is the whole of what makes it usable**
(#2589 decision 5). Nobody can hand-write an allowlist for an estate they do not
yet understand: deny-all is unusable on day one and allow-all makes the claim
false. So `observe` records what WOULD have been refused without refusing it,
which is how an operator learns what their own estate actually pulls. Promoting
that set is then one review rather than an archaeology project.

**Enforcement fails closed, and the observe-first ordering is what makes that
safe** (decision 6) -- an operator enforces against a set they have already seen
and approved, so it is not a cliff they walk off on upgrade.

The default is `off`, not `observe`: an operator who has configured nothing gets
no new log line per fetch, because a control nobody asked for that fills the log
is how the feature gets switched off wholesale before it is ever used.
"""

from __future__ import annotations

import fnmatch

from terrapod.config import settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


class ContentRefused(PermissionError):
    """An artifact this deployment is not willing to serve.

    Subclasses `PermissionError` rather than `ValueError` for the same reason
    `RepositoryNotAllowed` does: a bare `except OSError` meant for the network
    must not swallow a policy refusal and leave it looking like a flaky fetch.
    """


def _patterns(ecosystem: str) -> list[str]:
    """The patterns for one ecosystem, plus the ones that apply to every cache.

    Two scopes because both are genuinely wanted: an estate that mirrors one
    internal namespace everywhere (`@acme/*`) should say so once, and a rule that
    is only meaningful for one ecosystem (a Go module path) should not have to be
    repeated under the others.
    """
    cfg = settings.registry.content_allowlist
    per = dict(cfg.ecosystems or {})
    return list(cfg.packages or []) + list(per.get(ecosystem) or [])


def _matches(name: str, patterns: list[str]) -> bool:
    """Whether a package name matches any pattern.

    `fnmatch` rather than a regex, deliberately: an operator writes
    `@acme/*` or `github.com/acme/*`, and a regex here would make a
    mis-escaped `.` silently match more than it reads as. Case-folded, because
    every ecosystem this guards addresses packages case-insensitively or
    normalises before it gets here.
    """
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, p.strip().lower()) for p in patterns if p.strip())


def check(ecosystem: str, name: str, filename: str) -> None:
    """Apply the allowlist to one artifact about to be fetched.

    Raises `ContentRefused` only in `enforce`. In `observe` it logs and returns,
    which is the mode an operator runs first.

    **An EMPTY allowlist in `enforce` refuses everything, on purpose.** That is
    fail-closed, and it is loud rather than silent -- every fetch fails at once
    and the log says why, which is a better failure than a mode that quietly
    permitted everything and made the posture a false claim.
    """
    mode = settings.registry.content_allowlist.mode
    if mode == "off":
        return

    patterns = _patterns(ecosystem)
    if _matches(name, patterns):
        return

    # `patterns` is reported so the log answers "why did this not match" without
    # the reader going to the config; the count rather than the list, because an
    # estate-sized allowlist in every line is what makes a log unreadable.
    if mode == "enforce":
        raise ContentRefused(
            f"{ecosystem} package {name!r} is not on this deployment's content "
            f"allowlist (registry.content_allowlist, {len(patterns)} pattern(s) "
            f"for {ecosystem}), so it will not be fetched. Add a pattern that "
            f"matches it, or set the mode to 'observe' while you build the list."
        )

    logger.info(
        "content allowlist would refuse this artifact",
        ecosystem=ecosystem,
        name=name,
        filename=filename,
        patterns=len(patterns),
        mode=mode,
    )
