# Documentation fixture

The Terraform in this directory exists so the documentation screenshots
(#720) show a real plan of a real-looking estate, rather than a page of
`random_pet` and `null_resource`.

**Keep the `.tf` files free of any comment about what this fixture is for.**
The plan-summary AI reads the configuration as `CODE_CONTEXT`, so a comment
saying "demo fixture for screenshots" ends up narrated back in the summary —
which then goes into a committed image. That is why this explanation lives
here instead of at the top of `main.tf`.

## How it plans offline

Nothing ever leaves the runner. The provider is configured with
`skip_credentials_validation`, `skip_requesting_account_id`,
`skip_metadata_api_check` and `skip_region_validation`, and the seeder supplies
mock credentials through `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. That is
enough for `tofu plan` to expand the genuine provider schema without a cloud
account. Runs are queued with `refresh: false`, since a refresh would try to
read every resource in state.

Credentials come from the environment rather than the `provider` block on
purpose: a hardcoded key there is itself a Checkov finding (`CKV_AWS_41`), and
a demonstration of security scanning should not trip on its own fixture.

## Why the plan looks the way it does

`demo.tfstate` is checked in and deliberately does not match the config, so the
plan produces every shape worth showing — creates, an in-place update, a
replace, and destroys:

| Change | Resource | Cause |
|---|---|---|
| replace | `aws_ecs_service.web` | `launch_type` moves EC2 → FARGATE (immutable) |
| update | `aws_rds_cluster.main` | `backup_retention_period` 7 → 30 |
| destroy | `aws_instance.bastion` | in state, absent from config |
| destroy | `aws_security_group.legacy` | in state, absent from config |
| create | everything else | absent from state |

`aws_security_group.web` allows `0.0.0.0/0` on 443. That is a genuine finding,
not a staged one — it trips both the OPA policy set and the Checkov scan, so
those screenshots show real output.

## Regenerating

```sh
python3 scripts/demo/seed.py --host terrapod.local     # seed + run
node    scripts/demo/capture.mjs --host terrapod.local # capture
```
