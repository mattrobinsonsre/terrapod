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
// Consumers
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
// Version contract
//
// The SDK targets one Terrapod API version per Go module version. A
// build-time-pinned version (see VersionCheck) refuses to talk to a
// Terrapod deployment whose reported version doesn't match exactly,
// unless the caller explicitly opts out. This guards against schema
// drift between the SDK and the server during operator upgrades.
//
// Stability
//
// During the v0.x.y series the public surface may evolve as the
// migration tool's needs surface gaps. Breaking changes are called
// out in the release notes and bumped via the module version. The
// v1.x.y line locks the surface; that comes after a release cycle
// or two of operator feedback.
package terrapod
