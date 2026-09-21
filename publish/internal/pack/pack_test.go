package pack

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestProviderZipLayout(t *testing.T) {
	bin := []byte("the binary")
	z, err := ProviderZip("example", "1.0.0", "linux", bin)
	if err != nil {
		t.Fatal(err)
	}
	zr, err := zip.NewReader(bytes.NewReader(z), int64(len(z)))
	if err != nil {
		t.Fatal(err)
	}
	if len(zr.File) != 1 {
		t.Fatalf("want 1 entry, got %d", len(zr.File))
	}
	f := zr.File[0]
	if f.Name != "terraform-provider-example_v1.0.0" {
		t.Errorf("inner name = %s", f.Name)
	}
	if f.Mode()&0o100 == 0 {
		t.Errorf("inner entry not executable: %v", f.Mode())
	}
	rc, _ := f.Open()
	got, _ := io.ReadAll(rc)
	if err := rc.Close(); err != nil {
		t.Fatal(err)
	}
	if string(got) != string(bin) {
		t.Errorf("content = %q", got)
	}
}

func TestProviderZipWindowsExe(t *testing.T) {
	z, _ := ProviderZip("example", "1.0.0", "windows", []byte("x"))
	zr, _ := zip.NewReader(bytes.NewReader(z), int64(len(z)))
	if zr.File[0].Name != "terraform-provider-example_v1.0.0.exe" {
		t.Errorf("windows inner name = %s", zr.File[0].Name)
	}
}

func TestSHA256SUMSFormatAndOrder(t *testing.T) {
	files := map[string][]byte{
		"b.zip": []byte("bbb"),
		"a.zip": []byte("aaa"),
	}
	out := string(SHA256SUMS(files))
	wantA := fmt.Sprintf("%x  a.zip\n", sha256.Sum256([]byte("aaa")))
	wantB := fmt.Sprintf("%x  b.zip\n", sha256.Sum256([]byte("bbb")))
	if out != wantA+wantB {
		t.Errorf("manifest = %q, want sorted a then b", out)
	}
}

func TestTarGzDirRoundTrip(t *testing.T) {
	dir := t.TempDir()
	mustWrite := func(name string, data []byte) {
		t.Helper()
		if err := os.WriteFile(name, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mustMkdir := func(name string) {
		t.Helper()
		if err := os.MkdirAll(name, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	mustWrite(filepath.Join(dir, "main.tf"), []byte("resource {}"))
	mustMkdir(filepath.Join(dir, ".git"))
	mustWrite(filepath.Join(dir, ".git", "HEAD"), []byte("ref"))
	mustMkdir(filepath.Join(dir, "sub"))
	mustWrite(filepath.Join(dir, "sub", "vars.tf"), []byte("variable {}"))

	gz, _, err := TarGzDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	gr, _ := gzip.NewReader(bytes.NewReader(gz))
	tr := tar.NewReader(gr)
	found := map[string]bool{}
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		found[h.Name] = true
	}
	if !found["main.tf"] || !found["sub/vars.tf"] {
		t.Errorf("missing expected entries: %v", found)
	}
	for n := range found {
		if n == ".git" || n == ".git/HEAD" {
			t.Errorf(".git should be skipped, found %s", n)
		}
	}
}

// TestTarGzDirExcludesSecretBearingFiles pins GHSA-g2c3-vxf6-7jr8.
//
// `.terraform` was the only state-adjacent exclusion, and it is the wrong one:
// a local backend writes `terraform.tfstate` to the module ROOT. Running
// `terraform apply` while developing a module — the normal way to test one —
// therefore published that environment's state to an immutable registry
// version. The `.tf` entries are controls: the filter must not swallow the
// module itself.
func TestTarGzDirExcludesSecretBearingFiles(t *testing.T) {
	dir := t.TempDir()
	write := func(rel, data string) {
		t.Helper()
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	mustBeExcluded := []string{
		"terraform.tfstate",
		"terraform.tfstate.backup",
		"terraform.tfvars",
		"prod.auto.tfvars",
		"prod.auto.tfvars.json",
		".env",
		".terraform.tfstate.lock.info",
		"modules/inner/dev.tfvars", // nested, not just the root
	}
	mustBeKept := []string{
		"main.tf",
		"variables.tf",
		"README.md",
		"modules/inner/main.tf",
		"examples/basic/main.tf",
	}
	for _, n := range mustBeExcluded {
		write(n, "SECRET-"+n)
	}
	for _, n := range mustBeKept {
		write(n, "# "+n)
	}

	gz, _, err := TarGzDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	gr, err := gzip.NewReader(bytes.NewReader(gz))
	if err != nil {
		t.Fatal(err)
	}
	entries := map[string]bool{}
	tr := tar.NewReader(gr)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		entries[h.Name] = true
	}

	for _, n := range mustBeExcluded {
		if entries[n] {
			t.Errorf("secret-bearing file was packaged for publication: %s", n)
		}
	}
	for _, n := range mustBeKept {
		if !entries[n] {
			t.Errorf("the filter dropped module source: %s (entries: %v)", n, entries)
		}
	}
}

// The exclusion table, pinned at its EDGES rather than transcribed from the
// switch. The review's point was that listing exactly the implemented clauses
// proves only "the code does what the code does"; the value is in the cases a
// developer plausibly has lying in a module directory.
func TestExcludedFileCoversTheRealAccidents(t *testing.T) {
	mustExclude := []string{
		".env", ".env.local", ".env.production", ".envrc",
		".netrc", ".terraformrc", "credentials.tfrc.json", "credentials",
		"terraform.tfstate", "terraform.tfstate.backup", "prod.tfstate",
		"terraform.tfstate.1700000000.backup", ".terraform.tfstate.lock.info",
		"terraform.tfvars", "prod.auto.tfvars", "vars.tfvars.json",
		"Terraform.TFVars", "TERRAFORM.TFSTATE", // case-insensitive filesystems
		"key.pem", "id_rsa", "id_ed25519",
	}
	mustKeep := []string{
		"main.tf", "variables.tf", "outputs.tf", "versions.tf",
		"README.md", "LICENSE", "terraform.tfvars.example", "example.tf",
	}
	for _, n := range mustExclude {
		if !excludedFile(n) {
			t.Errorf("%q would be published", n)
		}
	}
	for _, n := range mustKeep {
		if excludedFile(n) {
			t.Errorf("%q would be dropped from the module", n)
		}
	}
}

// Publishing is irreversible, so the author has to be told what was left out —
// the filter matches by name and will also catch a deliberately-shipped
// example tfvars fixture.
func TestTarGzDirReportsWhatItSkipped(t *testing.T) {
	dir := t.TempDir()
	for _, n := range []string{"main.tf", "terraform.tfvars", "examples/basic/dev.tfvars"} {
		p := filepath.Join(dir, filepath.FromSlash(n))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	_, skipped, err := TarGzDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(skipped) != 2 {
		t.Fatalf("expected 2 skipped files, got %v", skipped)
	}
	if skipped[0] != "examples/basic/dev.tfvars" || skipped[1] != "terraform.tfvars" {
		t.Errorf("skipped list wrong or unsorted: %v", skipped)
	}
}
