# Terrapod smoke tests

Operator-driven end-to-end smoke tests for the v0.27.0 changes:
- `terrapod-migrate` (new tool, atlantis + tfe sources, state migration via S3/GCS/Azure SDKs)
- `terraform-provider-terrapod` (every resource rewritten on top of `go-terrapod`)

These are real-environment smokes, not unit tests — they spin up
docker containers, talk to the live Tilt-managed Terrapod stack, and
hit external object stores. The unit tests under each Go module
cover the per-package behaviour; these scripts catch the integration
gaps unit tests can't see.

## Prereqs

```sh
make dev                 # Tilt-managed Terrapod stack at https://terrapod.local
tofu login terrapod.local   # writes ~/.terraform.d/credentials.tfrc.json
```

Both smokes read the token from the credentials file (or
`$TERRAPOD_TOKEN`).

You also need:
- `docker compose` (for minio in the migrate smoke)
- `tofu` or `terraform` on PATH
- `go` (for building the migrator + provider locally)
- `git` (the migrate smoke seeds a fake clone)

## `smoke-migrate-s3.sh` — terrapod-migrate end-to-end against minio

```sh
scripts/smoke/smoke-migrate-s3.sh
```

What it does:

1. `docker compose up` — boots minio at `127.0.0.1:9100`
2. Generates a one-project Atlantis fixture with an S3-backed terraform module
3. Runs `terraform apply` to seed minio with real state
4. Builds the migrator from current source
5. Runs `terrapod-migrate apply --dry-run` then `--apply`
6. Runs `terrapod-migrate verify` (confirms workspace + state landed on Terrapod)
7. Runs `terrapod-migrate cutover --write-handover` (writes MIGRATION-HANDOVER.md)
8. Re-runs `apply` to confirm state migration is idempotent (must report `state: "unchanged"` for every workspace)

Leaves minio running so you can iterate; tear down with:

```sh
docker compose -f scripts/smoke/docker-compose.yml down -v
```

The minio bucket is wiped on `down -v`. The fixture and state file live in `/tmp/terrapod-smoke-*` — also wiped on reboot.

## `smoke-provider.sh` — terraform-provider-terrapod against Tilt

```sh
PROVIDER_DEV_BUILD=1 scripts/smoke/smoke-provider.sh
```

What it does:

1. Builds the provider from current source and installs into the
   local terraform-provider plugin mirror (skip via
   `PROVIDER_DEV_BUILD=0` if you've already done this)
2. Copies the fixture (`scripts/smoke/provider/main.tf`) into a
   scratch directory
3. `terraform init` → `plan` → `apply`
4. **`terraform plan` again** — the key test: any provider Create /
   Read drift surfaces here as a phantom diff. The smoke fails if
   the post-apply plan isn't empty.
5. `terraform destroy` (always runs, even after failure, via trap)

The fixture exercises:
- `terrapod_workspace` (+ labels)
- `terrapod_variable` (non-sensitive + sensitive)
- `terrapod_variable_set` + `_variable_set_variable` + `_variable_set_workspace`
- `terrapod_agent_pool` + `_agent_pool_token`
- `terrapod_role` + `_role_assignment`
- `terrapod_notification_configuration`
- `terrapod_gpg_key`

Resources NOT in the smoke (out of scope or require external infra):
- `terrapod_vcs_connection` — needs a real GitHub App / GitLab PAT,
  smoke would need credential plumbing we don't want in the repo
- `terrapod_registry_module` / `_provider` — needs tarballs / GPG
  signing infra to be useful
- `terrapod_remote_state_consumer` — only meaningful between two
  workspaces in real use
- `terrapod_run_task` / `_run_trigger` / `_module_workspace_link` /
  `_autodiscovery_rule` / `_user` — covered by the unit tests; would
  inflate the smoke without catching new bug classes

Run `SMOKE_ID=foo` to get a deterministic suffix on every resource
name (default is `smoke-<unix-time>`); useful when iterating because
you can re-run the smoke without colliding with leftover resources.

## Layout

```
scripts/smoke/
├── README.md                  # this file
├── docker-compose.yml         # minio service + bucket-init container
├── seed-fixture.sh            # generates the migrate-smoke atlantis fixture
├── smoke-migrate-s3.sh        # the migrate smoke driver
├── smoke-provider.sh          # the provider smoke driver
└── provider/
    ├── main.tf                # provider-smoke terraform fixture
    └── test-gpg-key.asc       # a throwaway PGP public key for terrapod_gpg_key
```

## What's deliberately not here

- **A docker-atlantis-as-source smoke.** We agreed Atlantis migration
  reads only from local clones; running real Atlantis would only test
  that we can host atlantis in docker. The migrate smoke generates a
  fixture that matches the shape `atlantis.LoadDirectory` expects.

- **A TFE/HCP-as-source smoke.** Authenticating against TFE/HCP needs
  a real account; we can't ship credentials. The TFE source plugin
  has its own httptest-backed unit tests; an operator running a real
  TFE migration can run `terrapod-migrate apply --source tfe
  --dry-run` to validate before committing to `--apply`.

- **A real GitHub / GitLab smoke.** The migrator deliberately does
  not authenticate against either — operators wire up Terrapod VCS
  connections separately, and the migrator just discovers them.
