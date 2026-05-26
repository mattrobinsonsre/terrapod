// Package atlantis is the Atlantis source plugin for terrapod-migrate.
//
// Atlantis stores almost no platform state of its own. The migration
// reads each repo's `atlantis.yaml` (v3 schema, atlantis ≥ v0.30), maps
// projects to Terrapod workspaces (or a single autodiscovery rule when
// the per-project pattern is uniform), and records the source repos so
// the Terrapod writer can create matching VCS connections.
//
// Subsequent increments fill in:
//   - vcs.go            VCS reader (fetches atlantis.yaml from GitHub/GitLab)
//   - emit.go           Projects → ir.Plan emitter (with autodiscovery
//                       heuristic and per-project field translation)
//   - state.go strategy The leave-state-in-place vs --migrate-state decision
//
// This file is the pure-data parser. Hand-rolled `yaml:"..."` tags
// against gopkg.in/yaml.v3 keep our struct shape independent of the
// upstream `runatlantis/atlantis` Go module (which has a heavy dep
// surface and would force us to track its CVE/release cadence). The
// `atlantis.yaml` schema is documented and stable; the cost of owning
// the parser shape is low.
package atlantis

import (
	"errors"
	"fmt"
	"strings"

	"gopkg.in/yaml.v3"
)

// SupportedVersion is the atlantis.yaml schema version this parser
// understands. Atlantis has bumped the version field through three
// generations; v3 is what every modern (v0.17+) Atlantis deployment
// writes. Anything else is rejected with a clear error rather than
// silently parsed against the wrong shape.
const SupportedVersion = 3

// AtlantisYAML is the root of an atlantis.yaml v3 document. Only the
// fields the migration tool actually consumes are unmarshalled; the
// rest are accepted (yaml.v3 silently drops unknown keys) so a v3
// document with newer optional fields still parses cleanly.
type AtlantisYAML struct {
	Version  int       `yaml:"version"`
	Projects []Project `yaml:"projects"`
	// Workflows + Allowed* fields are read so the source plugin can
	// emit `Skipped` items for any project that references a custom
	// workflow (Terrapod has no first-class equivalent today). We
	// don't materialise the workflow body — just record its presence.
	Workflows map[string]Workflow `yaml:"workflows,omitempty"`
	// AllowedOverrides / AllowedWorkflows / AllowCustomWorkflows control
	// what server-side defaults a repo may override. Their values
	// don't change the migration shape, but their *presence* hints at
	// org-level Atlantis configuration the operator may need to
	// recreate manually on Terrapod's side. Recorded in
	// SourceMetadata, not in the IR proper.
	AllowedOverrides     []string `yaml:"allowed_overrides,omitempty"`
	AllowedWorkflows     []string `yaml:"allowed_workflows,omitempty"`
	AllowCustomWorkflows bool     `yaml:"allow_custom_workflows,omitempty"`
	// AutoDiscover (Atlantis v0.27+) is the Atlantis-side cousin of
	// Terrapod's own autodiscovery feature. When set, Atlantis scans
	// the repo for Terraform projects without an explicit `projects:`
	// entry; we record the setting so the emitter can decide whether
	// to lean on Terrapod's autodiscovery in turn.
	AutoDiscover *AutoDiscover `yaml:"autodiscover,omitempty"`
}

// Project maps to a single Atlantis project — equivalent to a Terrapod
// workspace (or one entry in a Terrapod autodiscovery rule's
// materialisation).
type Project struct {
	Name             string   `yaml:"name,omitempty"`
	Dir              string   `yaml:"dir"`
	Workspace        string   `yaml:"workspace,omitempty"`
	TerraformVersion string   `yaml:"terraform_version,omitempty"`
	Branch           string   `yaml:"branch,omitempty"`
	AutoPlan         AutoPlan `yaml:"autoplan,omitempty"`
	// ApplyRequirements (`approved`, `mergeable`, `undiverged`) have no
	// direct Terrapod equivalent — recorded for the handover doc but
	// don't influence the migrated workspace.
	ApplyRequirements []string `yaml:"apply_requirements,omitempty"`
	ImportRequirements []string `yaml:"import_requirements,omitempty"`
	// Workflow references a key in the top-level `workflows:` map. If
	// non-empty the emitter records the project as "uses custom
	// workflow" in the handover doc.
	Workflow string `yaml:"workflow,omitempty"`
	// ExecutionOrderGroup is an integer indicating apply-ordering
	// across projects in the same Atlantis plan. No direct Terrapod
	// equivalent (Terrapod uses run triggers for cross-workspace
	// ordering). Recorded for the handover doc.
	ExecutionOrderGroup int `yaml:"execution_order_group,omitempty"`
	// RepoLocking and friends are runtime Atlantis behaviours and
	// don't influence the migrated workspace; ignored on purpose.
	RepoLocking *bool `yaml:"repo_locking,omitempty"`
	// CustomPolicyCheck signals Conftest-based policy gating. Terrapod
	// uses OPA per #343; recorded as an advisory skipped-item with
	// guidance pointing at docs/policies.md.
	CustomPolicyCheck *bool `yaml:"custom_policy_check,omitempty"`
	// Terraform Distribution selects terraform vs tofu at the repo
	// level. Terrapod has the same `execution_backend` distinction;
	// translates directly.
	TerraformDistribution string `yaml:"terraform_distribution,omitempty"`
}

