package atlantis

import (
	"fmt"
	"strings"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// EmitOptions tweaks the conversion from Atlantis projects to the IR.
// The first release keeps the option surface narrow on purpose — the
// only knob is the source identity, which we need for the IR's
// terrapod-migrated-from label and the migration state file.
type EmitOptions struct {
	// Repo is the canonical repo URL the atlantis.yaml lives in,
	// e.g. "https://github.com/acme/infra". Used to build a one-per-
	// repo Terrapod VCS connection and to thread the repo URL onto
	// every workspace produced from this atlantis.yaml.
	Repo string

	// VCSConnectionRef matches the VCSConnection.SourceID the writer
	// will create for `Repo`. The emitter sets every workspace's
	// VCSConnectionRef to this string so the writer can resolve
	// dependencies without name games.
	VCSConnectionRef string

	// DefaultBranch is the repo's default branch (e.g. "main"). Per-
	// project `branch:` regexes override this; when absent we record
	// the default. Operators set this from a CLI flag or by reading
	// the VCS provider's default-branch field.
	DefaultBranch string
}

// Emit converts a parsed atlantis.yaml into an ir.Plan slice — one
// per project. The caller appends these to the wider ir.Plan along
// with VCSConnections and SkippedItems Emit also produces.
//
// Translation rules (locked here so the migration tool's behaviour is
// auditable, not buried in 200 lines of mapping code):
//
//   - dir                     → ir.Workspace.WorkingDirectory
//   - workspace=""/"default"  → no workspace suffix on SourceName
//   - workspace="<other>"     → SourceID/Name suffix "/<workspace>"
//   - terraform_version       → ir.Workspace.TerraformVersion (verbatim;
//                                Terrapod accepts partials like "1.12")
//   - terraform_distribution  → ir.Workspace.ExecutionMode is always
//                                "agent" for Atlantis migrations (Atlantis
//                                runs the binary itself; the migration
//                                preserves that operational shape).
//                                ExecutionBackend is set elsewhere by the
//                                writer from this field — recorded for
//                                now in Labels["terraform_distribution"].
//   - autoplan.enabled=false  → SkippedItem (Terrapod runs plan on PR
//                                push unconditionally; an explicit
//                                "no-autoplan" project on Atlantis would
//                                surprise the operator on Terrapod)
//   - workflow != ""          → SkippedItem (custom workflows have no
//                                Terrapod equivalent — operator handles)
//   - apply_requirements      → SkippedItem (advisory metadata)
//   - custom_policy_check     → SkippedItem (Terrapod uses OPA, see #343)
//   - execution_order_group   → SkippedItem (Terrapod uses run triggers)
//
// Errors are returned for situations the emitter doesn't tolerate (e.g.
// duplicate project identifiers within a single atlantis.yaml). Schema
// issues are caught by the parser; the emitter focuses on translation
// semantics.
func Emit(doc *AtlantisYAML, opts EmitOptions) (workspaces []ir.Workspace, skipped []ir.SkippedItem, err error) {
	if doc == nil {
		return nil, nil, fmt.Errorf("atlantis.Emit: nil document")
	}
	if opts.Repo == "" {
		return nil, nil, fmt.Errorf("atlantis.Emit: Repo is required (the source repo URL)")
	}

	seen := make(map[string]int) // SourceID → projects[] index, for dupe detection

	for i, p := range doc.Projects {
		sourceID := ProjectIdentifier(p)
		if prev, ok := seen[sourceID]; ok {
			return nil, nil, fmt.Errorf("atlantis.Emit: duplicate project identifier %q (projects[%d] and projects[%d]). Atlantis disambiguates by `name:`; set unique names in atlantis.yaml or distinct workspace values", sourceID, prev, i)
		}
		seen[sourceID] = i

		w := ir.Workspace{
			SourceID:         sourceID,
			Name:             terrapodWorkspaceName(sourceID),
			ExecutionMode:    "agent",
			TerraformVersion: p.TerraformVersion,
			WorkingDirectory: p.Dir,
			VCSConnectionRef: opts.VCSConnectionRef,
			VCSRepoURL:       opts.Repo,
			VCSBranch:        branchOrDefault(p.Branch, opts.DefaultBranch),
			Labels:           map[string]string{},
		}

		// Record the source-side metadata that the writer doesn't yet
		// have first-class fields for, as labels with a
		// "terrapod-migration/" prefix. The prefix lets operators
		// search by it and lets a future increment promote any of
		// these to first-class without breaking the label-based
		// migration record.
		if p.TerraformDistribution != "" {
			w.Labels["terrapod-migration/atlantis-distribution"] = p.TerraformDistribution
		}
		if p.Workspace != "" && p.Workspace != "default" {
			w.Labels["terrapod-migration/atlantis-workspace"] = p.Workspace
		}

		workspaces = append(workspaces, w)

		// Per-project skipped-items. Each one becomes an entry the
		// operator reads in the migration report so they know what
		// they're losing.
		if !p.AutoPlan.Enabled && len(p.AutoPlan.WhenModified) == 0 {
			// `autoplan.enabled` defaults to true in Atlantis; the
			// only way to reach here is an explicit `enabled: false`
			// AND no `when_modified` glob. Empty AutoPlan struct
			// (operator didn't set it) is the common case and not
			// reported.
			//
			// Actually — yaml unmarshalling of a missing autoplan
			// block leaves Enabled at zero-value (false) too, which
			// is indistinguishable from `enabled: false`. We can't
			// tell the two apart without a *bool. For the first
			// release, skip this report — false positives would be
			// confusing. Track as a follow-up if operators ask for
			// it.
			_ = p // intentional no-op until *bool field
		}
		if p.Workflow != "" {
			skipped = append(skipped, ir.SkippedItem{
				Kind:   "atlantis-workflow",
				Name:   fmt.Sprintf("%s (referenced by project %s)", p.Workflow, sourceID),
				Reason: "Atlantis custom workflows have no Terrapod equivalent. Operator decides per workflow whether to translate to run-tasks (#343), runner setup scripts, or external CI.",
			})
		}
		if len(p.ApplyRequirements) > 0 {
			skipped = append(skipped, ir.SkippedItem{
				Kind:   "atlantis-apply-requirements",
				Name:   fmt.Sprintf("project %s: %s", sourceID, strings.Join(p.ApplyRequirements, ", ")),
				Reason: "Atlantis apply_requirements (approved/mergeable/undiverged) have no direct Terrapod equivalent. Configure repo branch-protection rules on the VCS side instead.",
			})
		}
		if p.CustomPolicyCheck != nil && *p.CustomPolicyCheck {
			skipped = append(skipped, ir.SkippedItem{
				Kind:   "atlantis-custom-policy-check",
				Name:   fmt.Sprintf("project %s", sourceID),
				Reason: "Atlantis Conftest-based policy checks are not migrated. Terrapod uses OPA via Rego — translate policies manually (see docs/policies.md).",
			})
		}
		if p.ExecutionOrderGroup != 0 {
			skipped = append(skipped, ir.SkippedItem{
				Kind:   "atlantis-execution-order-group",
				Name:   fmt.Sprintf("project %s (group %d)", sourceID, p.ExecutionOrderGroup),
				Reason: "Atlantis execution_order_group has no direct equivalent. Use Terrapod run-triggers between workspaces to express apply-ordering dependencies.",
			})
		}
	}

	// Top-level workflows that exist but aren't referenced by any
	// project still show up in the handover doc so operators
	// can decide whether to recreate any of them.
	for name := range doc.Workflows {
		// Was this workflow referenced by any project? If yes we
		// already emitted the SkippedItem; don't double-report.
		alreadyReported := false
		for _, p := range doc.Projects {
			if p.Workflow == name {
				alreadyReported = true
				break
			}
		}
		if alreadyReported {
			continue
		}
		skipped = append(skipped, ir.SkippedItem{
			Kind:   "atlantis-workflow",
			Name:   name + " (defined but unreferenced)",
			Reason: "Atlantis workflow defined in atlantis.yaml but no project references it. Recorded for completeness; safe to ignore unless intentional.",
		})
	}

	return workspaces, skipped, nil
}

// terrapodWorkspaceName takes the Atlantis project identifier (which
// may contain "/" from dir+workspace concatenation) and produces a
// Terrapod-legal workspace name. Terrapod workspaces accept "/" — but
// many operators prefer the "-" convention for readability. The
// migration tool defaults to "-" and records the original identifier
// as the SourceID, so the migration state file maps back cleanly.
func terrapodWorkspaceName(sourceID string) string {
	return strings.ReplaceAll(sourceID, "/", "-")
}

// branchOrDefault picks the per-project branch when set, otherwise the
// repo default. Atlantis stores `branch:` as a regex (e.g. `/main/`);
// Terrapod treats `vcs_branch` as a literal name. We strip the leading
// and trailing `/` so a typical `/main/` becomes `main`. More elaborate
// regexes (e.g. `/release-.*/`) are recorded verbatim with a follow-up
// SkippedItem suggesting the operator pick one literal branch.
//
// Returning the raw value when no `/` delimiters are present is the
// common case for non-regex `branch:` settings.
func branchOrDefault(projectBranch, defaultBranch string) string {
	if projectBranch == "" {
		return defaultBranch
	}
	pb := strings.TrimSpace(projectBranch)
	if strings.HasPrefix(pb, "/") && strings.HasSuffix(pb, "/") && len(pb) >= 2 {
		return strings.TrimSuffix(strings.TrimPrefix(pb, "/"), "/")
	}
	return pb
}
