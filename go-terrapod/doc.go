// Package terrapod is the Go SDK for the Terrapod API.
//
// Terrapod is a free, open-source platform replacement for Terraform
// Enterprise. This SDK gives Go programs strongly-typed access to the
// resources Terrapod manages — workspaces, variables, variable sets,
// state versions, configuration versions, the private module +
// provider registry, run triggers, notifications, agent pools, VCS
// connections, audit log, RBAC roles + assignments, policy sets (OPA),
// and remote-state consumer allowlists.
//
// # Consumers
//
// The SDK is used by:
//
//   - The terraform-provider-terrapod (HCL-as-code management of
//     Terrapod resources).
//   - terrapod-migrate (the TFE/Atlantis → Terrapod migration tool).
//   - Third-party automation (importable as
//     github.com/mattrobinsonsre/terrapod/go-terrapod; same shape as
//     hashicorp/go-tfe).
//
// All three landed in v0.27.0 as part of one release.
//
// # Version contract
//
// The SDK targets one Terrapod API version per Go module version.
// VersionCheck compares a build-time-pinned version against the version the
// deployment reports, so a caller can warn about schema drift during an
// operator upgrade.
//
// It is a COMPATIBILITY hint and nothing more — do not read it as an
// authenticity or anti-downgrade signal (GHSA-5fh8-vj57-6gvh). It fails open
// by design, and that design is right: an unparseable or "dev" server version
// returns a nil error rather than blocking the call, because refusing to work
// against a deployment we merely failed to parse would be worse than
// proceeding. The client-side default SDKVersion is "dev", which skips the
// check entirely. Every input it reasons about is supplied by the server it is
// asking, so a hostile one simply reports whatever passes.
//
// This paragraph previously said the check "refuses to talk to" a mismatched
// deployment unless the caller opts out. It does not refuse anything; it
// returns an error the caller may ignore, and every consumer in this repo
// calls it warn-only.
//
// # Stability
//
// During the v0.x.y series the public surface may evolve as the
// migration tool's needs surface gaps. Breaking changes are called
// out in the release notes and bumped via the module version. The
// v1.x.y line locks the surface; that comes after a release cycle
// or two of operator feedback.
package terrapod
