package tfe

import (
	"context"
	"fmt"
	"strings"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// EmitWorkspaces lists every workspace in the configured TFE org and
// emits one ir.Workspace per. The function also gathers the unique
// set of OAuth tokens referenced by VCS-connected workspaces and
// emits one ir.VCSConnection per unique token — the writer creates
// these on the Terrapod side first, then attaches workspaces to them
// by SourceID.
//
// What it doesn't do here:
//   - variable reads (a separate per-workspace pass, increment 4c)
//   - state version downloads (per-workspace, increment 7)
//   - configuration version tarball downloads (non-VCS workspaces, also
//     increment 7)
//
// Keeping this function narrow means a partial migration that fails
// during variable migration still has a complete workspace + VCS-
// connection record set in migration-state.json that a retry can
// resume from.
func (c *Client) EmitWorkspaces(ctx context.Context) ([]ir.Workspace, []ir.VCSConnection, []ir.SkippedItem, error) {
	workspaces, err := c.listAllWorkspaces(ctx)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("list workspaces: %w", err)
	}

	var (
		out      []ir.Workspace
		skipped  []ir.SkippedItem
		seenConn = make(map[string]ir.VCSConnection) // OAuth token id → VCS connection record
	)

	for _, w := range workspaces {
		// TFE's execution modes are "remote", "agent", "local". Terrapod
		// has "local" and "agent" (TFE's "remote" maps to Terrapod
		// "agent" — TFE remote IS server-runs-it; Terrapod agent IS
		// server-runs-it; nomenclature differs).
		execMode := translateExecutionMode(w.ExecutionMode)
		if execMode == "" {
			skipped = append(skipped, ir.SkippedItem{
				Kind:   "tfe-workspace",
				Name:   w.Name,
				Reason: fmt.Sprintf("execution-mode %q is unrecognised; skipped to avoid creating a broken workspace", w.ExecutionMode),
			})
			continue
		}

		// Speculative-only workspaces in TFE don't have runs of their
		// own; they exist for fork-PR speculative plans. Terrapod
		// doesn't model them. Skip.
		if w.SpeculativeEnabled && w.ExecutionMode == "remote" && w.Name == "" {
			// Defensive: an unnamed speculative-only workspace would
			// be unusual but if encountered, skip.
			continue
		}

		// Build IR labels: TFE tags + a few introduced terrapod-migration/
		// labels capturing source-side context that doesn't have a
		// first-class IR field yet.
		//
		// TFE has two "tag" shapes today: the older flat-string tags
		// (Workspace.Tags []*Tag, with Tag.Name carrying "key:value" or
		// just "key") and the newer key/value tag-bindings (HCP
		// projects). Read both and translate to the same map.
		labels := translateTags(tagNames(w.Tags))
		for _, tb := range w.TagBindings {
			if tb == nil || tb.Key == "" {
				continue
			}
			labels[tb.Key] = tb.Value
		}
		if w.AgentPool != nil && w.AgentPool.ID != "" {
			labels["terrapod-migration/tfe-agent-pool-id"] = w.AgentPool.ID
		}
		if w.Project != nil {
			// Projects don't exist in Terrapod; record the project's
			// name as a label so operators can search by it and
			// re-implement project-style scoping with labels.
			labels["terrapod-migration/tfe-project"] = w.Project.Name
		}

		ws := ir.Workspace{
			SourceID:         w.ID,
			Name:             w.Name,
			ExecutionMode:    execMode,
			TerraformVersion: w.TerraformVersion,
			WorkingDirectory: w.WorkingDirectory,
			AutoApply:        w.AutoApply,
			Labels:           labels,
		}

		// VCS link, if any.
		if w.VCSRepo != nil {
			ws.VCSRepoURL = canonicaliseVCSRepoURL(w.VCSRepo)
			ws.VCSBranch = w.VCSRepo.Branch
			if w.VCSRepo.OAuthTokenID != "" {
				ws.VCSConnectionRef = w.VCSRepo.OAuthTokenID
				if _, ok := seenConn[w.VCSRepo.OAuthTokenID]; !ok {
					seenConn[w.VCSRepo.OAuthTokenID] = ir.VCSConnection{
						SourceID:  w.VCSRepo.OAuthTokenID,
						Name:      fmt.Sprintf("tfe-%s", shortID(w.VCSRepo.OAuthTokenID)),
						Provider:  guessProviderFromRepoURL(w.VCSRepo.RepositoryHTTPURL, w.VCSRepo.Identifier),
						ServerURL: deriveServerURL(w.VCSRepo.RepositoryHTTPURL),
					}
				}
			}
		}

		out = append(out, ws)
	}

	// Materialise the VCS-connection map into a deterministic slice so
	// dry-run reports and migration state files diff cleanly across
	// runs. The OAuth token IDs are already opaque strings, so sort
	// them lexically.
	conns := make([]ir.VCSConnection, 0, len(seenConn))
	for _, c := range seenConn {
		conns = append(conns, c)
	}
	sortVCSConnectionsByName(conns)

	return out, conns, skipped, nil
}

