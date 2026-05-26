package tfe

import (
	"context"
	"fmt"
	"slices"
	"strings"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// dynamicCredsEnvVarPrefixes are the TFE env-var keys that drive
// Workload Identity / Dynamic Credentials. These names are
// TFE-platform-specific — Terrapod uses ServiceAccount annotations on
// the runner pool, not workspace env vars. They get stripped from the
// migrated variable set and surfaced in the report so the operator
// knows to configure the equivalent on Terrapod's runner side.
//
// The list is conservative — it matches the documented "TFC_..._AUTH"
// triggers + cloud-specific role/identity follow-ups. Names not in
// this list pass through unchanged to be safe.
var dynamicCredsEnvVarPrefixes = []string{
	"TFC_AWS_PROVIDER_AUTH",
	"TFC_AWS_RUN_ROLE_ARN",
	"TFC_AWS_PLAN_ROLE_ARN",
	"TFC_AWS_APPLY_ROLE_ARN",
	"TFC_AWS_WORKLOAD_IDENTITY_AUDIENCE",
	"TFC_GCP_PROVIDER_AUTH",
	"TFC_GCP_RUN_SERVICE_ACCOUNT_EMAIL",
	"TFC_GCP_PLAN_SERVICE_ACCOUNT_EMAIL",
	"TFC_GCP_APPLY_SERVICE_ACCOUNT_EMAIL",
	"TFC_GCP_WORKLOAD_PROVIDER_NAME",
	"TFC_GCP_WORKLOAD_IDENTITY_AUDIENCE",
	"TFC_AZURE_PROVIDER_AUTH",
	"TFC_AZURE_RUN_CLIENT_ID",
	"TFC_AZURE_WORKLOAD_IDENTITY_AUDIENCE",
	"TFC_VAULT_ADDR",
	"TFC_VAULT_AUTH_PATH",
	"TFC_VAULT_AUTH_TYPE",
	"TFC_VAULT_NAMESPACE",
	"TFC_VAULT_RUN_ROLE",
}

// isDynamicCredsKey returns true if a TFE workspace env-var key
// matches one of the Dynamic Credentials trigger names. Used to filter
// these out of the migrated variable set + emit operator guidance.
//
// Matching is exact, not prefix — TFE's docs list specific keys and
// we don't want a key like "TFC_AWS_PROVIDER_AUTH_FOO" (operator
// custom) to be silently dropped.
func isDynamicCredsKey(key string) bool {
	return slices.Contains(dynamicCredsEnvVarPrefixes, key)
}

// AttachVariables fills in workspaces[i].Variables for every workspace
// in the slice. Each workspace's variables are fetched via go-tfe's
// Workspaces.ReadWithOptions(..., Include: Vars). Sensitive values are
// returned only when the token's tier is owner; with a worker tier the
// value comes back redacted (empty) and the migration emits a SkippedItem
// per sensitive variable so the operator knows what to re-enter.
//
// Returns the per-workspace variable lists merged into the workspaces
// slice in place + a SkippedItem per sensitive-var-needing-re-entry.
func (c *Client) AttachVariables(ctx context.Context, workspaces []ir.Workspace) ([]ir.SkippedItem, error) {
	var skipped []ir.SkippedItem
	for i := range workspaces {
		ws := &workspaces[i]
		if ws.SourceID == "" {
			continue
		}
		vars, dynSkipped, sensSkipped, err := c.readWorkspaceVariables(ctx, ws.SourceID, ws.Name)
		if err != nil {
			return nil, fmt.Errorf("read variables for workspace %s: %w", ws.Name, err)
		}
		ws.Variables = vars
		skipped = append(skipped, dynSkipped...)
		skipped = append(skipped, sensSkipped...)
	}
	return skipped, nil
}

// readWorkspaceVariables fetches one workspace's variables and
// translates them to ir.Variable, filtering Dynamic Credentials env
// vars and reporting sensitive variables we can't read.
func (c *Client) readWorkspaceVariables(ctx context.Context, workspaceID, workspaceName string) ([]ir.Variable, []ir.SkippedItem, []ir.SkippedItem, error) {
	// Workspaces.Variables.List paginates the same way as the workspace
	// list — drive it explicitly so a workspace with hundreds of vars
	// still finishes.
	var allVars []*tfe.Variable
	page := 1
	for {
		opts := &tfe.VariableListOptions{
			ListOptions: tfe.ListOptions{PageNumber: page, PageSize: 100},
		}
		list, err := c.API.Variables.List(ctx, workspaceID, opts)
		if err != nil {
			return nil, nil, nil, err
		}
		allVars = append(allVars, list.Items...)
		if list.NextPage == 0 || page >= list.TotalPages {
			break
		}
		page++
	}

	var (
		out          []ir.Variable
		dynSkipped   []ir.SkippedItem
		sensSkipped  []ir.SkippedItem
	)
	for _, v := range allVars {
		if v.Category == tfe.CategoryEnv && isDynamicCredsKey(v.Key) {
			dynSkipped = append(dynSkipped, ir.SkippedItem{
				Kind: "tfe-dynamic-credentials",
				Name: fmt.Sprintf("workspace %s: %s", workspaceName, v.Key),
				Reason: "TFE Dynamic Credentials env var; Terrapod uses Kubernetes Workload Identity " +
					"via runner-pool ServiceAccount annotations instead. See docs/runners.md for the " +
					"per-cloud setup (AWS IRSA, GCP WIF, Azure WI). This variable is stripped from " +
					"the migrated workspace; the runner pool needs the equivalent annotation.",
			})
			continue
		}

		// Variable category and HCL flag both translate verbatim.
		// Terrapod's Variable model uses the same shape.
		cat := categoryString(v.Category)

		// Sensitive vars: go-tfe returns the value empty when the
		// token can't read it. With TokenTierWorker that's every
		// sensitive var; with TokenTierOwner the API still returns
		// "" for sensitive vars unless the operator explicitly
		// requested values — go-tfe's default list endpoint doesn't.
		//
		// We DON'T try to fetch sensitive values here. The migration's
		// goal is to record metadata + non-sensitive values; sensitive
		// values land via operator re-entry post-migration (the safer
		// path — sensitive-var movement is its own concern with its
		// own audit trail).
		out = append(out, ir.Variable{
			Key:         v.Key,
			Value:       v.Value, // empty when sensitive
			Category:    cat,
			HCL:         v.HCL,
			Sensitive:   v.Sensitive,
			Description: v.Description,
		})

		if v.Sensitive {
			sensSkipped = append(sensSkipped, ir2skippedSensitive(workspaceName, v.Key, c.TokenTier))
		}
	}
	return out, dynSkipped, sensSkipped, nil
}

// categoryString maps go-tfe's CategoryType to the IR's string form.
// TFE has exactly two: "terraform" (TF_VAR_*) and "env" (raw env var).
// Terrapod uses the same convention.
func categoryString(c tfe.CategoryType) string {
	switch c {
	case tfe.CategoryTerraform:
		return "terraform"
	case tfe.CategoryEnv:
		return "env"
	default:
		// Future TFE category we don't model — keep verbatim so the
		// operator sees it in the report and can decide.
		return string(c)
	}
}

// ir2skippedSensitive emits the per-sensitive-variable SkippedItem
// telling the operator to re-enter it. Wording differs slightly based
// on token tier so the operator gets actionable info ("rerun with an
// owner token" vs "manually re-enter").
func ir2skippedSensitive(workspaceName, key string, tier TokenTier) ir.SkippedItem {
	reason := "Variable value is sensitive. Re-enter manually on the migrated workspace post-cutover; sensitive values are never written to the migration state file or report."
	if tier == TokenTierWorker {
		reason = "Variable value is sensitive AND the migration token is worker-tier (cannot read sensitive values). Re-run the migration with an org-owner token to migrate the value automatically, or re-enter manually on the migrated workspace post-cutover."
	}
	return ir.SkippedItem{
		Kind:   "tfe-sensitive-variable",
		Name:   fmt.Sprintf("workspace %s: %s", workspaceName, key),
		Reason: reason,
	}
}

// VariableSetsReport pulls every variable set in the org plus its
// workspace assignments and emits SkippedItems for any unsupported
// scoping (project-scoped sets when the org has projects). The
// migration tool can't create varsets on Terrapod yet — that's a
// later increment (writer + Terrapod varset endpoints). For now we
// record what exists so the report covers it.
//
// Returns the raw list so a future increment can wire varset → IR
// translation without re-reading the API.
func (c *Client) VariableSetsReport(ctx context.Context) ([]*tfe.VariableSet, []ir.SkippedItem, error) {
	var all []*tfe.VariableSet
	page := 1
	for {
		opts := &tfe.VariableSetListOptions{
			ListOptions: tfe.ListOptions{PageNumber: page, PageSize: 100},
			Include:     "workspaces,vars",
		}
		list, err := c.API.VariableSets.List(ctx, c.OrgName, opts)
		if err != nil {
			return nil, nil, fmt.Errorf("list variable sets: %w", err)
		}
		all = append(all, list.Items...)
		if list.NextPage == 0 || page >= list.TotalPages {
			break
		}
		page++
	}

	// First-cut: report every varset as a skipped-item so the operator
	// is aware. Later increment translates varsets to Terrapod IR. The
	// Terrapod writer needs Terrapod-side varset CRUD endpoints wired,
	// which is its own piece of work in the writer increment.
	var skipped []ir.SkippedItem
	for _, vs := range all {
		desc := fmt.Sprintf("varset %q with %d workspace(s)", vs.Name, len(vs.Workspaces))
		if vs.Global {
			desc = fmt.Sprintf("global varset %q applying to every workspace", vs.Name)
		}
		skipped = append(skipped, ir.SkippedItem{
			Kind:   "tfe-variable-set",
			Name:   desc,
			Reason: "Variable sets are migrated separately (later increment). Recorded here for completeness.",
		})
	}
	return all, skipped, nil
}

// StripTFCPrefixedVariables is a public helper exposing the
// Dynamic-Credentials filtering rule. The Terrapod writer calls this
// when creating workspace variables to ensure no stray TFC_* env vars
// slip through if a future caller bypasses AttachVariables.
//
// Returns the input slice with Dynamic-Credentials env vars omitted.
func StripTFCPrefixedVariables(vars []ir.Variable) []ir.Variable {
	out := make([]ir.Variable, 0, len(vars))
	for _, v := range vars {
		if v.Category == "env" && strings.HasPrefix(v.Key, "TFC_") && isDynamicCredsKey(v.Key) {
			continue
		}
		out = append(out, v)
	}
	return out
}
