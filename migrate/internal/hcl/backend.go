// Package hcl contains read-only HCL utilities shared between the
// Atlantis source plugin (which reads backend declarations to know
// where state lives) and the `rewrite` subcommand (which rewrites
// backend / cloud / module-source declarations).
//
// Read vs. write split: this package only INSPECTS HCL — no file
// modifications. The rewriter package (internal/rewriter, increment
// 6) handles writes. Keeping the boundary clean means the Atlantis
// source plugin imports hcl but never the rewriter, so the source's
// dry-run path can never accidentally mutate operator files.
package hcl

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/hashicorp/hcl/v2"
	"github.com/hashicorp/hcl/v2/hclparse"
	"github.com/hashicorp/hcl/v2/hclsyntax"
)

// BackendKind identifies the source-side state-storage backend
// declared in a Terraform module. Terrapod is single-backend by design
// — every migrated workspace's state lives in Terrapod's own storage,
// regardless of execution mode. The migration tool reads the source-
// side backend declarations only to know where to fetch state from
// before rewriting them away.
type BackendKind string

const (
	// BackendLocal is the implicit `terraform {}` (no backend block)
	// case OR explicit `backend "local" {}` — state in a file on disk.
	BackendLocal BackendKind = "local"
	// BackendS3 — AWS S3 (most common for Atlantis users).
	BackendS3 BackendKind = "s3"
	// BackendGCS — Google Cloud Storage.
	BackendGCS BackendKind = "gcs"
	// BackendAzureRM — Azure Blob Storage.
	BackendAzureRM BackendKind = "azurerm"
	// BackendRemote — HashiCorp's `terraform { backend "remote" {} }`
	// pointing at TFE / HCP. Detected separately so the migrator can
	// tell the operator "this isn't really an Atlantis migration;
	// rerun with --source=tfe".
	BackendRemote BackendKind = "remote"
	// BackendCloud — `terraform { cloud {} }` block, also TFE/HCP.
	// Same operator action as BackendRemote.
	BackendCloud BackendKind = "cloud"
)

// Backend describes a backend block as found in a Terraform module's
// HCL. Settings is the literal key=value pairs from the block — terraform
// itself forbids variables/functions in backend config, so every value
// is guaranteed to be a literal string/number/bool that we can read
// without an evaluation context.
type Backend struct {
	// Kind is "s3", "gcs", "azurerm", "local", "remote", or "cloud".
	Kind BackendKind

	// Settings maps the backend's attribute name to its literal value
	// rendered as a string. Backend-specific keys: S3 has "bucket",
	// "key", "region"; GCS has "bucket", "prefix"; azurerm has
	// "storage_account_name", "container_name", "key", "resource_group_name".
	// All values are coerced to string for uniformity; consumers parse
	// as needed.
	Settings map[string]string

	// SourceFile is the absolute path of the .tf file the block was
	// declared in. Surfaced in errors and in the migration report so
	// the operator can find the declaration when rewriting.
	SourceFile string

	// Range is the HCL position of the backend block. Used by the
	// rewriter (increment 6) to know where to splice in the
	// `terraform { cloud {} }` replacement.
	Range hcl.Range
}

// ErrConflictingBackends means a single module directory declared
// `terraform { backend "..." }` in more than one .tf file with different
// backend kinds — terraform itself rejects this. We do too.
var ErrConflictingBackends = errors.New("module has conflicting backend declarations across .tf files")

// ErrMultipleTerraformBlocks means more than one `terraform {}` block
// across the .tf files in a single module directory carries a backend
// declaration. terraform allows multiple terraform blocks but caps
// backend declarations at one; we mirror.
var ErrMultipleTerraformBlocks = errors.New("module has more than one backend declaration")

