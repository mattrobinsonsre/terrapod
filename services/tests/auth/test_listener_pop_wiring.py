"""The proof is only worth the places that apply it.

Each guard here is positional or structural — a behavioural test cannot see any
of them, because the code keeps working (and keeps authenticating) when they
regress. They just stop proving possession, which is the whole point.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "terrapod"


class TestEveryListenerCallSigns:
    def test_no_call_site_takes_the_unsigned_default(self):
        """`_auth_headers()` with no arguments returns the certificate alone.

        That default exists so the signature is opt-in per call site, which means
        a new call site added later silently sends an unsigned, replayable
        request. Nothing fails; the request succeeds while the API has the
        setting off, and 401s once it is on.
        """
        # `_auth_headers(self, method="GET", path="")` has TWO defaults, and the
        # signing branch requires a non-empty `path` -- so an empty path means no
        # signature at all, not a signature over an empty path. A matcher for the
        # zero-argument spelling therefore misses `_auth_headers("POST")`, which
        # takes the method and leaves the path defaulted: one positional
        # argument, no signature, a replayable bearer-certificate request, and
        # nothing else fails because the API accepts the certificate form when
        # PoP is off. So require BOTH arguments at every call site rather than
        # refusing the bare one (#2715).
        src = (ROOT / "runner/listener.py").read_text()
        starved = []
        for call in ast.walk(ast.parse(src)):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
            if name != "_auth_headers":
                continue
            # An argument count on ONE call, not a coverage floor: this asks
            # whether this call site passes both parameters, and the other
            # direction -- the call-site SET growing -- is the equality below.
            if len(call.args) + len(call.keywords) < 2:
                starved.append(f"line {call.lineno}: {len(call.args) + len(call.keywords)} arg(s)")
        assert not starved, (
            f"{len(starved)} _auth_headers() call site(s) do not pass both the method AND "
            "the path, so the certificate goes unsigned -- the signing branch needs a "
            f"non-empty path: {starved}. Pass both, or route the call through "
            "_sign_headers via arequest_with_retry's headers_factory."
        )
        # The other direction: a matcher that stops finding the call reports
        # nothing. Asserted as an equality, so a NEW call site is visible too.
        found = [
            c
            for c in ast.walk(ast.parse(src))
            if isinstance(c, ast.Call)
            and (getattr(c.func, "attr", None) or getattr(c.func, "id", None)) == "_auth_headers"
        ]
        assert len(found) == 1, f"expected 1 _auth_headers call site, found {len(found)}"


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


class TestTheFingerprintPathIsGatedToo:
    def test_the_shared_fingerprint_helper_enforces_proof_of_possession(self):
        """#2602 added a THIRD way into listener auth, and the gate above cannot see it.

        `TestBothServerPathsEnforceIt` slices the two named auth functions and
        requires the call inside each. Both now delegate to
        `_identity_from_fingerprint` for a request carrying a fingerprint instead of a
        certificate — so that body satisfies the older gate via its certificate
        fallback while the fingerprint branch could have no gate at all.

        A fingerprint is public material that travels on every request, exactly like
        the certificate it replaces, so it is replayable in precisely the same way and
        needs precisely the same proof.
        """
        src = (ROOT / "api/dependencies.py").read_text()
        start = src.index("async def _identity_from_fingerprint(")
        nxt = src.find("\nasync def ", start + 1)
        nxt2 = src.find("\ndef ", start + 1)
        end = min(x for x in (nxt, nxt2, len(src)) if x != -1)
        body = src[start:end]
        assert "_enforce_listener_pop(" in body, (
            "_identity_from_fingerprint does not call _enforce_listener_pop, so a "
            "request authenticating on a fingerprint is accepted on public material "
            "alone and a captured header can be replayed for as long as it is valid."
        )

    def test_the_gate_verifies_against_the_recorded_key_not_the_request(self):
        """The fingerprint path must verify against what the API STORED.

        Passing anything derived from the request would make the proof circular: the
        caller would be supplying both the signature and the key it is checked
        against, which is no proof at all.
        """
        src = (ROOT / "api/dependencies.py").read_text()
        start = src.index("async def _identity_from_fingerprint(")
        nxt = src.find("\nasync def ", start + 1)
        end = nxt if nxt != -1 else len(src)
        body = src[start:end]
        assert "public_key=public_key" in body, "the gate is not given the stored key"
        assert "load_der_public_key" in body, "the stored key is not being decoded"
        # The key must come from the lookup, not from anything parsed out of the request.
        assert "x509.load_pem" not in body, (
            "the fingerprint path parses a certificate out of the request, which is "
            "the attacker-supplied material this path exists to stop trusting"
        )
