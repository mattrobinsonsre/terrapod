package rewriter

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestRewriteFile_CloudBlock_SwapsHostnameOrgWorkspace(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "main.tf")
	writeFile(t, path, `terraform {
  cloud {
    hostname     = "app.terraform.io"
    organization = "acme"
    workspaces {
      name = "app"
    }
  }
}
`)
	change, err := rewriteFile(path, Options{
		SourceHost:       "app.terraform.io",
		DestHost:         "terrapod.example.com",
		SourceOrg:        "acme",
		WorkspaceNameMap: map[string]string{"app": "app-migrated"},
	})
	if err != nil {
		t.Fatalf("rewriteFile: %v", err)
	}
	if !change.Modified {
		t.Errorf("expected Modified=true, got %+v", change)
	}
	if len(change.Edits) < 3 {
		t.Errorf("expected ≥3 edits, got: %+v", change.Edits)
	}

	got, _ := os.ReadFile(path)
	want := []string{`"terrapod.example.com"`, `"default"`, `"app-migrated"`}
	for _, w := range want {
		if !strings.Contains(string(got), w) {
			t.Errorf("rewritten file missing %q: %s", w, got)
		}
	}
	if strings.Contains(string(got), `"acme"`) {
		t.Errorf("rewritten file still contains source org: %s", got)
	}
}

func TestRewriteFile_BackendRemote_SameRewriteAsCloud(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "main.tf")
	writeFile(t, path, `terraform {
  backend "remote" {
    hostname     = "app.terraform.io"
    organization = "acme"
    workspaces {
      name = "app"
    }
  }
}
`)
	change, err := rewriteFile(path, Options{
		SourceHost: "app.terraform.io",
		DestHost:   "terrapod.example.com",
		SourceOrg:  "acme",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !change.Modified {
		t.Errorf("expected Modified=true")
	}
	got, _ := os.ReadFile(path)
	if !strings.Contains(string(got), `"terrapod.example.com"`) || !strings.Contains(string(got), `"default"`) {
		t.Errorf("rewrite missed attributes: %s", got)
	}
}

func TestRewriteFile_LeavesForeignBackendAlone_WithNote(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "main.tf")
	original := `terraform {
  backend "s3" {
    bucket = "acme-tfstate"
    key    = "app/terraform.tfstate"
    region = "us-east-1"
  }
}
`
	writeFile(t, path, original)
	change, err := rewriteFile(path, Options{SourceHost: "app.terraform.io", DestHost: "terrapod.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if change.Modified {
		t.Errorf("s3 backend should be left alone")
	}
	if len(change.Notes) == 0 || !strings.Contains(change.Notes[0], "s3") {
		t.Errorf("expected a 'foreign backend' note, got: %+v", change.Notes)
	}
	// File on disk should be byte-identical.
	got, _ := os.ReadFile(path)
	if string(got) != original {
		t.Errorf("foreign-backend file was modified:\nwant=%q\ngot=%q", original, got)
	}
}

func TestRewriteFile_HostnameMismatch_LeavesAlone(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "main.tf")
	writeFile(t, path, `terraform {
  cloud {
    hostname     = "tfe.elsewhere.com"
    organization = "acme"
    workspaces {
      name = "app"
    }
  }
}
`)
	// SourceHost="app.terraform.io" doesn't match "tfe.elsewhere.com",
	// so the rewriter should leave this block alone — we don't want
	// to retarget the operator's other TFE instance.
	change, err := rewriteFile(path, Options{
		SourceHost: "app.terraform.io",
		DestHost:   "terrapod.example.com",
	})
	if err != nil {
		t.Fatal(err)
	}
	if change.Modified {
		t.Errorf("rewrite should leave mismatched hostname alone")
	}
}

func TestRewriteFile_AddsMissingHostname(t *testing.T) {
	// Default-host cloud{} blocks (no explicit hostname → app.terraform.io)
	// should be retargeted to Terrapod by adding the hostname attribute.
	dir := t.TempDir()
	path := filepath.Join(dir, "main.tf")
	writeFile(t, path, `terraform {
  cloud {
    organization = "acme"
    workspaces {
      name = "app"
    }
  }
}
`)
	change, err := rewriteFile(path, Options{
		SourceHost: "app.terraform.io",
		DestHost:   "terrapod.example.com",
		SourceOrg:  "acme",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !change.Modified {
		t.Errorf("expected Modified=true")
	}
	got, _ := os.ReadFile(path)
	if !strings.Contains(string(got), `hostname = "terrapod.example.com"`) {
		t.Errorf("hostname not added: %s", got)
	}
}

func TestRewriteFile_DryRun_NoDiskWrite(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "main.tf")
	original := `terraform {
  cloud {
    hostname     = "app.terraform.io"
    organization = "acme"
    workspaces { name = "app" }
  }
}
`
	writeFile(t, path, original)
	change, err := rewriteFile(path, Options{
		SourceHost: "app.terraform.io",
		DestHost:   "terrapod.example.com",
		SourceOrg:  "acme",
		DryRun:     true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !change.Modified {
		t.Errorf("dry-run should still report Modified")
	}
	got, _ := os.ReadFile(path)
	if string(got) != original {
		t.Errorf("dry-run wrote to disk!\nwant=%q\ngot=%q", original, got)
	}
}

func TestRewriteDir_SkipsHiddenAndVendorDirs(t *testing.T) {
	dir := t.TempDir()
	rewritable := `terraform {
  cloud {
    hostname     = "app.terraform.io"
    organization = "acme"
  }
}
`
	writeFile(t, filepath.Join(dir, "main.tf"), rewritable)
	writeFile(t, filepath.Join(dir, ".terraform", "skip.tf"), rewritable)
	writeFile(t, filepath.Join(dir, ".git", "config"), `# git, not hcl`)

	report, err := RewriteDir(dir, Options{
		SourceHost: "app.terraform.io",
		DestHost:   "terrapod.example.com",
		SourceOrg:  "acme",
	})
	if err != nil {
		t.Fatal(err)
	}
	if report.Modified != 1 {
		t.Errorf("expected 1 modified file, got %d (%+v)", report.Modified, report.Files)
	}
	// .terraform/skip.tf should NOT appear in the report.
	for _, f := range report.Files {
		if strings.Contains(f.Path, ".terraform") {
			t.Errorf("walker descended into .terraform: %+v", f)
		}
	}
}
