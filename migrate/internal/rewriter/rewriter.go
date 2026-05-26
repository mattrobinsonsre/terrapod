// Package rewriter rewrites Terraform HCL files to point at Terrapod
// instead of the source platform.
//
// Two rewrite shapes are supported (both attach inside a
// `terraform {}` block):
//
//	cloud {
//	  hostname     = "app.terraform.io"   →  Terrapod hostname
//	  organization = "acme"               →  "default"
//	  workspaces { name = "app" }         →  workspace name as recorded in state
//	}
//
//	backend "remote" {
//	  hostname     = "app.terraform.io"   →  Terrapod hostname
//	  organization = "acme"               →  "default"
//	  workspaces { name = "app" }         →  workspace name as recorded in state
//	}
//
// Other backends (s3, gcs, azurerm, local, ...) are left alone — the
// rewriter sees them and reports them in its output, but doesn't
// touch the file. Operators decide whether to convert those workspaces
// to remote-state-backed agent mode by hand.
//
// The rewriter is conservative: it only touches the exact attributes
// listed above, preserves all formatting/whitespace/comments outside
// those attributes, and writes files back atomically.
package rewriter

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/hashicorp/hcl/v2"
	"github.com/hashicorp/hcl/v2/hclparse"
	"github.com/hashicorp/hcl/v2/hclwrite"
	"github.com/zclconf/go-cty/cty"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
)

// Options controls a single rewrite invocation.
type Options struct {
	// SourceHost is the source platform hostname that should be
	// replaced — `app.terraform.io` for TFC, `tfe.example.com` for
	// self-hosted, or any other configured cloud-block host. The
	// rewriter only touches blocks whose hostname attribute matches
	// this value (case-insensitive). Required.
	SourceHost string

	// DestHost is the Terrapod hostname to write into every matched
	// block. Required.
	DestHost string

	// SourceOrg is the TFE-side organisation name that needs to be
	// rewritten to "default". When empty the rewriter touches any
	// `organization` attribute regardless of value (Atlantis migrations
	// don't have a source org, but the rewriter still wants to set
	// the destination org to "default" if a cloud block happens to
	// already use a different one — defensive against half-migrated
	// repos).
	SourceOrg string

	// WorkspaceNameMap maps source workspace names → Terrapod
	// workspace names. Optional: if absent, workspace `name = "..."`
	// attributes are left alone (the migration kept the same name).
	WorkspaceNameMap map[string]string

	// DryRun=true reports the planned edits in the FileChange slice
	// but does NOT touch any file on disk.
	DryRun bool
}

// FileChange is the per-file outcome from Rewrite.
type FileChange struct {
	Path     string   `json:"path"`
	Modified bool     `json:"modified"`
	Edits    []string `json:"edits,omitempty"`
	Notes    []string `json:"notes,omitempty"`
}

// Report is the aggregate from RewriteDir.
type Report struct {
	Root     string       `json:"root"`
	Files    []FileChange `json:"files"`
	Modified int          `json:"modified"`
	Skipped  int          `json:"skipped"`
}

// RewriteFromState is a convenience that builds Options from a
// migration state file: source/dest hosts come straight out, and the
// workspace name map is derived from the per-workspace records.
// Callers typically prefer this over hand-building Options so the
// `apply` → `rewrite` flow stays end-to-end consistent.
func RewriteFromState(root string, state *framework.State, dryRun bool) (*Report, error) {
	if state == nil {
		return nil, fmt.Errorf("rewriter: nil migration state — run apply first")
	}
	if state.SourceHost == "" {
		return nil, fmt.Errorf("rewriter: state file has no source_host (was apply run?)")
	}
	if state.DestHost == "" {
		return nil, fmt.Errorf("rewriter: state file has no dest_host (was apply run with --target?)")
	}
	nameMap := map[string]string{}
	for _, w := range state.Workspaces {
		// Same-name migrations are recorded too — leave them in the
		// map so the rewriter can still verify the rewrite hits a
		// known workspace.
		nameMap[w.SourceName] = w.SourceName
	}
	opts := Options{
		SourceHost:       state.SourceHost,
		DestHost:         state.DestHost,
		SourceOrg:        state.SourceOrg,
		WorkspaceNameMap: nameMap,
		DryRun:           dryRun,
	}
	return RewriteDir(root, opts)
}

