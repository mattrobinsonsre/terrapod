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

**Not a trust boundary, deliberately.** Nothing here re-hashes or re-signs. The
index rewrite changes the *URL* and leaves upstream's integrity metadata exactly
as published — npm's `dist.integrity`, PyPI's `#sha256=` fragment — so the client
verifies our bytes against the hash the upstream author published. A corrupted or
substituted artifact fails at the client, where it should, rather than being
laundered into legitimacy by our re-signing it.
"""
