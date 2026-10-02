#!/usr/bin/env python3
"""Every API-token mint names the IdP the token's identity belongs to.

After GHSA-3m8x-ff8g-7x8c a token's roles are resolved by joining on
(provider, email) rather than on email alone, so a token minted without an
`identity_provider` resolves to `everyone` and nothing else. That is the
intended fail-closed behaviour -- but it makes an unattributed mint a silent
downgrade rather than an error, and the failure surfaces far from its cause.

It already did. `scripts/eval-smoke-plan.sh` mints an admin token in the API pod
from a shell heredoc; without the provider that token stopped being an admin, so
the eval boot failed on a 404 for an agent pool it could no longer see -- a
status that named neither the token nor the provider.

This check lives here, rather than beside the `AuthenticatedUser` parity gate in
`services/tests/api/`, for one concrete reason: the pytest tiers run inside
`docker/Dockerfile.test`, which does not ship `scripts/`. A gate there would have
been blind to exactly the two call sites that regressed. The docs-audit job runs
against a full checkout, so it can see the whole repository.

Text rather than AST on purpose: two of these calls sit inside shell heredocs,
so the files holding them are not parseable Python at all -- and those are the
ones that broke.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CALL = "create_api_token("

#: Trees that can hold a mint. `services/tests` is excluded below: a test may
#: legitimately mint whatever shape it is asserting about.
SEARCH = ("services/terrapod", "scripts")

#: A mint this many sites below is a sign the matcher has stopped matching --
#: the router, both OAuth grants and the two smokes are the floor.
MIN_SITES = 5


def _call_text(text: str, ix: int) -> str:
    """The call's source, from its name to the matching close paren.

    Bounded, so an unbalanced heredoc cannot run to the end of the file and
    swallow an unrelated `identity_provider` further down.
    """
    depth = 0
    start = ix + len(CALL)
    for end in range(start, min(len(text), start + 2000)):
        if text[end] == "(":
            depth += 1
        elif text[end] == ")":
            if depth == 0:
                return text[ix : end + 1]
            depth -= 1
    return text[ix : start + 2000]


def _sites() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rel in SEARCH:
        root = ROOT / rel
        if not root.is_dir():
            print(f"FAIL — {rel} does not exist; this check has gone blind.")
            sys.exit(1)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            if "/tests/" in str(path):
                continue
            text = path.read_text(errors="replace")
            ix = text.find(CALL)
            while ix != -1:
                line_start = text.rfind("\n", 0, ix) + 1
                line = text[line_start : text.find("\n", ix)]
                # The definition and import lines are not call sites.
                if not line.lstrip().startswith(("def ", "async def ", "from ", "import ")):
                    loc = f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, ix) + 1}"
                    out.append((loc, _call_text(text, ix)))
                ix = text.find(CALL, ix + 1)
    return out


def main() -> int:
    sites = _sites()
    print(f"  checked: {len(sites)} create_api_token call site(s)")

    if len(sites) < MIN_SITES:
        print(
            f"\nFAIL — only {len(sites)} mint site(s) found, expected at least "
            f"{MIN_SITES}. The matcher has probably stopped matching rather than "
            f"the mints having gone away."
        )
        return 1

    problems = []
    for loc, call in sites:
        if "identity_provider" in call:
            continue
        # A detached token (the HA peer client_credentials grant) owns no
        # identity at all, so there is no provider to name and role resolution
        # never runs for it -- its authority is its `kind`. Exempted on the
        # call's own shape, so a new detached mint is covered and a new bound
        # one in the same file is not.
        if "bound_to=None" in call.replace(" ", ""):
            continue
        problems.append(loc)

    if problems:
        print(
            f"\nFAIL — {len(problems)} mint(s) do not name an identity provider, so "
            f"the token resolves to `everyone` only (GHSA-3m8x-ff8g-7x8c) — an admin "
            f"token that is silently not an admin:\n"
        )
        for p in problems:
            print(f"  {p}")
        return 1

    print("PASS — every bound token mint names its identity provider.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
