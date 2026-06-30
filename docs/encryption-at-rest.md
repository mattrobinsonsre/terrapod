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

## What's encrypted

The same master switch (`encryption.enabled`) covers two kinds of secret at rest.

**DB-stored secrets** (envelope-encrypted *before* they reach Postgres):

- the **CA private key** (`certificate_authority.ca_key_pem`) — the single most sensitive blob;
- **workspace + variable-set variable values** (`variables.value`, `variable_set_variables.value`) — this also covers catalog inputs, which are stored as workspace variables;
- **VCS connection credentials** (`vcs_connections.token` — GitHub App PEM / GitLab PAT) and the per-connection **webhook secret** (`vcs_connections.webhook_secret`);
- **notification tokens** (`notification_configurations.token`).

**State files** (envelope-encrypted *before* they reach object storage) — state
routinely contains secrets (any attribute a provider persisted in plaintext), so
when encryption is on, new state writes are encrypted too. Because the stored
object is then ciphertext, the runner's state download switches from a presigned
redirect to an **API-proxied decrypt** (the CLI's `cloud`-backend download +
manual upload + rollback already go through the API, so those just decrypt/encrypt
in place). The integrity metadata (`md5`, `sha256`, `serial`, `state_size`) is
always computed over the **plaintext**, so divergence detection and the TFE
protocol are unaffected.

> **State encryption buffers, it doesn't stream.** A state blob is sealed under a
> single AES-GCM tag (so it decrypts whole or fails whole — no chunk-reordering
> footgun), which means the encrypt/decrypt path holds the whole state file in
> memory (off the event loop) instead of streaming it. For the typical few-MB
> state this is a non-issue; if you have very large state *and* a CSP that
> already encrypts the bucket, prefer leaving state encryption to the object
> store. The plaintext-streaming path is unchanged when encryption is off (the
> default). (The plan binary + plan-JSON artifacts are not yet app-encrypted —
> keep object-store SSE for those.)

Encrypted columns are `TEXT` (the one length-bounded column, `webhook_secret`,
was widened `VARCHAR(255)→TEXT` so an envelope can never overflow it). Values
written **before** you enable encryption stay readable — they pass through
untouched until re-written or migrated — so enabling/disabling is a migration,
not a hard cutover. The same is true of state: state versions written before you
enabled encryption stay readable (plaintext blobs pass through), and each new
apply writes an encrypted version. Encrypted columns are never indexed or
unique-constrained (ciphertext is non-deterministic).

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
- **Break-glass DR** — when state encryption is on, the state objects in your
  bucket (and the `state/index.yaml` break-glass index points at them) are
  `TPENC1` ciphertext. Recovering state out-of-band therefore needs the KEK and
  the envelope format, not just the bucket — factor the KEK into your DR runbook
  exactly as you would the database.
- **Disabling** stops new encryption immediately; existing ciphertext stays
  readable as long as the provider + key remain configured. To fully revert,
  run the decrypt migration **before** removing the key (see below).

## Encrypt existing data / revert (resumable migration)

Enabling encryption only encrypts **new** writes; rows written earlier stay
plaintext until re-written. To encrypt everything now — or to decrypt back before
disabling — run the resumable migration. It is **verify-readback per row** (a row
is only overwritten once the new value is proven decryptable), resumable, and
idempotent:

```bash
# encrypt all existing secrets under the active DEK (run with encryption enabled)
kubectl exec deploy/terrapod-api -- python -m terrapod.cli.encryption_migrate encrypt

# decrypt everything back to plaintext BEFORE disabling / removing the key
kubectl exec deploy/terrapod-api -- python -m terrapod.cli.encryption_migrate decrypt
```

`encrypt` skips rows already at the active DEK version, so it's safe to re-run
after an interruption. **Run `decrypt` while the key is still available** — once
the key is gone, encrypted rows can't be converted back.

## Key rotation

- **DEK rotation** — mint a new active data-encryption key via
  `POST /api/terrapod/v1/admin/encryption/rotate-dek` (admin). Prior DEK versions
  are **retained** so existing ciphertext stays decryptable; new writes use the
  new key. To re-encrypt old rows under the new key, run
  `encryption_migrate encrypt` afterwards. The new key is wrapped **and unwrapped
  (round-trip verified)** before it's activated — a broken provider aborts with
  nothing changed.
  - **Multi-replica propagation** — the new DEK is minted on whichever replica
    served the request; the others pick it up automatically within ~30s via the
    `encryption_key_refresh` background task (no leader election, no restart). In
    that short window a replica that hasn't refreshed yet may transiently fail to
    read data just written under the new key. It's harmless (fail-loud, no data
    loss) but if you want zero-window propagation, do a rolling restart after
    rotating, or rotate during low traffic.
- **KEK rotation** — for `awskms` / `vault_transit`, the provider manages its own
  key versions transparently; Terrapod's wrapped DEKs keep working across the
  CSP/Vault key rotation as long as the old version remains decryptable. For
  `static`, rotating the master key means re-wrapping the DEKs under the new key
  — do this as a deliberate, backed-up operation (and verify with the
  [encryption doctor](#monitoring-decryptability--dont-get-surprised) after).

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