// AutoPlan is the per-project autoplanning config.
type AutoPlan struct {
	WhenModified []string `yaml:"when_modified,omitempty"`
	Enabled      bool     `yaml:"enabled,omitempty"`
}

// Workflow holds the per-step commands. We don't actually run these or
// inspect their contents; we only need to know a project *uses* a
// workflow (then we record an unsupported skipped-item).
type Workflow struct {
	Plan       *WorkflowStage `yaml:"plan,omitempty"`
	Apply      *WorkflowStage `yaml:"apply,omitempty"`
	PolicyCheck *WorkflowStage `yaml:"policy_check,omitempty"`
	Import     *WorkflowStage `yaml:"import,omitempty"`
	StateRm    *WorkflowStage `yaml:"state_rm,omitempty"`
}

// WorkflowStage is the steps slice for one workflow stage. Each step
// is either a string ("init", "plan") or a map ({run: "make foo"}).
// We keep them as raw YAML nodes; nothing in the migration tool needs
// to *execute* these. The presence of any custom step in the apply
// stage is what triggers the skipped-item note.
type WorkflowStage struct {
	Steps []yaml.Node `yaml:"steps,omitempty"`
}

// AutoDiscover is the Atlantis-side autodiscovery config (v0.27+).
type AutoDiscover struct {
	Mode         string   `yaml:"mode,omitempty"` // "auto" | "enabled" | "disabled"
	IgnorePaths  []string `yaml:"ignore_paths,omitempty"`
}

// ErrUnsupportedVersion is returned for atlantis.yaml documents whose
// `version` field is not 3. Atlantis itself rejects v1/v2 in modern
// releases; we mirror that rather than silently best-effort.
var ErrUnsupportedVersion = errors.New("atlantis.yaml: only version 3 is supported")

// ErrMissingVersion is returned when the YAML parses but the version
// field is absent. Atlantis treats this as v1 (legacy), which is the
// same as ErrUnsupportedVersion but with a clearer error message.
var ErrMissingVersion = errors.New("atlantis.yaml: missing required `version: 3` field")

// Parse decodes an atlantis.yaml document from raw bytes. Caller is
// responsible for fetching the bytes (from a local file for tests, or
// from a VCS read for real migrations).
//
// Errors are wrapped with the path the operator passed in so error
// messages name the file even when many repos are processed in a
// single migration.
func Parse(path string, data []byte) (*AtlantisYAML, error) {
	if len(data) == 0 {
		return nil, fmt.Errorf("%s: empty file", path)
	}
	var doc AtlantisYAML
	dec := yaml.NewDecoder(strings.NewReader(string(data)))
	dec.KnownFields(false) // forward-compatible with newer v3 fields
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("%s: parse YAML: %w", path, err)
	}
	switch doc.Version {
	case 0:
		return nil, fmt.Errorf("%s: %w", path, ErrMissingVersion)
	case SupportedVersion:
		// OK
	default:
		return nil, fmt.Errorf("%s: %w (got %d)", path, ErrUnsupportedVersion, doc.Version)
	}
	// Sanity: every project needs a `dir`. Atlantis itself rejects
	// projects without it; we mirror that so the operator sees the
	// problem at migration time rather than at first-run time.
	for i, p := range doc.Projects {
		if strings.TrimSpace(p.Dir) == "" {
			label := p.Name
			if label == "" {
				label = fmt.Sprintf("projects[%d]", i)
			}
			return nil, fmt.Errorf("%s: project %q is missing `dir`", path, label)
		}
	}
	return &doc, nil
}

// ProjectIdentifier returns the canonical name the migration tool uses
// to talk about an Atlantis project. Atlantis allows two shapes:
//
//   - `name:` is set → use that as-is
//   - `name:` is empty → derive from `dir` (+ `workspace` if non-empty)
//
// The derived name matches Atlantis's own --autoplan-file-list output
// and shows up in PR comments, so operators recognise it. The migration
// state file uses this string as the SourceID for the project.
func ProjectIdentifier(p Project) string {
	if p.Name != "" {
		return p.Name
	}
	dir := strings.TrimSpace(p.Dir)
	if dir == "" {
		dir = "."
	}
	if p.Workspace != "" && p.Workspace != "default" {
		return dir + "/" + p.Workspace
	}
	return dir
}
