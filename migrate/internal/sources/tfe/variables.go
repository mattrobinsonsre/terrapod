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
		out         []ir.Variable
		dynSkipped  []ir.SkippedItem
		sensSkipped []ir.SkippedItem
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

// VariableSets pulls every variable set in the org (with its variables
// and workspace assignments) and translates them to ir.VariableSet for
// the writer to create on Terrapod. Sensitive variable values and
// Dynamic-Credentials env vars are handled exactly as for workspace
// variables: sensitive values are never read (empty value + a
// SkippedItem telling the operator to re-enter), and TFC_* dynamic-creds
// keys are stripped (Terrapod uses runner-pool workload identity).
//
// Project- and Stack-scoped assignments have no Terrapod equivalent
// (single-org, no projects): the varset itself and its direct workspace
// assignments migrate, but the project/stack scoping is surfaced as a
// SkippedItem so the operator re-assigns by hand.
func (c *Client) VariableSets(ctx context.Context) ([]ir.VariableSet, []ir.SkippedItem, error) {
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

	var (
		sets    []ir.VariableSet
		skipped []ir.SkippedItem
	)
	for _, vs := range all {
		set, sk := varsetToIR(vs, c.TokenTier)
		sets = append(sets, set)
		skipped = append(skipped, sk...)
	}
	return sets, skipped, nil
}

// varsetToIR translates one go-tfe VariableSet (with its Variables and
// Workspaces relationships populated) into an ir.VariableSet plus any
// SkippedItems. Pure — no API calls — so it's directly unit-testable.
func varsetToIR(vs *tfe.VariableSet, tier TokenTier) (ir.VariableSet, []ir.SkippedItem) {
	set := ir.VariableSet{
		SourceID:    vs.ID,
		Name:        vs.Name,
		Description: vs.Description,
		Global:      vs.Global,
		Priority:    vs.Priority,
	}
	var skipped []ir.SkippedItem

	for _, v := range vs.Variables {
		if v == nil {
			continue
		}
		if v.Category == tfe.CategoryEnv && isDynamicCredsKey(v.Key) {
			skipped = append(skipped, ir.SkippedItem{
				Kind: "tfe-dynamic-credentials",
				Name: fmt.Sprintf("varset %s: %s", vs.Name, v.Key),
				Reason: "TFE Dynamic Credentials env var; Terrapod uses Kubernetes Workload Identity " +
					"via runner-pool ServiceAccount annotations instead (see docs/runners.md). Stripped " +
					"from the migrated variable set.",
			})
			continue
		}
		set.Variables = append(set.Variables, ir.Variable{
			Key:         v.Key,
			Value:       v.Value, // empty when sensitive
			Category:    categoryString(v.Category),
			HCL:         v.HCL,
			Sensitive:   v.Sensitive,
			Description: v.Description,
		})
		if v.Sensitive {
			skipped = append(skipped, ir2skippedSensitiveVarset(vs.Name, v.Key, tier))
		}
	}

	// Direct workspace assignments carry over as source-ID refs the
	// writer resolves to Terrapod workspace IDs after the workspace
	// loop. Global sets carry none (they apply to everything).
	if !vs.Global {
		for _, ws := range vs.Workspaces {
			if ws != nil && ws.ID != "" {
				set.WorkspaceRefs = append(set.WorkspaceRefs, ws.ID)
			}
		}
	}

	// Project / Stack scoping has no single-org Terrapod equivalent.
	if len(vs.Projects) > 0 || len(vs.Stacks) > 0 {
		skipped = append(skipped, ir.SkippedItem{
			Kind:   "tfe-variable-set-project-scope",
			Name:   fmt.Sprintf("varset %s", vs.Name),
			Reason: "Assigned to TFE project(s)/stack(s), which Terrapod (single-org, no projects) has no equivalent for. The variable set and its direct workspace assignments migrate; re-assign the project/stack-scoped workspaces by hand.",
		})
	}

	return set, skipped
}

// ir2skippedSensitiveVarset is the varset analogue of
// ir2skippedSensitive — same wording, varset context.
func ir2skippedSensitiveVarset(varsetName, key string, tier TokenTier) ir.SkippedItem {
	s := ir2skippedSensitive(varsetName, key, tier)
	s.Kind = "tfe-sensitive-varset-variable"
	s.Name = fmt.Sprintf("varset %s: %s", varsetName, key)
	return s
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
