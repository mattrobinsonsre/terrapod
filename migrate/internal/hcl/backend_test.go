package hcl

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeTF(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestDetectBackend_S3(t *testing.T) {
	// The common Atlantis case: S3 backend with explicit bucket/key/region.
	dir := t.TempDir()
	writeTF(t, dir, "backend.tf", `
terraform {
  backend "s3" {
    bucket = "acme-tfstate"
    key    = "prod/api/terraform.tfstate"
    region = "eu-west-1"
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendS3 {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendS3)
	}
	if b.Settings["bucket"] != "acme-tfstate" || b.Settings["key"] != "prod/api/terraform.tfstate" || b.Settings["region"] != "eu-west-1" {
		t.Errorf("S3 settings: %+v", b.Settings)
	}
	if !strings.HasSuffix(b.SourceFile, "backend.tf") {
		t.Errorf("SourceFile = %q", b.SourceFile)
	}
}

func TestDetectBackend_GCS(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "gcs" {
    bucket = "acme-tfstate-gcs"
    prefix = "prod/api"
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendGCS {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendGCS)
	}
	if b.Settings["bucket"] != "acme-tfstate-gcs" || b.Settings["prefix"] != "prod/api" {
		t.Errorf("GCS settings: %+v", b.Settings)
	}
}

func TestDetectBackend_AzureRM(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "azurerm" {
    storage_account_name = "acmetfstate"
    container_name       = "tfstate"
    key                  = "prod/api.terraform.tfstate"
    resource_group_name  = "tfstate-rg"
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendAzureRM {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendAzureRM)
	}
	for k, want := range map[string]string{
		"storage_account_name": "acmetfstate",
		"container_name":       "tfstate",
		"key":                  "prod/api.terraform.tfstate",
		"resource_group_name":  "tfstate-rg",
	} {
		if got := b.Settings[k]; got != want {
			t.Errorf("azurerm.%s = %q, want %q", k, got, want)
		}
	}
}

func TestDetectBackend_NoBackendBlockIsLocal(t *testing.T) {
	// Module with only resources / variables: terraform defaults to
	// local backend. We encode that as an explicit BackendLocal
	// rather than nil so the state-reader has one less branch.
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
resource "null_resource" "stub" {}
variable "x" {}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendLocal {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendLocal)
	}
	wantPath := filepath.Join(dir, "terraform.tfstate")
	if b.Settings["path"] != wantPath {
		t.Errorf("Settings[path] = %q, want %q", b.Settings["path"], wantPath)
	}
	if b.SourceFile != "" {
		t.Errorf("SourceFile should be empty for implicit local, got %q", b.SourceFile)
	}
}

func TestDetectBackend_ExplicitLocal(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "local" {
    path = "states/prod.tfstate"
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendLocal {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendLocal)
	}
	if b.Settings["path"] != "states/prod.tfstate" {
		t.Errorf("explicit local path: %+v", b.Settings)
	}
}

func TestDetectBackend_RemoteIsDetectedSeparately(t *testing.T) {
	// `backend "remote"` points at TFE/HCP. The Atlantis source plugin's
	// caller checks this and tells the operator to rerun with --source=tfe.
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "remote" {
    hostname     = "app.terraform.io"
    organization = "acme"
    workspaces {
      name = "api-prod"
    }
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendRemote {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendRemote)
	}
}

func TestDetectBackend_CloudIsDetected(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  cloud {
    organization = "acme"
    workspaces {
      tags = ["env:prod"]
    }
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendCloud {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendCloud)
	}
	if b.Settings["organization"] != "acme" {
		t.Errorf("cloud settings: %+v", b.Settings)
	}
}

func TestDetectBackend_UnknownBackendErrors(t *testing.T) {
	// terraform's own backend list is large (consul, etcd, http, ...).
	// We're explicit about what we support; anything else is a hard
	// error rather than a silent treat-as-local.
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "consul" {
    address = "localhost:8500"
    path    = "tfstate/api"
  }
}
`)
	_, err := DetectBackend(dir)
	if err == nil {
		t.Fatal("expected error for unsupported backend type")
	}
	if !strings.Contains(err.Error(), "consul") {
		t.Errorf("error should name the unsupported backend: %v", err)
	}
}

func TestDetectBackend_TwoFiles_BothDeclare(t *testing.T) {
	// terraform itself rejects this: more than one backend block
	// across the module's .tf files. We surface ErrMultipleTerraformBlocks
	// regardless of whether the kinds agree or differ.
	dir := t.TempDir()
	writeTF(t, dir, "a.tf", `
terraform {
  backend "s3" {
    bucket = "a"
  }
}
`)
	writeTF(t, dir, "b.tf", `
terraform {
  backend "s3" {
    bucket = "b"
  }
}
`)
	_, err := DetectBackend(dir)
	if !errors.Is(err, ErrMultipleTerraformBlocks) {
		t.Errorf("expected ErrMultipleTerraformBlocks, got: %v", err)
	}
}

func TestDetectBackend_TwoFiles_ConflictingKinds(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "a.tf", `
terraform {
  backend "s3" {
    bucket = "a"
  }
}
`)
	writeTF(t, dir, "b.tf", `
terraform {
  backend "gcs" {
    bucket = "b"
  }
}
`)
	_, err := DetectBackend(dir)
	if !errors.Is(err, ErrConflictingBackends) {
		t.Errorf("expected ErrConflictingBackends, got: %v", err)
	}
	// Error message names both files so operators can locate them.
	if !strings.Contains(err.Error(), "a.tf") || !strings.Contains(err.Error(), "b.tf") {
		t.Errorf("error should name both files, got: %v", err)
	}
}

