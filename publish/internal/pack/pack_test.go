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
	z, err := ProviderZip("awsmai", "1.0.0", "linux", bin)
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
	if f.Name != "terraform-provider-awsmai_v1.0.0" {
		t.Errorf("inner name = %s", f.Name)
	}
	if f.Mode()&0o100 == 0 {
		t.Errorf("inner entry not executable: %v", f.Mode())
	}
	rc, _ := f.Open()
	got, _ := io.ReadAll(rc)
	rc.Close()
	if string(got) != string(bin) {
		t.Errorf("content = %q", got)
	}
}

func TestProviderZipWindowsExe(t *testing.T) {
	z, _ := ProviderZip("awsmai", "1.0.0", "windows", []byte("x"))
	zr, _ := zip.NewReader(bytes.NewReader(z), int64(len(z)))
	if zr.File[0].Name != "terraform-provider-awsmai_v1.0.0.exe" {
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
	os.WriteFile(filepath.Join(dir, "main.tf"), []byte("resource {}"), 0o644)
	os.MkdirAll(filepath.Join(dir, ".git"), 0o755)
	os.WriteFile(filepath.Join(dir, ".git", "HEAD"), []byte("ref"), 0o644)
	os.MkdirAll(filepath.Join(dir, "sub"), 0o755)
	os.WriteFile(filepath.Join(dir, "sub", "vars.tf"), []byte("variable {}"), 0o644)

	gz, err := TarGzDir(dir)
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
