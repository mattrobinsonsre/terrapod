# Post-quantum readiness

Where Terrapod uses cryptography, which of it is already quantum-resistant, which is
ready to become so, and which cannot move yet — with the reason and the mitigation in
each case.

This page exists to be read by someone assessing Terrapod against a post-quantum
policy. It is an inventory, not a roadmap: every row describes what the code does
today.

## Read this first: the two risks are not the same shape

**Confidentiality is retroactively at risk.** Traffic or ciphertext captured today can
be stored and decrypted later, once a capable quantum computer exists — "harvest now,
decrypt later". Nothing about a short lifetime helps: the data was already copied. So
for encryption and key exchange, quantum resistance has to be in place *now* to protect
data that exists *now*.

**Signatures are not retroactive.** A signature cannot be forged before the capability
to forge it exists, and a forgery is only useful against a verifier that is still
willing to accept it. So for signatures, **a short lifetime is a genuine mitigation**,
and moving to a post-quantum algorithm is readiness rather than urgency.

That distinction decides the order of the three sections below, and it is why a 15-minute
RS256 token is a materially different exposure from an RSA-encrypted blob at rest.

## Already post-quantum

Symmetric and hash-based constructions are not broken by a quantum computer. Grover's
algorithm gives a quadratic speedup against a brute-force key search, which halves the
effective bit strength — so a 256-bit symmetric key retains roughly 128-bit security,
and a 256-bit hash retains roughly 128-bit collision resistance. Both remain
comfortably out of reach.

Terrapod's own cryptography is overwhelmingly in this category.

| What | Algorithm | Where |
|---|---|---|
| Encryption at rest — the data path | **AES-256-GCM** per-value envelope | [`crypto/envelope.py`](../services/terrapod/crypto/envelope.py), see [Encryption at rest](encryption-at-rest.md) |
| Encryption at rest — the KEK wrap | symmetric, by provider: a hashed static key, OpenBao/Vault Transit, or AWS KMS | the KEK never leaves the provider for the latter two |
| Platform tokens — runner, run-task callback, download tickets, Slack links | **HMAC-SHA256** | one signing key, generated and persisted by Terrapod |
| Capability URLs | **HMAC-SHA256**, over a BLAKE2b-derived per-deployment key | `auth/capability_urls.py` |
| Webhook signatures | **HMAC-SHA256** (GitHub `X-Hub-Signature-256`); GitLab sends an unsigned token, compared timing-safe | `routers/vcs_events.py` |
| Stored API, session and agent-pool tokens | **SHA-256** hashed at rest; the session token itself is 32 bytes of CSPRNG output, not a signed bearer | `auth/sessions.py` |
| Local passwords | **PBKDF2-HMAC-SHA256**, 100,000 iterations | `auth/passwords.py` |
| Artifact integrity — the pull-through caches | **SHA-256 / SHA-512** digests, verified before an artifact is stored | [Package cache](package-cache.md) |
| Go module integrity | the `h1:` dirhash, which is SHA-256 throughout | `provider_cache_service.py` |

Nothing in this table needs to change, and nothing in it is affected by the sections
below.

## Compatible — hybrid key exchange, with no change on our side

TLS is where the harvest-now-decrypt-later risk actually lands, and it is already
addressable without a Terrapod release.