// DetectBackend walks every .tf file directly under dir (non-recursive
// — terraform itself only loads files in the current module dir) and
// returns the single Backend declared, or BackendLocal if none.
//
// Returns:
//   - (*Backend{Kind:Local}, nil) for "no backend block" or explicit local
//   - (*Backend{Kind:S3/GCS/AzureRM/Remote/Cloud}, nil) for matching declarations
//   - non-nil error on parse failure, multiple conflicting declarations,
//     or unrecognised backend kinds (a value we don't know how to
//     migrate from is better as a hard error than a silent skip)
//
// The function is read-only and side-effect-free.
func DetectBackend(dir string) (*Backend, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read module dir %s: %w", dir, err)
	}
	var tfFiles []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		// Skip .tf.json + override + ignored-by-convention dotfiles.
		// terraform's own loader is much more permissive than this, but
		// the operator-facing migrator doesn't need to model every
		// edge case — the common module-dir layout is what matters.
		if !strings.HasSuffix(name, ".tf") {
			continue
		}
		if strings.HasPrefix(name, ".") {
			continue
		}
		tfFiles = append(tfFiles, filepath.Join(dir, name))
	}
	// Sort for deterministic error messages — when two files
	// disagree, we want the report to name the same first-and-second
	// file every run.
	sort.Strings(tfFiles)

	parser := hclparse.NewParser()
	var found *Backend
	for _, path := range tfFiles {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", path, err)
		}
		file, diags := parser.ParseHCL(data, path)
		if diags.HasErrors() {
			return nil, fmt.Errorf("parse %s: %s", path, diags.Error())
		}

		got, err := detectInFile(file, path)
		if err != nil {
			return nil, err
		}
		if got == nil {
			continue
		}
		if found != nil {
			if found.Kind != got.Kind {
				return nil, fmt.Errorf("%w: %s declares %s, %s declares %s",
					ErrConflictingBackends, found.SourceFile, found.Kind, got.SourceFile, got.Kind)
			}
			return nil, fmt.Errorf("%w: first in %s, again in %s",
				ErrMultipleTerraformBlocks, found.SourceFile, got.SourceFile)
		}
		found = got
	}

	if found != nil {
		return found, nil
	}
	// No explicit backend → terraform defaults to local backend in
	// terraform.tfstate at the module root. Encode that here so the
	// state-reader doesn't need a "no backend means local" branch.
	return &Backend{
		Kind:       BackendLocal,
		Settings:   map[string]string{"path": filepath.Join(dir, "terraform.tfstate")},
		SourceFile: "",
	}, nil
}

// detectInFile inspects one parsed .tf file for a `terraform {}` block
// containing a `backend "..." {}` or `cloud {}` child. Returns nil
// when the file has no backend declaration (the common case for
// modules that are imported into a workspace, not the workspace root).
func detectInFile(file *hcl.File, path string) (*Backend, error) {
	body, ok := file.Body.(*hclsyntax.Body)
	if !ok {
		return nil, fmt.Errorf("%s: unexpected body type %T", path, file.Body)
	}
	for _, block := range body.Blocks {
		if block.Type != "terraform" {
			continue
		}
		for _, child := range block.Body.Blocks {
			switch child.Type {
			case "backend":
				if len(child.Labels) != 1 {
					return nil, fmt.Errorf("%s: `backend` block must have exactly one label naming the backend type (got %d labels)", path, len(child.Labels))
				}
				kind := BackendKind(child.Labels[0])
				switch kind {
				case BackendS3, BackendGCS, BackendAzureRM, BackendLocal, BackendRemote:
					// Supported (or detected-and-rejected, in the case
					// of BackendRemote — see DetectBackend caller).
				default:
					return nil, fmt.Errorf("%s: backend %q is not supported for migration. Supported: s3, gcs, azurerm, local, remote", path, kind)
				}
				settings, err := readSettings(child, path)
				if err != nil {
					return nil, err
				}
				return &Backend{
					Kind:       kind,
					Settings:   settings,
					SourceFile: path,
					Range:      child.Range(),
				}, nil
			case "cloud":
				settings, err := readSettings(child, path)
				if err != nil {
					return nil, err
				}
				return &Backend{
					Kind:       BackendCloud,
					Settings:   settings,
					SourceFile: path,
					Range:      child.Range(),
				}, nil
			}
		}
	}
	return nil, nil
}

// readSettings extracts every attribute of a backend/cloud block as a
// literal string. terraform forbids variables/functions in backend
// blocks, so every Expr evaluates without a context. Settings the
// detector can't render as a literal (an unexpected expression type)
// return an error rather than silently dropping the key — operator
// must see the issue, not get half a config later.
func readSettings(block *hclsyntax.Block, path string) (map[string]string, error) {
	out := make(map[string]string, len(block.Body.Attributes))
	for name, attr := range block.Body.Attributes {
		// LiteralValueExpr / TemplateExpr / numbers / bools all
		// evaluate cleanly with a nil context because terraform
		// disallows interpolation here.
		val, diags := attr.Expr.Value(nil)
		if diags.HasErrors() {
			return nil, fmt.Errorf("%s: backend/cloud attribute %q is not a literal (terraform forbids interpolation in this block): %s", path, name, diags.Error())
		}
		// Coerce to string. The backend SDKs will reparse as needed.
		var s string
		switch {
		case val.Type().FriendlyName() == "string":
			s = val.AsString()
		case val.Type().FriendlyName() == "bool":
			if val.True() {
				s = "true"
			} else {
				s = "false"
			}
		default:
			// Numbers and other primitives — use the cty stringifier.
			s = val.GoString()
		}
		out[name] = s
	}
	return out, nil
}
