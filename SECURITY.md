# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Previous minor | Security fixes only |

We recommend always running the latest release.

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

To report a security vulnerability, please use GitHub's private vulnerability
reporting:

1. Go to the [Security Advisories page](https://github.com/mattrobinsonsre/terrapod/security/advisories/new)
2. Click **"Report a vulnerability"**
3. Fill in the description, steps to reproduce, and affected versions

This ensures the vulnerability is disclosed privately to maintainers and can be
fixed before public disclosure. You will receive updates on the advisory as it
is triaged and resolved.

If you are unable to use GitHub's reporting, email the maintainer directly.

## Security Design

### Key Security Properties

- **Encryption at rest via CSP services**: Sensitive variables and VCS tokens
  are stored in PostgreSQL and protected by database encryption-at-rest (e.g.
  RDS encryption, Cloud SQL encryption, Azure Database encryption). State files
  are stored as-is in object storage and protected by object store
  encryption-at-rest (S3 SSE, Azure Storage encryption, GCS default encryption).
  For filesystem-backed storage, enable volume encryption at the infrastructure
  level.
- **Session-based auth**: Web sessions are server-side (Redis) with 12-hour
  sliding TTL. Revoking a session takes effect instantly.
- **API tokens**: Long-lived tokens for CLI and automation are SHA-256 hashed
  at rest. Only the raw token value is returned once at creation time.
  Configurable max TTL via `auth.api_token_max_ttl_hours`.
- **Label-based RBAC**: Hierarchical workspace permissions (read/plan/write/admin)
  with label-based access control. No teams — labels replace teams entirely.
- **MFA delegated to IdP**: Terrapod never implements MFA directly. Your
  identity provider (Auth0, Okta, Azure AD) handles MFA enforcement.
- **RFC3339 timestamps**: All datetimes are timezone-aware UTC, serialized with
  trailing `Z`. No naive datetimes anywhere in the codebase.
- **Multi-replica safe**: No leader election. All background tasks coordinate
  via Redis-based distributed scheduler with mutual exclusion.
- **Certificate-based runner auth**: Runner listeners authenticate via
  Ed25519 certificates issued by the built-in CA after joining a pool with a token.

### Dependency Security

- `pip-audit` (Python) is available for dependency vulnerability scanning.
- Container images are scanned with Trivy for HIGH/CRITICAL CVEs.
- Static analysis via Semgrep with OWASP Top 10 and custom project rules.
- Dynamic application security testing via Nuclei with custom templates.

## Security Testing

Terrapod includes a three-layer security testing framework:

| Layer | Tool | What it covers |
|-------|------|----------------|
| SAST | Semgrep | Source code analysis, OWASP Top 10, secrets detection, project-specific rules |
| Container scanning | Trivy | CVEs in Docker images (HIGH/CRITICAL) |
| DAST | Nuclei | Auth bypass, header injection, CORS, state endpoint security |

Run with:

```zsh
make pentest-sast     # Static analysis
make pentest-images   # Container image CVE scan
make pentest-dast     # Dynamic testing (requires running stack)
make pentest          # All three layers
```

## Security-Related Configuration

See the [Security Hardening Guide](docs/security-hardening.md) for production
hardening, including:

- TLS configuration (database, Redis, ingress)
- Authentication hardening (disable local auth, token TTL, SSO enforcement)
- Secrets management (Kubernetes Secrets, External Secrets Operator)
- Network policies (pod-to-pod traffic restriction)
- Pod Security Standards (namespace-level enforcement)
- Rate limiting (API and auth endpoint protection)
- Audit log retention and SIEM export
- Database and object storage hardening
- Runner isolation (dedicated nodes, security contexts)
- Backup strategy

See also the [Deployment Guide](docs/deployment.md) for general production
deployment guidance.