**Inbound TLS is the operator's.** Terrapod is terminated by your own reverse proxy or
Ingress controller, so the cipher suites and key-exchange groups offered to clients are
configured there. If your policy requires hybrid key exchange, enable `X25519MLKEM768`
(or your proxy's equivalent) at that edge — see [Security hardening → TLS](security-hardening.md#tls-configuration).

**Outbound TLS is already hybrid-capable, because Terrapod pins nothing.** Terrapod
makes outbound connections to VCS providers, package registries, cloud APIs and
OpenBao/Vault. It sets **no** key-exchange group, curve or cipher preference anywhere —
no `set_ecdh_curve`, no `SSL_CTX_set1_groups`, no Go `CurvePreferences` — so each
runtime's own default group list applies, and the current defaults include a hybrid
group:

- the API and listener are **Python 3.14** linked against **OpenSSL 4.0.3**;
- the Go tooling (`go-terrapod`, the providers, `terrapod-migrate`, `terrapod-mcp`) is
  built with **Go 1.26**, and its two `tls.Config` sites set only
  `MinVersion: tls.VersionTLS13` — which hybrid key exchange requires in any case.

Whether a given connection actually negotiates a hybrid group depends on the server at
the other end. The point is that Terrapod does not prevent it, and will pick it up as
upstream servers enable it, with no configuration from you.

## Not post-quantum — why, and what mitigates it

Every row here is a **signature**, which is the non-retroactive risk above. None of them
protects data at rest or in transit.

| What | Algorithm | Why it cannot move yet | Mitigation today |
|---|---|---|---|
| **Listener CA**, the certificates it issues, and the listener proof-of-possession | Ed25519 by default, **ML-DSA selectable** | Nothing external constrains this one — it is ours at both ends, which is why it is the one path that *can* move | **It can be moved today**: set `api.config.agent_pools.ca_algorithm` to `mldsa44`, `mldsa65` or `mldsa87` and a generated CA, the certificates it issues, and their proof-of-possession signatures are all post-quantum. See below. Until then: certificates live **3600s** and renew at half that, and the CA is **node-local and not replicated**, so it has no fleet-wide reach ([HA](high-availability.md)) |
| **Run identity tokens** (the OIDC issuer a cloud federates to) | RS256 | Cloud workload-identity federation does not accept EdDSA, let alone ML-DSA. A federation path that works on two clouds out of three is not worth the elegance | **900s** default lifetime, minted per run, audience-scoped to a single target, and exchanged by the cloud for its own credentials immediately. The signing key rotates as a set, with a publish-before-sign window |
| **GitHub App JWTs** | RS256 | GitHub requires it | **600s** — GitHub's own maximum — exchanged at once for an installation token and never stored |
| **Provider registry signatures** | OpenPGP | The signature must be verifiable by `terraform` / `tofu`, which define the format | It is not the only control. The publisher's `SHA256SUMS` is signed and checked, the engine's lock file pins a per-platform hash, and Terrapod refuses an artifact it cannot verify at admission rather than serving it |
| **SSO assertions** — OIDC id tokens and SAML | the identity provider's choice | Set by your IdP, not by Terrapod | Assertions are short-lived and **replay-protected**: the request id is checked, and each assertion id is cached until its own `NotOnOrAfter`. The resulting Terrapod session is an **opaque random token** looked up server-side, not a signed bearer, so there is no onward signature to forge |
| **Upstream vendors' signing keys** | each vendor's own | We verify against the key the vendor publishes | A vendor moving to a post-quantum key is picked up by registering it; nothing here is pinned to RSA |

## Moving the listener CA to ML-DSA

This is the one algorithm Terrapod can change on its own, because it issues the
certificates and verifies them: no external party has to agree.

```yaml
api:
  config:
    agent_pools:
      ca_algorithm: mldsa65     # or mldsa44 / mldsa87; default is ed25519
```

**It applies when a CA is generated, not when one is loaded.** The algorithm is a
property of the stored key, so an existing deployment keeps signing with the CA it
has — setting this changes nothing until that CA is rotated, and cannot invalidate
certificates already in your fleet.

**What it covers**: the CA key, the listener certificates it issues, and therefore
the proof-of-possession signature on every listener request. A post-quantum CA
issuing classical leaves would leave the signature that actually authenticates a
request classical, which is most of what this is for.

**Sizes, measured.** ML-DSA certificates are large, and that is why
[identifying by fingerprint](runners.md#identifying-by-fingerprint-rather-than-by-certificate)
had to come first — a listener no longer sends its certificate on every request, so
only the signature header scales:

| | certificate (if it were sent) | signature header |
|---|---|---|
| Ed25519 | 584 | 88 |
| ML-DSA-44 | 7,196 | 3,228 |
| ML-DSA-65 | 9,952 | 4,412 |
| ML-DSA-87 | 13,492 | 6,172 |

nginx's default per-header buffer is 8 KB and Node's default total is 16 KiB, so
sending an ML-DSA-65 certificate would exceed the first and ML-DSA-87 with its
signature would exceed the second. Sending the fingerprint instead leaves every size
comfortably inside both.

**RSA and ECDSA are deliberately not offered.** The capability-URL signing key is
derived from the CA private key's raw bytes, an encoding only the Edwards and ML-DSA
key types expose, so admitting one would break a path with nothing to do with
listener certificates.

**Nothing forces this.** Given the mitigations above, the realistic reason to set it
is an organisational post-quantum mandate rather than a present threat.

### Moving an existing deployment: rotate the CA

The setting is read only when a CA is *generated*, so an existing deployment keeps
signing with the CA it has. To move it:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://terrapod.example.com/api/v1/listener-ca/actions/rotate
```

The new CA takes over signing immediately and **the previous one stays trusted for
one listener-certificate lifetime** — exactly as long as anything it issued can still
be live. Listeners pick up a certificate from the new chain on their own renewal
cycle, at half that. **No re-join, no restart, and no window where the fleet is
refused.** `GET /api/v1/listener-ca` shows the trust set and which CA is signing;
`meta.rotation-in-progress` is true while more than one is trusted.

Three things to know before you rotate:

- **Capability URLs minted before the rotation stop verifying once the window
  closes.** The capability signing key is derived from the CA private key, so a
  rotation changes it. They are accepted *during* the window, which is far shorter
  than their own 25-hour lifetime — so a plan or apply log being streamed across a
  rotation may need re-opening. That is the intended trade: matching the capability
  lifetime would keep a rotated-away key usable for a day, and the point of rotating
  is that the old key stops being accepted.
- **It is per node.** The CA is deliberately node-local and not replicated
  ([HA](high-availability.md)), so each node in an HA pair has its own and you rotate
  each one. The two legitimately report different fingerprints.
- **The window closes by the clock**, derived from the new CA certificate's own
  validity start. There is no retirement job to run and nothing to go stale.

## What would change this

Each row above moves when the ecosystem that constrains it does: when a cloud's
workload-identity federation accepts ML-DSA, when the Terraform provider protocol
defines a post-quantum signature format, when an IdP offers one. There is nothing to
configure in Terrapod ahead of those, and the inventory above is maintained so the
question does not have to be re-audited from scratch each time.

## See also

- [Encryption at rest](encryption-at-rest.md) — the optional application-layer envelope encryption
- [Security hardening](security-hardening.md) — TLS, secrets, network policy
- [High availability](high-availability.md) — why the listener CA is node-local
