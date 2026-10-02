"""The proof is only worth the places that apply it.

Each guard here is positional or structural — a behavioural test cannot see any
of them, because the code keeps working (and keeps authenticating) when they
regress. They just stop proving possession, which is the whole point.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2] / "terrapod"


class TestEveryListenerCallSigns:
    def test_no_call_site_takes_the_unsigned_default(self):
        """`_auth_headers()` with no arguments returns the certificate alone.

        That default exists so the signature is opt-in per call site, which means
        a new call site added later silently sends an unsigned, replayable
        request. Nothing fails; the request succeeds while the API has the
        setting off, and 401s once it is on.
        """
        src = (ROOT / "runner/listener.py").read_text()
        bare = re.findall(r"_auth_headers\(\s*\)", src)
        assert not bare, (
            f"{len(bare)} call site(s) use _auth_headers() with no method/path, so they "
            "send the certificate unsigned. Pass the method and path, or route the call "
            "through _sign_headers via arequest_with_retry's headers_factory."
        )


class TestBothServerPathsEnforceIt:
    def test_each_listener_auth_path_calls_the_shared_helper(self):
        """There are two listener auth paths and they must not drift.

        `authenticate_listener` exists because SSE endpoints cannot hold a
        yield-dependency; `get_listener_identity` serves everything else. Both
        reach a point where they have verified only the certificate — public
        material — so a path missing this call accepts a replayed header for ever
        while looking completely authenticated.
        """
        src = (ROOT / "api/dependencies.py").read_text()
        for fn in ("authenticate_listener", "get_listener_identity"):
            start = src.index(f"async def {fn}(")
            # to the next top-level def, which bounds this function's body
            nxt = src.find("\nasync def ", start + 1)
            nxt2 = src.find("\ndef ", start + 1)
            end = min(x for x in (nxt, nxt2, len(src)) if x != -1)
            body = src[start:end]
            assert "_enforce_listener_pop(" in body, (
                f"{fn} does not call _enforce_listener_pop, so that path authenticates "
                "on the certificate alone and a captured header can be replayed."
            )


class TestRenewalSignsPerAttempt:
    def test_the_signature_is_built_inside_the_retry_loop(self):
        """Renewal retries three times with one nonce if signed outside the loop.

        Attempts 2 and 3 would then be rejected as replays — and a 401 from renew
        is deliberately not retried, so the caller falls back to the join token
        and the listener re-registers under a new name every cycle. Quiet, and
        it churns pool membership rather than failing visibly.
        """
        src = (ROOT / "runner/identity.py").read_text()
        fn = src[src.index("async def _call_renew_with_retries(") :]
        fn = fn[: fn.index("\nasync def ", 1)] if "\nasync def " in fn[1:] else fn
        loop = fn.index("for attempt in range(3):")
        sign = fn.index("sign_request(")
        assert sign > loop, (
            "sign_request is called before the retry loop, so all three attempts "
            "share one single-use nonce and the retries are rejected as replays."
        )
