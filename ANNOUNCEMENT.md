Terrapod is a self-hosted, open-source replacement for Terraform Enterprise. It provides the collaboration, governance, and UI layer that wraps around terraform or tofu — it doesn't fork or reimplement either engine, it orchestrates them.

The motivation is straightforward. HashiCorp moved from per-user pricing to Resources Under Management (RUM) pricing — $0.10 to $0.99 per managed resource per month. For teams with large infrastructure footprints, costs became difficult to predict. Terrapod exists because managing Terraform at scale shouldn't require a second mortgage.

What it covers:

- Workspaces with remote state management, locking, and Fernet encryption at rest
- Remote execution via K8s Jobs (ARC pattern — ephemeral runners, not persistent agents)
- VCS integration (GitHub App, GitLab) with polling-first design — no inbound webhook dependency
- Label-based RBAC with hierarchical workspace permissions
- Private module and provider registry with pull-through caching
- SSO via OIDC and SAML (Auth0, Okta, Azure AD, etc.)
- Drift detection — scheduled plan-only runs to catch out-of-band changes
- Run triggers for cross-workspace dependency chains
- Run tasks for pre/post-plan validation webhooks
- Notifications (webhook, Slack, email) on run lifecycle events
- Audit logging with configurable retention
- Dynamic cloud credentials via K8s workload identity (AWS IRSA, GCP WIF, Azure WI)
- TFE V2 API compatibility — terraform CLI, tofu CLI, and go-tfe work against it

Built with Python (FastAPI), Next.js, PostgreSQL, Redis, and native cloud object storage (S3, Azure Blob, GCS). Deployed via Helm chart on Kubernetes.

Current status is alpha. Early feedback would be appreciated.

Licensed under GPLv3.

https://github.com/mattrobinsonsre/terrapod
