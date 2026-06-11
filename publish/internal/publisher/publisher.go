// Package publisher orchestrates a registry publish over the go-terrapod SDK:
// build artifacts (pack), sign the manifest (sign), then upload in the order
// the server validates.
package publisher

import (
	"context"
	"fmt"
	"strings"

	"github.com/ProtonMail/go-crypto/openpgp"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/publish/internal/pack"
	"github.com/mattrobinsonsre/terrapod/publish/internal/sign"
)

// ProviderInput describes a provider version to publish.
type ProviderInput struct {
	Name       string
	Version    string
	SigningKey *openpgp.Entity
	// Binaries maps "<goos>/<goarch>" to the raw, already-built provider binary.
	Binaries map[string][]byte
}

// Progress receives one human-readable line per publish step (may be nil).
type Progress func(string)

func note(p Progress, format string, a ...any) {
	if p != nil {
		p(fmt.Sprintf(format, a...))
	}
}

// PublishProvider zips each platform binary, builds + signs the SHA256SUMS
// manifest, and uploads in the order the server enforces: manifest, then the
// signature (the trust gate — the server verifies it against the registered
// key before accepting binaries), then each platform zip (validated against
// the signed manifest as it is received). It never sends the private key; only
// the detached signature crosses the wire.
func PublishProvider(ctx context.Context, c *terrapod.Client, in ProviderInput, p Progress) error {
	if len(in.Binaries) == 0 {
		return fmt.Errorf("no platform binaries to publish")
	}
	zips := make(map[string][]byte, len(in.Binaries)) // platform -> zip
	files := make(map[string][]byte, len(in.Binaries)) // filename -> zip (for the manifest)
	for platform, bin := range in.Binaries {
		goos, goarch, err := splitPlatform(platform)
		if err != nil {
			return err
		}
		z, err := pack.ProviderZip(in.Name, in.Version, goos, bin)
		if err != nil {
			return fmt.Errorf("zip %s: %w", platform, err)
		}
		zips[platform] = z
		files[pack.ProviderZipName(in.Name, in.Version, goos, goarch)] = z
	}

	manifest := pack.SHA256SUMS(files)
	sig, err := sign.DetachSign(in.SigningKey, manifest)
	if err != nil {
		return fmt.Errorf("sign SHA256SUMS: %w", err)
	}

	note(p, "uploading SHA256SUMS (%d platforms)", len(files))
	if err := c.UploadProviderSHASUMS(ctx, in.Name, in.Version, manifest); err != nil {
		return fmt.Errorf("upload SHA256SUMS: %w", err)
	}
	note(p, "uploading + verifying signature (key %s)", sign.KeyID(in.SigningKey))
	if err := c.UploadProviderSignature(ctx, in.Name, in.Version, sig); err != nil {
		return fmt.Errorf("upload signature: %w", err)
	}
	for platform, z := range zips {
		goos, goarch, _ := splitPlatform(platform)
		note(p, "uploading %s (%d bytes)", platform, len(z))
		if err := c.UploadProviderPlatform(ctx, in.Name, in.Version, goos, goarch, z); err != nil {
			return fmt.Errorf("upload %s: %w", platform, err)
		}
	}
	note(p, "published %s %s", in.Name, in.Version)
	return nil
}

// PublishModule tars + gzips a module source directory and uploads it. The
// server extracts the module interface and triggers linked-workspace runs.
func PublishModule(ctx context.Context, c *terrapod.Client, name, provider, version, sourceDir string, p Progress) error {
	note(p, "packing %s", sourceDir)
	tarball, err := pack.TarGzDir(sourceDir)
	if err != nil {
		return fmt.Errorf("pack %s: %w", sourceDir, err)
	}
	note(p, "uploading module tarball (%d bytes)", len(tarball))
	if err := c.UploadModuleVersion(ctx, name, provider, version, tarball); err != nil {
		return fmt.Errorf("upload module: %w", err)
	}
	note(p, "published %s/%s %s", name, provider, version)
	return nil
}

func splitPlatform(s string) (goos, goarch string, err error) {
	parts := strings.SplitN(s, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", fmt.Errorf("invalid platform %q, want <goos>/<goarch>", s)
	}
	return parts[0], parts[1], nil
}