// listAllWorkspaces walks every page of GET /organizations/{org}/workspaces.
// go-tfe doesn't auto-paginate; we drive it explicitly so a large org
// with thousands of workspaces still finishes.
func (c *Client) listAllWorkspaces(ctx context.Context) ([]*tfe.Workspace, error) {
	var out []*tfe.Workspace
	page := 1
	for {
		opts := &tfe.WorkspaceListOptions{
			ListOptions: tfe.ListOptions{PageNumber: page, PageSize: 100},
			Include:     []tfe.WSIncludeOpt{tfe.WSCurrentStateVer, tfe.WSProject},
		}
		list, err := c.API.Workspaces.List(ctx, c.OrgName, opts)
		if err != nil {
			return nil, err
		}
		out = append(out, list.Items...)
		if list.NextPage == 0 || page >= list.TotalPages {
			break
		}
		page++
	}
	return out, nil
}

// translateExecutionMode maps TFE's execution-mode values to Terrapod's.
// Returns "" for unrecognised modes — caller surfaces a SkippedItem.
func translateExecutionMode(tfeMode string) string {
	switch tfeMode {
	case "agent", "remote":
		return "agent"
	case "local":
		return "local"
	default:
		return ""
	}
}

// tagNames flattens TFE's []*Tag relation into a plain string slice.
// Nil-safe — a workspace included without `tags` simply yields nil.
func tagNames(tags []*tfe.Tag) []string {
	out := make([]string, 0, len(tags))
	for _, t := range tags {
		if t == nil {
			continue
		}
		out = append(out, t.Name)
	}
	return out
}

// translateTags converts TFE's flat string-tag list to Terrapod's
// key:value label map per the locked convention:
//
//   - "env:prod" → {"env": "prod"}
//   - "production" (no colon) → {"production": ""}
//
// Empty input returns an empty (non-nil) map so the IR's Labels field
// is consistent across migrated workspaces.
func translateTags(tags []string) map[string]string {
	out := make(map[string]string, len(tags))
	for _, t := range tags {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		k, v, hasColon := strings.Cut(t, ":")
		if hasColon {
			out[k] = v
		} else {
			out[k] = ""
		}
	}
	return out
}

// canonicaliseVCSRepoURL picks the operator-recognisable URL form
// from go-tfe's VCSRepo struct. TFE returns both Identifier (e.g.
// "acme/infra") and RepositoryHTTPURL (full URL); operators recognise
// the URL form, so prefer that. Strip trailing ".git" for consistency
// with the Atlantis source's URL normaliser.
func canonicaliseVCSRepoURL(r *tfe.VCSRepo) string {
	if r == nil {
		return ""
	}
	if r.RepositoryHTTPURL != "" {
		return strings.TrimSuffix(strings.TrimSpace(r.RepositoryHTTPURL), ".git")
	}
	// Fallback: synthesise from the github.com/<identifier> form.
	if r.Identifier != "" {
		return "https://github.com/" + r.Identifier
	}
	return ""
}

// guessProviderFromRepoURL picks "github" / "gitlab" from the URL host
// (or the identifier when the URL is empty). The migration tool stores
// the provider name on the Terrapod VCS connection — operators see it
// as the connection's `provider:` attribute. Defaults to "github"
// because that's by far the common case; operators with self-hosted
// gitlab can override post-migration.
func guessProviderFromRepoURL(repoURL, identifier string) string {
	src := repoURL
	if src == "" {
		src = identifier
	}
	low := strings.ToLower(src)
	switch {
	case strings.Contains(low, "gitlab"):
		return "gitlab"
	case strings.Contains(low, "github"):
		return "github"
	default:
		return "github"
	}
}

// deriveServerURL strips the path from a full repo URL to leave just
// scheme + host. Self-hosted GitLab / GHE deployments need a non-default
// server URL on the Terrapod VCS connection; for github.com / gitlab.com
// we leave it empty (the provider's library defaults work).
func deriveServerURL(repoURL string) string {
	if repoURL == "" {
		return ""
	}
	// Look for the second "/" after the scheme. e.g.
	// "https://gitlab.example.com/acme/infra" → "https://gitlab.example.com"
	const schemeSep = "://"
	idx := strings.Index(repoURL, schemeSep)
	if idx < 0 {
		return ""
	}
	rest := repoURL[idx+len(schemeSep):]
	slash := strings.Index(rest, "/")
	if slash < 0 {
		return repoURL
	}
	host := rest[:slash]
	if host == "github.com" || host == "gitlab.com" {
		return ""
	}
	return repoURL[:idx+len(schemeSep)+slash]
}

// shortID produces a stable 8-char excerpt of a long OAuth token ID
// for use in synthesised connection names ("tfe-abcd1234"). The full
// ID is preserved as SourceID for idempotency; the short form is for
// operator-readable display.
func shortID(id string) string {
	id = strings.TrimPrefix(id, "ot-") // TFE OAuth token IDs use "ot-" prefix
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

// sortVCSConnectionsByName sorts the slice in place by SourceID for
// deterministic ordering. Imported into client_test indirectly via
// the EmitWorkspaces test that asserts ordering.
func sortVCSConnectionsByName(conns []ir.VCSConnection) {
	// Simple insertion sort — len(conns) is usually < 20.
	for i := 1; i < len(conns); i++ {
		j := i
		for j > 0 && conns[j-1].SourceID > conns[j].SourceID {
			conns[j-1], conns[j] = conns[j], conns[j-1]
			j--
		}
	}
}
