"""OCI Distribution v2 registry (#1408).

Terrapod serves execution environments and other container images from its own
storage, implementing the distribution spec rather than wrapping a registry
product — the same posture that has Terrapod implement the TFE V2 protocol and
the provider network mirror rather than deploying someone else's server.

The surface lives in the existing API application at ``/v2/`` (the spec mandates
that prefix at the root), so it inherits auth, RBAC, audit, the four-backend
storage abstraction and the pull-through cache machinery.

Modules separate the *grammar* of the spec from the *plumbing*, because the
grammar is what every other part must agree about and is the easiest thing to
get subtly and dangerously wrong:

* :mod:`~terrapod.services.oci.names` — digests, references, repository names.
* :mod:`~terrapod.services.oci.errors` — the spec's error envelope and codes.
"""
