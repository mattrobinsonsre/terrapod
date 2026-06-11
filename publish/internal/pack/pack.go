// Package pack builds the on-the-wire artifacts for a Terrapod registry
// publish — provider platform zips, the SHA256SUMS manifest, and gzipped
// module source tarballs — using only the Go standard library (no shelling
// out to zip/tar/sha256sum).
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
	"sort"
)

// ProviderZip builds a terraform-provider distribution zip in memory. The zip
// holds a single entry, the binary, named `terraform-provider-<name>_v<version>`
// (with `.exe` on windows) and marked executable (0755) — the layout
// terraform/tofu expect when installing from a network mirror.
func ProviderZip(name, version, goos string, binary []byte) ([]byte, error) {
	inner := fmt.Sprintf("terraform-provider-%s_v%s", name, version)
	if goos == "windows" {
		inner += ".exe"
	}
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	hdr := &zip.FileHeader{Name: inner, Method: zip.Deflate}
	hdr.SetMode(0o755)
	w, err := zw.CreateHeader(hdr)
	if err != nil {
		return nil, err
	}
	if _, err := w.Write(binary); err != nil {
		return nil, err
	}
	if err := zw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// ProviderZipName is the canonical registry filename for a platform zip — it
// MUST match the name the server reconstructs and the entry in SHA256SUMS.
func ProviderZipName(name, version, goos, goarch string) string {
	return fmt.Sprintf("terraform-provider-%s_%s_%s_%s.zip", name, version, goos, goarch)
}

// SHA256SUMS renders a manifest over the given files (filename -> contents) in
// the `<hex-sha256>  <filename>\n` format, sorted by filename for determinism.
func SHA256SUMS(files map[string][]byte) []byte {
	names := make([]string, 0, len(files))
	for n := range files {
		names = append(names, n)
	}
	sort.Strings(names)
	var buf bytes.Buffer
	for _, n := range names {
		sum := sha256.Sum256(files[n])
		fmt.Fprintf(&buf, "%x  %s\n", sum, n)
	}
	return buf.Bytes()
}

// TarGzDir builds a gzipped tar of a module source directory. Entry paths are
// relative to dir (forward slashes). `.git` and `.terraform` directories are
// skipped; symlinks and other non-regular files are ignored.
func TarGzDir(dir string) ([]byte, error) {
	var buf bytes.Buffer
	gw := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gw)

	walkErr := filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		if info.IsDir() {
			if info.Name() == ".git" || info.Name() == ".terraform" {
				return filepath.SkipDir
			}
		}
		hdr, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(rel)
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = io.Copy(tw, f)
		return err
	})
	if walkErr != nil {
		return nil, walkErr
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	if err := gw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
