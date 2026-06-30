# Encryption at rest (application-layer, optional)

> **Most deployments do not need this.** If your platform encrypts data at rest
> natively — RDS / Cloud SQL / Azure Database encryption for Postgres, and S3
> SSE / Azure Storage / GCS default encryption for the object store — that is the
> recommended baseline and Terrapod relies on it by default. Turning on
> application-layer encryption *on top* of that is **belt-and-braces**.
>
> **Where it earns its keep:** deployments **without a usable CSP at-rest
> switch** — bare-metal, on-prem, a niche cloud that doesn't offer it, or
> air-gapped — where you can't just tick a box in a console. There, this is your
> path to encryption at rest at all. It's deliberately for the fussy and the
> fringe; it is **off by default**.

When enabled, sensitive values are envelope-encrypted **before** they hit
Postgres, so a leaked database dump exposes ciphertext, not plaintext. The key
that protects them is held by a key manager you choose — "we hold the key, not
just the platform."

## What's encrypted (Phase 1)

Application-layer encryption currently covers **DB-stored secrets**, starting
with the **CA private key** (the single most sensitive blob). The remaining
Phase-1 columns — sensitive variables, variable-set values, catalog inputs, VCS
tokens/keys, webhook secrets — land in a follow-up. **State files are out of
scope for now** (they need a decrypting download proxy and chunked streaming;
keep them protected by object-store at-rest encryption).

The DB columns stay `TEXT`; nothing is re-typed at the database level. Values
written before you enable encryption stay readable (they're passed through
untouched until a future migration re-encrypts them), so enabling/disabling is a
migration, not a hard cutover.

## How it works — envelope encryption

- A random **DEK** (data-encryption key) encrypts the data with AES-256-GCM.
- A **KEK** (key-encryption key), held by your chosen provider, *wraps* the DEK.
  The KEK never reaches the data path: the DEK is unwrapped **once at startup**
  and cached in memory, so per-row crypto is local and fast.
- Every encrypted value is self-describing (`tpenc:1:<dek_version>:…`) so DEK
  rotation and mixed state during a migration are well-defined.
- A **decryptability canary** (a known value encrypted at enable time) is checked
  on every boot. If the key is wrong or missing, the API **fails to start and
  refuses writes** — it never stores data it can't read back.

## Choosing a KEK provider

| Provider | Key custody | Best for |
|---|---|---|
| `vault_transit` | HashiCorp Vault Transit (key never leaves Vault) | **on-prem / multi-cloud / air-gapped** — the CSP-agnostic KMS, and the recommended choice when you have no cloud KMS |
| `static` | an operator-held master key (K8s Secret) | bare-metal / dev / the universal fallback when there is **no** key manager at all |
| `awskms` | AWS KMS | deployments already on AWS (belt-and-braces) |

> **`static` is a footgun — read this.** With `static`, **you** own key
> durability. If you lose the master key, **all encrypted data is
> unrecoverable** — there is no recovery path. Back it up out-of-band, in more
> than one place, under multi-person control, *before* you enable encryption.
> Prefer `vault_transit` (or a cloud KMS) whenever you have one, because they
> own durability, rotation, and audit for you.

## Enabling it

The KEK secret (the `static` master key and/or the Vault token) is injected via
a K8s Secret — never a ConfigMap. Create it first:

```bash
# static: a strong master key (passphrase or base64 — it's hashed to a 256-bit KEK)
kubectl -n terrapod create secret generic terrapod-encryption \
  --from-literal=static_key="$(openssl rand -base64 48)"

# vault_transit: a Vault token with encrypt/decrypt on the transit key
kubectl -n terrapod create secret generic terrapod-encryption \
  --from-literal=vault_token="$VAULT_TOKEN"
```

Then opt in via Helm values:

```yaml
api:
  config:
    encryption:
      enabled: true
      provider: vault_transit          # or: static | awskms
      existingSecret: terrapod-encryption
      # vault_transit:
      vault_address: "https://vault.internal:8200"
      vault_mount: transit
      vault_key_name: terrapod
      # awskms:
      # provider: awskms
      # aws_kms_key_id: "arn:aws:kms:…:key/…"   # auth via the API pod's IRSA
```

On first enable the API mints DEK v1, wraps it with your KEK, stores it (wrapped)
in the `crypto_keys` table, and writes the canary. From then on, new writes to
covered columns are encrypted; the API refuses to start if the KEK can't unwrap
the DEK (canary fail-closed).

## Operational notes

- **Back up the KEK first** (`static` especially) — see the footgun box above.
- The DB backup CronJob and your object store are unaffected: encrypted columns
  are ciphertext in the dump, which is the point.
- **Disabling** stops new encryption immediately; existing ciphertext stays
  readable as long as the provider + key remain configured (a decrypt-everything
  migration to fully revert lands with the Phase-2 breadth work).
- Rotation (re-wrap DEKs under a new KEK; roll the DEK) and a resumable
  encrypt-existing-data pass are Phase-2 follow-ups.

## Monitoring decryptability — don't get surprised

Losing decryptability is data loss, so treat "can we still decrypt?" as a
first-class health signal, the same way you'd treat a backup you've never tested.

- **Status endpoint** (admin): `GET /api/terrapod/v1/admin/encryption` returns
  `{enabled, provider, active_version, dek_versions, canary_ok, decryptable}`.
  `decryptable: false` means the platform is running but can't read the canary
  back — page on it.
- **Encryption doctor** — an on-demand drill that independently re-builds the
  KEK provider and re-unwraps **every** DEK version live (catching a KMS
  permission revoked, a Vault key deleted, or a static key rotated away *after*
  startup) and verifies each canary. Exit code is non-zero on any failure:

  ```bash
  kubectl exec deploy/terrapod-api -- python -m terrapod.cli.encryption_doctor
  ```

  Run it after any change to the KEK/IAM/Vault policy, and consider scheduling it
  (a CronJob using the same image + entrypoint) so a broken key path is caught
  *before* an outage forces a restart that then can't decrypt.

## Recovery

There is **no recovery without the KEK** — that is the whole point of the
feature and the whole danger. Plan for it:

1. **Before enabling**, back up the KEK out-of-band:
   - `static` — copy the master secret to a separate secret manager / offline
     vault, under multi-person control. This is the only copy that matters.
   - `vault_transit` / `awskms` — ensure the Vault key / KMS key has the
     provider's own durable backup + a deletion-protection / key-recovery window
     enabled, and that the Terrapod identity retains decrypt permission.
2. **If the doctor or status reports `decryptable: false`:**
   - Do **not** delete or rotate anything. First restore the *exact* prior key
     material / permissions (re-grant KMS decrypt, undelete the Vault key,
     restore the `static` secret from your out-of-band copy).
   - The API fails closed on a bad key, so a misconfigured restart won't corrupt
     data — fix the key, then restart; the boot canary confirms recovery.
3. **Never** change `encryption.provider` or the key on a deployment that already
   has encrypted rows without first proving the new path can unwrap the existing
   DEKs — otherwise existing ciphertext becomes unreadable. (Safe KEK/DEK
   rotation that re-wraps existing DEKs is a separate, guarded operation.)

See the [encryption key recovery runbook](runbooks.md#encryption-key-lost-or-unusable-decryptable-false).

## See also

- [Cloud Credentials](cloud-credentials.md) — the at-rest encryption baseline this sits on top of.
- [Security Hardening](security-hardening.md) — the broader posture.
- [Supply-chain verification](supply-chain-verification.md) — signed releases + verified dependencies.