// RewriteDir walks root recursively, finds every *.tf file, and
// applies Rewrite to it. Subdirectories named `.terraform` /
// `.git` / `node_modules` are skipped — those never contain
// operator-authored HCL.
func RewriteDir(root string, opts Options) (*Report, error) {
	if err := validateOptions(opts); err != nil {
		return nil, err
	}
	report := &Report{Root: root}
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.IsDir() {
			switch d.Name() {
			case ".terraform", ".git", "node_modules":
				return fs.SkipDir
			}
			return nil
		}
		if filepath.Ext(path) != ".tf" {
			return nil
		}
		change, err := rewriteFile(path, opts)
		if err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		report.Files = append(report.Files, change)
		if change.Modified {
			report.Modified++
		} else {
			report.Skipped++
		}
		return nil
	})
	if err != nil {
		return report, err
	}
	sort.Slice(report.Files, func(i, j int) bool { return report.Files[i].Path < report.Files[j].Path })
	return report, nil
}

// rewriteFile is the per-file driver. Parses HCL, walks
// `terraform { cloud { ... } }` and `terraform { backend "remote" { ... } }`
// blocks, applies the substitution, and writes the result back.
func rewriteFile(path string, opts Options) (FileChange, error) {
	change := FileChange{Path: path}

	data, err := os.ReadFile(path)
	if err != nil {
		return change, fmt.Errorf("read: %w", err)
	}

	parser := hclparse.NewParser()
	srcFile, diags := parser.ParseHCL(data, path)
	if diags.HasErrors() {
		// Surface HCL parse errors as notes rather than aborting the
		// whole rewrite — the operator may have a non-Terraform .tf
		// file (e.g. terragrunt) we should report but not fail on.
		change.Notes = append(change.Notes, fmt.Sprintf("HCL parse error: %s", diags.Error()))
		return change, nil
	}
	_ = srcFile // hclparse keeps a reference internally

	// hclwrite preserves trivia (comments, whitespace, ordering) on
	// every block we don't touch.
	f, diags := hclwrite.ParseConfig(data, path, hcl.Pos{Line: 1, Column: 1})
	if diags.HasErrors() {
		change.Notes = append(change.Notes, fmt.Sprintf("hclwrite parse error: %s", diags.Error()))
		return change, nil
	}

	modified := false
	for _, block := range f.Body().Blocks() {
		if block.Type() != "terraform" {
			continue
		}
		for _, inner := range block.Body().Blocks() {
			switch {
			case inner.Type() == "cloud":
				edits := rewriteCloudOrRemote(inner, opts, "cloud")
				if len(edits) > 0 {
					change.Edits = append(change.Edits, edits...)
					modified = true
				}
			case inner.Type() == "backend" && len(inner.Labels()) == 1 && inner.Labels()[0] == "remote":
				edits := rewriteCloudOrRemote(inner, opts, "backend \"remote\"")
				if len(edits) > 0 {
					change.Edits = append(change.Edits, edits...)
					modified = true
				}
			case inner.Type() == "backend" && len(inner.Labels()) == 1:
				// Foreign backend (s3, gcs, azurerm, local): we don't
				// rewrite these — the apply path migrated their
				// state into Terrapod's object store and the operator
				// is expected to swap the backend stanza for a cloud
				// block by hand. Report it so they see the pending
				// work.
				change.Notes = append(change.Notes, fmt.Sprintf(
					"foreign backend %q left alone — convert to cloud{} block by hand after migration",
					inner.Labels()[0]))
			}
		}
	}

	if !modified {
		return change, nil
	}
	change.Modified = true

	if opts.DryRun {
		return change, nil
	}

	out := f.Bytes()
	if err := atomicWrite(path, out); err != nil {
		return change, fmt.Errorf("write: %w", err)
	}
	return change, nil
}

