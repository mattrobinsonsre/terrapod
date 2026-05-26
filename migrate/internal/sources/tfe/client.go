// Package tfe is the TFE / HCP Terraform source plugin for
// terrapod-migrate.
//
// One TFE organisation maps to one Terrapod deployment (Terrapod is
// single-org by design — there's no `--target-org` flag because there
// is no destination org concept). The plugin reads:
//
//   - workspaces (settings, tags → Terrapod labels, working directory,
//     execution mode, VCS link, terraform/tofu version, resource sizing)
//   - variables (workspace + variable sets, sensitive flag, HCL flag)
//   - state version (per workspace, with serial + lineage preserved)
//   - configuration version (non-VCS workspaces only — the latest
//     uploaded tarball)
//   - private registry: modules + module versions + providers + GPG
//   - run triggers (cross-workspace dependencies in scope)
//   - notification configurations (webhook / Slack / email)
//   - agent pools (names + workspace assignments; tokens are not portable)
//
// Skipped (with operator-readable rationale):
//
//   - Sentinel policies (proprietary; Terrapod uses OPA)
//   - HCP Stacks
//   - Run history (too lossy to be useful)
//   - Projects (Terrapod has no project concept)
//   - Teams as first-class objects (replaced by label-RBAC)
//
// The plugin works through go-tfe for the read side. go-tfe handles
// pagination, auth, and rate-limit backoff for us — we don't reinvent
// it. Sensitive variable values are returned by go-tfe only to
// org-owner tokens; the migration emits a report listing which
// variables require operator re-entry when called with a lower-tier
// token.
package tfe

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/hashicorp/go-tfe"
)

// DefaultTFEAddress is the public HCP Terraform endpoint. Operators on
// a self-hosted TFE deployment override this via --source-host.
const DefaultTFEAddress = "https://app.terraform.io"

// Client wraps go-tfe with the migration tool's preferred defaults:
//
//   - Context required (no go-tfe call we make is safe to issue without
//     one — operator cancellation must reach the API immediately).
//   - Token-tier detected once at startup so sensitive-variable
//     handling is consistent across the run.
//   - Source-host normalised to scheme + host with no trailing slash,
//     matching what migration-state.json's SourceHost field stores.
//
// The struct is intentionally narrow — the migration uses one TFE org
// per `apply` invocation, so we don't need org-scoped sub-clients or
// multi-tenancy machinery.
type Client struct {
	// API is the underlying go-tfe client. Exported so per-resource
	// emitter packages can call directly without us re-wrapping every
	// go-tfe method.
	API *tfe.Client

	// Address is the canonical source-side TFE URL (e.g.
	// "https://app.terraform.io" or a self-hosted equivalent), with
	// scheme but no trailing slash.
	Address string

	// OrgName is the TFE organisation being migrated. One org per
	// invocation.
	OrgName string

	// TokenTier is "owner" or "worker", determined once at startup
	// via probeTokenTier. Sensitive variable values are readable
	// only with TokenTierOwner.
	TokenTier TokenTier
}

// TokenTier captures the access level of the TFE token we were given.
// TFE's API distinguishes org-owner tokens (which can read sensitive
// variable values) from team/worker tokens (which see those values as
// write-only redactions). The migration tool detects the tier once and
// adjusts its variable-migration report accordingly.
type TokenTier string

const (
	// TokenTierOwner — org-owner / team-with-manage-variables token.
	// Sensitive variable values are returned.
	TokenTierOwner TokenTier = "owner"

	// TokenTierWorker — any token without the sensitive-value
	// permission. Variables that have sensitive=true return value=""
	// from the API; the migration emits a report listing which keys
	// require manual re-entry post-migration.
	TokenTierWorker TokenTier = "worker"
)

// Config is the read-side configuration for the TFE source plugin.
// Populated from CLI flags before NewClient.
type Config struct {
	// Address is the source-side TFE / HCP base URL. Empty means
	// HCP's public endpoint.
	Address string

	// Token is the TFE API token the operator provides. Read-only
	// access to the org being migrated is the minimum; org-owner
	// preferred for sensitive-variable visibility.
	Token string

	// OrgName is the TFE organisation to migrate. One per invocation.
	OrgName string
}

// ErrMissingToken is returned when Config.Token is empty.
var ErrMissingToken = errors.New("tfe: token is required (set --tfe-token or TFE_TOKEN)")