func TestDetectBackend_BackendWithoutLabel(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend {
    bucket = "x"
  }
}
`)
	_, err := DetectBackend(dir)
	if err == nil {
		t.Fatal("expected error for backend without label")
	}
	if !strings.Contains(err.Error(), "exactly one label") {
		t.Errorf("error should mention label requirement: %v", err)
	}
}

func TestDetectBackend_HCLParseError(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "broken.tf", `
terraform { backend "s3"
`)
	_, err := DetectBackend(dir)
	if err == nil {
		t.Fatal("expected parse error")
	}
	if !strings.Contains(err.Error(), "parse") {
		t.Errorf("error should mention 'parse': %v", err)
	}
}

func TestDetectBackend_NonTfFilesIgnored(t *testing.T) {
	// Module dirs often have README.md / .gitignore / etc. Make sure
	// they don't confuse the scanner.
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "s3" { bucket = "x" }
}
`)
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("hi"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "main.tf.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".gitignore"), []byte("*"), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Kind != BackendS3 {
		t.Errorf("Kind = %q, want %q", b.Kind, BackendS3)
	}
}

func TestDetectBackend_NumericLiteral(t *testing.T) {
	// Some backends accept numeric settings (workspace_key_prefix
	// nesting depths, port numbers for self-hosted). Make sure we
	// stringify numbers without losing them.
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "s3" {
    bucket  = "x"
    max_retries = 5
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Settings["max_retries"] == "" {
		t.Errorf("numeric attribute lost: %+v", b.Settings)
	}
}

func TestDetectBackend_BoolLiteral(t *testing.T) {
	dir := t.TempDir()
	writeTF(t, dir, "main.tf", `
terraform {
  backend "s3" {
    bucket  = "x"
    encrypt = true
  }
}
`)
	b, err := DetectBackend(dir)
	if err != nil {
		t.Fatalf("DetectBackend: %v", err)
	}
	if b.Settings["encrypt"] != "true" {
		t.Errorf("bool attribute: %q, want %q", b.Settings["encrypt"], "true")
	}
}

func TestDetectBackend_MissingDirErrors(t *testing.T) {
	_, err := DetectBackend("/nonexistent/dir/should/not/exist")
	if err == nil {
		t.Fatal("expected error for missing dir")
	}
}
