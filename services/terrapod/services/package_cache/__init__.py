"""Pull-through caching for language package registries (#1417).

Terrapod's promise is that a run needs no upstream reach. A Pulumi program's
`package.json` or `pyproject.toml` belongs to the user and varies per workspace,
so unlike Ansible collections it can never be baked into a runner image —
`npm install` and `pip install` happen at run time and have to reach a registry.
These proxies are that registry.

**One substrate, several ecosystems.** Every language proxy reduces to the same
thing: resolve a name and version to a file, fetch upstream on a miss, store it,
serve it, record it. That lives in :mod:`substrate` and is written once. What
genuinely differs is the *index* — a PEP 503 HTML list of links versus an npm
packument — and those stay as plain per-ecosystem modules rather than being
forced through an interface invented from two examples.

**Verified at admission, and still not re-signed.** Two separate things, and the
distinction is the whole of the trust model.

What we DO: every artifact is checked against the digest its own upstream
published, before it is stored (:func:`substrate.fetch_and_cache`). A mismatch is
refused and nothing is cached. This is the job — validating the objects we hand a
runner (#2589).

What we deliberately do NOT do: re-hash or re-sign. The index rewrite changes the
*URL* and leaves upstream's integrity metadata exactly as published — npm's
`dist.integrity`, PyPI's `#sha256=` fragment — so a client that checks also
checks against the upstream author's own hash rather than against an assertion of
ours. Two independent checks of the same claim; nothing laundered into legitimacy
by our re-signing it.

This paragraph used to say the client's check was the point and ours unnecessary.
That was only half true: `pip install` verifies nothing without `--require-hashes`
or a hash-pinned lock, so in the commonest case in the commonest ecosystem there
was no check anywhere.
"""
