"""How good the operator-supplied secret material actually is.

Two secrets decide how much of Terrapod an attacker can forge or decrypt, and
both shipped with a weak default (GHSA-hc47-q72v-4vcm):

* **The token signing key.** `get_token_signing_key()` falls back to
  `sha256(database_url)`, and that one key signs four stateless token families —
  runner tokens, run-task callback tokens, download tickets and Slack link
  tokens. Anyone who learns the DSN can mint all four.
* **The static KEK.** `StaticKEKProvider` derives its key as
  `sha256(master_secret)` and its own docstring invited "any sufficiently-strong
  passphrase", which is precisely the input a single unsalted SHA-256 does not
  protect.

**The DSN fallback is not an entropy problem, and measuring entropy will not
catch it.** Measured against the shipped default,
`postgresql+asyncpg://terrapod:terrapod@…` scores zxcvbn 4 and ~192 bits — it is
long and structured, so a strength estimator calls it strong. It is weak because
of *where it goes*, not how it looks: a DSN is handed to every client of the
database, sits in env, Helm values, backups and support bundles, and is rotated
on a schedule nothing to do with token forgery. So the fallback is reported by
**identity** — the fact that it is the fallback — and never by score.

For a secret the operator actually chose, a score is meaningful, and zxcvbn does
the job on its own. Measured:

    random 32-byte base64   146 bits      correct-horse-battery-staple   67 bits
    random 16-byte base64    80 bits      MyTerrapodMasterKey2026        62 bits
    random 32-byte hex      207 bits      "changeme"                      9 bits

which is why the bar is 70 bits rather than 128: zxcvbn systematically
*under*-estimates generated key material (16 genuinely random bytes read as 80),
so a 128-bit bar would reject real keys. Everything above the bar is generated
material; everything below is a passphrase somebody typed. A decoded-length
heuristic was tried first and abandoned — `MyTerrapodMasterKey2026` base64-decodes
to 17 bytes with 15 distinct values, which is indistinguishable from a real
16-byte key.

**Rejecting `correct-horse-battery-staple` is the intended behaviour, not a false
positive.** It is a fine login password and a poor key-encryption key: the KEK
protects every encrypted value at rest, offline, for as long as the backups live.
"""

from __future__ import annotations

import math

#: The bar, in estimated bits. See the module docstring for why it is not 128.
MINIMUM_BITS = 70.0

#: Below this length nothing is strong enough to be worth scoring, and zxcvbn is
#: slow enough on long inputs that a cheap reject first is worth having.
MINIMUM_LENGTH = 20


#: zxcvbn RAISES above this length rather than saturating, and a 64-byte key is
#: 88 base64 characters -- so scoring one unguarded took the API down at startup.
#: Truncating is safe in both directions: the prefix of a generated key still
#: scores far above the bar, and the prefix of a repeated string still scores far
#: below it.
_SCORING_MAX_LENGTH = 72


def estimate_bits(secret: str) -> float:
    """Estimated bits of guessing resistance. 0.0 for empty.

    Never raises. This is a diagnostic, and a diagnostic that can crash the
    process is worse than the weakness it reports: an operator holding a
    perfectly good key should not be unable to start the API because the
    estimator disliked its shape.
    """
    if not secret:
        return 0.0
    from zxcvbn import zxcvbn

    try:
        guesses = zxcvbn(secret[:_SCORING_MAX_LENGTH])["guesses"]
    except Exception:  # noqa: BLE001 - see the docstring; never fail closed here
        return math.inf
    return math.log2(guesses) if guesses > 0 else 0.0


def describe_weakness(secret: str, *, name: str) -> str | None:
    """A sentence naming the problem with `secret`, or None if it is fine.

    The message names the setting and says what to do, because it is read by an
    operator in a pod log who did not write this code.
    """
    if not secret:
        return f"{name} is empty"
    if len(secret) < MINIMUM_LENGTH:
        return (
            f"{name} is {len(secret)} characters; at least {MINIMUM_LENGTH} are needed. "
            "Generate one with: openssl rand -base64 32"
        )
    bits = estimate_bits(secret)
    if bits < MINIMUM_BITS:
        return (
            f"{name} looks like a typed passphrase (about {bits:.0f} bits of "
            f"guessing resistance; {MINIMUM_BITS:.0f} are needed). "
            "Generate one with: openssl rand -base64 32"
        )
    return None