// rewriteCloudOrRemote applies the substitutions inside a cloud or
// backend "remote" block. Returns a slice of human-readable edit
// descriptions for the report. When the block targets a different
// hostname than opts.SourceHost we leave the whole block alone — the
// operator may have multiple TFE instances and we never want to
// retarget one to Terrapod that wasn't part of the migration.
func rewriteCloudOrRemote(block *hclwrite.Block, opts Options, what string) []string {
	body := block.Body()

	// Hostname gate: bail out if this block isn't pointing at the
	// migration's source. Default-host (no explicit hostname) means
	// implicit app.terraform.io — only consider it a match when the
	// migration's SourceHost is that.
	if attr := body.GetAttribute("hostname"); attr != nil {
		cur := attrStringValue(attr)
		if cur == "" || !strings.EqualFold(cur, opts.SourceHost) {
			return nil
		}
	} else if !strings.EqualFold(opts.SourceHost, "app.terraform.io") {
		// Implicit-hostname block, but the migration source isn't
		// the default TFC host — don't touch it.
		return nil
	}

	var edits []string
	if attr := body.GetAttribute("hostname"); attr != nil {
		cur := attrStringValue(attr)
		if cur != opts.DestHost {
			body.SetAttributeValue("hostname", cty.StringVal(opts.DestHost))
			edits = append(edits, fmt.Sprintf("%s: hostname %q → %q", what, cur, opts.DestHost))
		}
	} else {
		// Default-host cloud blocks (no `hostname` attribute) implicitly
		// point at app.terraform.io — add an explicit hostname so the
		// migrated file uses Terrapod.
		body.SetAttributeValue("hostname", cty.StringVal(opts.DestHost))
		edits = append(edits, fmt.Sprintf("%s: added hostname = %q", what, opts.DestHost))
	}

	if attr := body.GetAttribute("organization"); attr != nil {
		cur := attrStringValue(attr)
		if cur != "" && cur != "default" {
			if opts.SourceOrg == "" || strings.EqualFold(cur, opts.SourceOrg) {
				body.SetAttributeValue("organization", cty.StringVal("default"))
				edits = append(edits, fmt.Sprintf("%s: organization %q → \"default\"", what, cur))
			}
		}
	}

	// Workspaces sub-block: rewrite `name = "..."` per the map.
	for _, sub := range body.Blocks() {
		if sub.Type() != "workspaces" {
			continue
		}
		if nameAttr := sub.Body().GetAttribute("name"); nameAttr != nil {
			cur := attrStringValue(nameAttr)
			if cur != "" && len(opts.WorkspaceNameMap) > 0 {
				if mapped, ok := opts.WorkspaceNameMap[cur]; ok && mapped != "" && mapped != cur {
					sub.Body().SetAttributeValue("name", cty.StringVal(mapped))
					edits = append(edits, fmt.Sprintf("%s: workspaces.name %q → %q", what, cur, mapped))
				}
			}
		}
	}

	return edits
}

// attrStringValue best-effort reads a string literal from a parsed
// attribute. Returns "" for non-literal expressions (interpolations,
// variable refs, etc.) — those we never rewrite.
func attrStringValue(attr *hclwrite.Attribute) string {
	tokens := attr.Expr().BuildTokens(nil)
	if len(tokens) < 2 {
		return ""
	}
	// Expect: OQuote, QuotedLit, CQuote. Anything else (heredoc,
	// template expression) we leave alone.
	if tokens[0].Type.String() != "TokenOQuote" {
		return ""
	}
	var sb strings.Builder
	for i := 1; i < len(tokens); i++ {
		t := tokens[i]
		if t.Type.String() == "TokenCQuote" {
			return sb.String()
		}
		if t.Type.String() != "TokenQuotedLit" {
			return "" // template / interpolation — bail
		}
		sb.Write(t.Bytes)
	}
	return ""
}

// atomicWrite writes content to path via a temp file + rename. The
// permission bits match what most operators expect for committed
// HCL: 0644.
func atomicWrite(path string, content []byte) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".rewriter-*.tf")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()
	if _, err := tmp.Write(content); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o644); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

func validateOptions(opts Options) error {
	if opts.SourceHost == "" {
		return fmt.Errorf("rewriter: SourceHost is required")
	}
	if opts.DestHost == "" {
		return fmt.Errorf("rewriter: DestHost is required")
	}
	return nil
}