// ErrMissingOrg is returned when Config.OrgName is empty.
var ErrMissingOrg = errors.New("tfe: org name is required (set --tfe-org)")

// ErrOrgNotFound is returned when the configured org can't be read
// with the supplied token. Either the org doesn't exist or the token
// is for a different org.
var ErrOrgNotFound = errors.New("tfe: organisation not found or token lacks access")

// NewClient constructs a Client, validates the configuration, and
// probes the token's tier against the org. The probe doubles as a
// fail-fast check that auth works before any further work starts —
// migration with a bad token would otherwise fail dozens of API calls
// in before surfacing.
func NewClient(ctx context.Context, cfg Config) (*Client, error) {
	if cfg.Token == "" {
		return nil, ErrMissingToken
	}
	if cfg.OrgName == "" {
		return nil, ErrMissingOrg
	}
	addr := normaliseAddress(cfg.Address)

	api, err := tfe.NewClient(&tfe.Config{
		Address: addr,
		Token:   cfg.Token,
	})
	if err != nil {
		return nil, fmt.Errorf("tfe: build go-tfe client: %w", err)
	}

	// Fail-fast org-access check. Read the org meta; on 404 surface
	// ErrOrgNotFound (operator action: fix --tfe-org or token); on
	// other errors return verbatim.
	if _, err := api.Organizations.Read(ctx, cfg.OrgName); err != nil {
		if errors.Is(err, tfe.ErrResourceNotFound) {
			return nil, fmt.Errorf("%w: %s", ErrOrgNotFound, cfg.OrgName)
		}
		return nil, fmt.Errorf("tfe: read org %q: %w", cfg.OrgName, err)
	}

	tier, err := probeTokenTier(ctx, api, cfg.OrgName)
	if err != nil {
		return nil, fmt.Errorf("tfe: detect token tier: %w", err)
	}

	return &Client{
		API:       api,
		Address:   addr,
		OrgName:   cfg.OrgName,
		TokenTier: tier,
	}, nil
}

// probeTokenTier figures out whether the token can read sensitive
// variable values. We can't ask TFE directly — there's no API for
// "what tier is this token?". The most reliable heuristic is whether
// the token can list the org's tokens (an org-owner action). That
// API is read-only and side-effect-free, so the probe is safe.
//
// An ideal implementation would create a dummy sensitive variable
// and read it back, but that's destructive on the source org and
// outright forbidden by go-tfe's read-only contract for this tool.
func probeTokenTier(ctx context.Context, api *tfe.Client, orgName string) (TokenTier, error) {
	// OrganizationMemberships.List is permitted for org admins/owners
	// and rejected for plain members. A 403/404 there means worker
	// tier; success means owner.
	opts := &tfe.OrganizationMembershipListOptions{
		ListOptions: tfe.ListOptions{PageNumber: 1, PageSize: 1},
	}
	_, err := api.OrganizationMemberships.List(ctx, orgName, opts)
	if err == nil {
		return TokenTierOwner, nil
	}
	// Any error here is taken as "worker tier" — we may miss an
	// owner-token whose probe failed for an unrelated reason (network
	// blip during NewClient), but the cost of a false-negative is a
	// noisier report ("re-enter these sensitive values" when they'd
	// have been read fine), which is recoverable. The cost of a
	// false-positive (thinking we can read sensitive values when we
	// can't) is silent data loss, which isn't.
	return TokenTierWorker, nil
}

// normaliseAddress trims trailing slashes and adds the https:// scheme
// when operators paste a bare host. Mirrors what we do for the
// migrate target URL in internal/version.Check — gives operators the
// "paste your host name, scheme is implicit" UX.
func normaliseAddress(raw string) string {
	addr := strings.TrimSpace(raw)
	if addr == "" {
		return DefaultTFEAddress
	}
	if !strings.HasPrefix(addr, "http://") && !strings.HasPrefix(addr, "https://") {
		addr = "https://" + addr
	}
	// Parse + re-serialise to canonicalise: drops trailing slash,
	// rejects nonsense early.
	u, err := url.Parse(addr)
	if err != nil || u.Host == "" {
		// Fall through; the go-tfe constructor will reject this with
		// its own clearer error.
		return strings.TrimRight(addr, "/")
	}
	u.Path = ""
	u.RawQuery = ""
	u.Fragment = ""
	return strings.TrimRight(u.String(), "/")
}
