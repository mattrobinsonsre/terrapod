package tfe

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"github.com/hashicorp/go-tfe"
)

// RBACTranslation is the operator-facing summary of how a TFE org's
// permission model maps onto Terrapod's. Generated at apply time and
// emitted into the handover doc as a "suggested roles" section.
//
// The translation is *not* applied to Terrapod automatically — RBAC
// is the highest-blast-radius operator decision in a migration and
// every role boundary needs a human signing off. The migrator
// produces the recommendation; the operator reviews, edits, and
// applies via terraform-provider-terrapod (terrapod_role +
// terrapod_role_assignment).
type RBACTranslation struct {
	Teams           []TeamMapping            `json:"teams"`
	UnsupportedFeat []string                 `json:"unsupported_features"`
	WorkspaceTeams  map[string][]TeamMember  `json:"workspace_team_access,omitempty"`
}

// TeamMapping captures one source-side TFE team and the Terrapod
// role we recommend creating in its place.
type TeamMapping struct {
	TFETeamName        string `json:"tfe_team_name"`
	TFETeamID          string `json:"tfe_team_id"`
	SuggestedRoleName  string `json:"suggested_role_name"`
	SuggestedPermLevel string `json:"suggested_permission_level"` // read|plan|write|admin
	MemberCount        int    `json:"member_count"`
	Members            []TeamMember `json:"members,omitempty"`
	Notes              string `json:"notes,omitempty"`
}

// TeamMember is a single user assignment on a TFE team. Surfaced in
// the handover doc so the operator can re-grant via
// terrapod_role_assignment without having to re-query TFE.
type TeamMember struct {
	Email string `json:"email"`
}

// TranslateRBAC reads the source TFE org's team + team-access info
// and returns a recommendation set. Errors are wrapped — the caller
// (apply subcommand) treats a translate failure as advisory: the
// migration continues but the handover doc carries a note that RBAC
// info was incomplete.
func (c *Client) TranslateRBAC(ctx context.Context) (*RBACTranslation, error) {
	out := &RBACTranslation{
		WorkspaceTeams: map[string][]TeamMember{},
	}

	teams, err := c.API.Teams.List(ctx, c.OrgName, &tfe.TeamListOptions{
		Include: []tfe.TeamIncludeOpt{tfe.TeamUsers},
	})
	if err != nil {
		return nil, fmt.Errorf("list TFE teams: %w", err)
	}

	for _, t := range teams.Items {
		mapping := TeamMapping{
			TFETeamName:        t.Name,
			TFETeamID:          t.ID,
			MemberCount:        len(t.Users),
			SuggestedRoleName:  suggestedRoleName(t.Name),
			SuggestedPermLevel: suggestedPermLevel(t),
		}
		for _, u := range t.Users {
			if u.Email != "" {
				mapping.Members = append(mapping.Members, TeamMember{Email: u.Email})
			}
		}
		sort.Slice(mapping.Members, func(i, j int) bool {
			return mapping.Members[i].Email < mapping.Members[j].Email
		})
		if t.OrganizationAccess != nil && (t.OrganizationAccess.ManageWorkspaces ||
			t.OrganizationAccess.ManagePolicies || t.OrganizationAccess.ManageVCSSettings) {
			mapping.Notes = "TFE org-level managers — Terrapod equivalent is the `admin` platform role"
			mapping.SuggestedPermLevel = "admin"
		}
		out.Teams = append(out.Teams, mapping)
	}

	// Terrapod doesn't support: Sentinel policies (deliberately
	// out of scope per docs/migration.md), TFE's "manage modules"
	// (Terrapod modules are RBAC'd via workspace-style labels), or
	// the "secret teams" feature (project-specific).
	out.UnsupportedFeat = []string{
		"Sentinel policies — Terrapod uses run tasks + OPA for policy enforcement",
		"TFE 'manage modules' — Terrapod registry uses label-based RBAC instead",
		"Notification configurations on teams — Terrapod scopes notifications to workspaces",
	}

	sort.Slice(out.Teams, func(i, j int) bool {
		return out.Teams[i].TFETeamName < out.Teams[j].TFETeamName
	})
	return out, nil
}

// suggestedRoleName normalises a TFE team name into a Terrapod role
// name. Terrapod role names follow `^[a-z][a-z0-9-]*$` so we
// lowercase + replace spaces + strip non-conforming bytes.
func suggestedRoleName(tfeName string) string {
	s := strings.ToLower(tfeName)
	var b strings.Builder
	for i, r := range s {
		switch {
		case r >= 'a' && r <= 'z':
			b.WriteRune(r)
		case r >= '0' && r <= '9':
			if i == 0 {
				b.WriteString("t")
			}
			b.WriteRune(r)
		case r == ' ' || r == '_' || r == '/':
			b.WriteByte('-')
		case r == '-':
			b.WriteRune(r)
		default:
			// drop everything else
		}
	}
	out := b.String()
	if out == "" {
		out = "team"
	}
	if out == "admin" || out == "audit" || out == "everyone" {
		// Reserved by Terrapod's built-in roles — prefix to disambig.
		out = "tfe-" + out
	}
	return out
}

// suggestedPermLevel inspects a TFE team's org-access flags and
// returns Terrapod's closest workspace_permission level. The
// mapping is conservative: ambiguous cases pick the lower of two
// candidate permissions.
func suggestedPermLevel(t *tfe.Team) string {
	if t.OrganizationAccess != nil {
		if t.OrganizationAccess.ManageWorkspaces {
			return "admin"
		}
		if t.OrganizationAccess.ManageVCSSettings || t.OrganizationAccess.ManageRunTasks {
			return "write"
		}
	}
	// No org-level signal — workspace-level permissions are the
	// fallback, and even those vary per workspace. "read" is the
	// safest default; the operator escalates per-team in their
	// terraform-provider-terrapod review.
	return "read"
}
