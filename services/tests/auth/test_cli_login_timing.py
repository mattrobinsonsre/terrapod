"""The browser hand-off must finish well inside the auth code's lifetime.

`tofu login` opens a browser, and the page at `/auth/cli-complete` hands the
authorization code to the CLI's local listener. Two independent constants
govern that hand-off, in two languages:

  * `AUTH_CODE_TTL` here -- how long the code stays redeemable;
  * `POLL_TIMEOUT_MS` in the page -- how long it waits before giving up on the
    automatic delivery and showing the manual link.

They were 60s and 60s. So a user who needed the manual link was handed a code
that had already expired, every time: the fallback could not work, by
arithmetic rather than by race. On Safari that fallback is the ONLY path,
because WebKit blocks the page's mixed-content fetch to `http://127.0.0.1`
that Chromium permits, so `tofu login` was simply broken there.

Neither file can see the other, and nothing connected them, which is why the
two were allowed to meet. This is that connection.
"""

import pathlib
import re

import pytest

from terrapod.auth.auth_state import AUTH_CODE_TTL

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _web_source() -> str:
    """The CLI hand-off page, from a checkout or the test image.

    Raises rather than skipping. A timing gate that cannot read one of the two
    numbers it compares proves nothing, and would pass silently for exactly the
    releases where someone had stopped copying the file.
    """
    rel = "web/src/app/auth/cli-complete/page.tsx"
    for base in (_TESTS_DIR.parents[2], _TESTS_DIR.parents[1]):  # local, docker
        candidate = base / rel
        if candidate.exists():
            return candidate.read_text()
    raise FileNotFoundError(
        f"Cannot find {rel} from {_TESTS_DIR}. If this is the test image, it "
        "needs a COPY line in docker/Dockerfile.test — this gate reads it, so "
        "without it the gate would pass vacuously."
    )


def _poll_timeout_seconds() -> float:
    src = _web_source()
    # Anchored to end-of-statement on purpose. The loose form matched the
    # leading digits of an EXPRESSION, so `const POLL_TIMEOUT_MS = 60 * 1000`
    # read as 60 MILLISECONDS and every assertion here passed while the real
    # timeout was the 60s bug this gate exists to prevent.
    m = re.search(r"const\s+POLL_TIMEOUT_MS\s*=\s*([0-9_]+)\s*$", src, re.MULTILINE)
    assert m, (
        "POLL_TIMEOUT_MS must be declared as a bare integer literal in "
        "milliseconds (e.g. `const POLL_TIMEOUT_MS = 20_000`). An expression "
        "such as `60 * 1000` is refused: this gate cannot evaluate one, and a "
        "partial match silently reads the wrong number. If it was renamed, "
        "update this gate rather than deleting it — it is the only thing tying "
        "the page's wait to the code's lifetime."
    )
    return int(m.group(1).replace("_", "")) / 1000.0


def test_the_fallback_appears_well_before_the_code_expires():
    """The bug this exists to prevent, stated as a number.

    A 2x margin, not merely "less than": the user still has to read the page
    and act after the fallback appears, and the code has to survive that too.
    """
    poll = _poll_timeout_seconds()
    assert poll * 2 <= AUTH_CODE_TTL, (
        f"the hand-off page waits {poll}s before offering the manual link, but "
        f"the auth code expires after {AUTH_CODE_TTL}s. A user who reaches the "
        "fallback would be handed a code that is already dead or about to be. "
        "Raise AUTH_CODE_TTL or lower POLL_TIMEOUT_MS."
    )


def test_the_code_outlives_the_hand_off_by_enough_for_a_human():
    """A floor, not just a ratio.

    The ratio test above is satisfied by shrinking the poll as easily as by
    keeping the TTL, so on its own it would let AUTH_CODE_TTL drift back to 60s
    unnoticed -- caught when exactly that mutation passed. After the fallback
    appears the user still has to read it, act, and possibly clear a browser
    prompt, and the code has to survive all of it. Two minutes is the floor
    below which that stops being comfortable.
    """
    assert AUTH_CODE_TTL >= 120, (
        f"AUTH_CODE_TTL={AUTH_CODE_TTL}s leaves too little room after the "
        f"hand-off gives up at {_poll_timeout_seconds()}s. The 60s default was "
        "what made `tofu login` impossible to complete on Safari."
    )


def test_the_wait_is_short_enough_to_be_a_localhost_call():
    """This is a call to the user's own machine: it answers in milliseconds or
    it is not going to answer. Waiting a minute to conclude that taught the
    user nothing and burned the code's lifetime doing it."""
    assert _poll_timeout_seconds() <= 30, (
        "the hand-off waits longer than a call to 127.0.0.1 could plausibly "
        "take; a browser that is going to block it blocks it immediately"
    )


def test_a_blocked_delivery_navigates_rather_than_only_offering_a_link():
    """The fix for Safari, pinned.

    A rejected `no-cors` fetch means delivery definitely failed -- an opaque
    success resolves. A top-level navigation to the same URL is not subresource
    content, so it is allowed where the fetch was not. Doing it automatically
    is what makes `tofu login` work on Safari without the user clicking
    anything; reverting to a link-only fallback silently restores the breakage
    for every WebKit user.
    """
    src = _web_source()
    catch = src[src.index(".catch(") :]
    catch = catch[: catch.index("}, [code")]
    # Commented-out code is not code. A plain substring check passed with the
    # navigation sitting behind a `//`, which is exactly the regression this
    # asserts against.
    catch = "\n".join(line for line in catch.splitlines() if not line.strip().startswith("//"))
    assert "window.location.href = localhostUrl" in catch, (
        "the failed-delivery path no longer navigates to the CLI listener. "
        "Safari blocks the fetch that Chromium allows, so without this "
        "navigation `tofu login` cannot complete there at all."
    )


@pytest.mark.parametrize("name", ["AUTH_CODE_TTL"])
def test_the_code_lifetime_stays_inside_the_oauth_recommendation(name):
    """RFC 6749 s4.1.2 recommends a maximum authorization code lifetime of ten
    minutes. The code is single-use and PKCE-bound, so a longer window is not
    the risk a longer-lived bearer token would be -- but the recommendation is
    still the ceiling, and this stops a future timeout problem being 'fixed'
    by pushing the TTL somewhere indefensible."""
    assert AUTH_CODE_TTL <= 600, f"{name}={AUTH_CODE_TTL}s exceeds the RFC 6749 recommendation"
