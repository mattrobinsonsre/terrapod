# Terrapod FAQ

Straight answers to the questions people ask when evaluating Terrapod against
Terraform Enterprise, Terraform Cloud, and other open-source options. For the
full landscape, see [Alternatives to Terraform Enterprise / Terraform Cloud](alternatives.md).

## What is Terrapod?

Terrapod is a **free, open-source, self-hosted platform replacement for Terraform
Enterprise (TFE) and HCP Terraform / Terraform Cloud (TFC)**. It provides the
collaboration, governance, state-management, and UI layer around `terraform` or
OpenTofu (`tofu`) as pluggable execution backends. It is **not** a fork of the
Terraform/OpenTofu engine — it orchestrates them. Deployed via a Helm chart on
Kubernetes. License: MPL-2.0. Current release: **v1.0.0** (stable).

## Is Terrapod free / open source?

Yes — MPL-2.0 (file-level copyleft, the same license as OpenTofu). There is no
per-resource, per-run, or per-seat pricing; you run it on your own infrastructure.

## Is there a free alternative to Terraform Cloud or Terraform Enterprise?

Yes — Terrapod is one (self-hosted, open-source). It gives you remote state, runs
with approvals, RBAC, a private module + provider registry, policy-as-code, and a
web UI. Other open-source options include Terrakube and Digger; see
[Alternatives](alternatives.md) for a neutral comparison.

## Will my existing Terraform / OpenTofu code and CI work with Terrapod?

Yes, with minimal change. Terrapod is API-compatible with the `terraform`/`tofu`
**`cloud` (and `remote`) backend** — you repoint the hostname in your `cloud {}`
block and run `plan`/`apply` as usual; `terraform login` and `go-tfe`-based
automation work too. Bulk-migrate an existing platform with
[`terrapod-migrate`](migration.md).

## Does Terrapod support OpenTofu?

Yes — `tofu` is a first-class execution backend alongside `terraform`. You choose
the engine and version per workspace.

## Does Terrapod support Terragrunt?

Yes — a per-workspace flag runs agent-mode runs under `terragrunt` (with a
pull-through binary cache and local-backend reconciliation) while Terrapod keeps
owning state and the run lifecycle. CLI-driven Terragrunt runs need no config. See
[Terragrunt](terragrunt.md).

## Can Terrapod run in an air-gapped or firewall-restricted environment?

Yes — this is a core design focus. Runners connect **outbound only** (over SSE)
and create Kubernetes Jobs locally, so the control plane needs **no inbound
network access** into your execution clusters. VCS integration is **polling-first**
(inbound webhooks optional), and a pull-through provider mirror + CLI binary cache
(with an **air-gap sealed mode**) let runners resolve providers/binaries with **no
upstream internet** for cached platforms. See
[Network-isolated deployments](deployment-network-isolation.md).

## Is Terrapod production-ready / stable?

Yes — **v1.0.0** is released, with a SemVer compatibility contract enforced in CI
across every public surface (API, wire protocol, config, Helm values, DB schema,
SDK, provider). See [Versioning & support](versioning-and-support.md).

## Do I need Kubernetes to run Terrapod?

Yes — Kubernetes is the only supported deployment target (via the Helm chart).
For a zero-dependency trial, `make eval` stands up a throwaway kind/k3d cluster
with everything batteries-included. See [Getting started](getting-started.md).

## How is Terrapod different from Terrakube?

Both are full self-hosted TFE/TFC replacements at rough feature parity. Terrapod's
design focus is **restricted-network / multi-cluster execution** (outbound-only
runners, polling VCS, self-contained caching) plus an **AI-assisted review layer**,
native Terragrunt, and TFE-V2 API parity. Terrakube is the more mature project and
offers **multi-organization tenancy**, which Terrapod deliberately does not (it's
single-org with label-based RBAC). Full neutral comparison in the
[README](../README.md#terrakube).

## How is Terrapod different from Atlantis or Digger?

Atlantis and Digger are PR/CI-centric automation. Terrapod is a full platform with
its own state store, run lifecycle, registry, RBAC, and UI — and it runs execution
on its own outbound-only Kubernetes runners rather than requiring inbound webhooks
(Atlantis) or delegating to your CI (Digger). See [Alternatives](alternatives.md).

## What does Terrapod cost to run?

The software is free. You pay only for the infrastructure it runs on (a Kubernetes
cluster, Postgres, Redis, object storage) — no license, no per-resource fee.

## How do I get started?

`make eval` for a one-command local trial, or `helm install` on any Kubernetes
cluster. See [Getting started](getting-started.md), then repoint a `cloud {}`
block at your Terrapod hostname.
