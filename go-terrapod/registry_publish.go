package terrapod

import (
	"context"
	"fmt"
)

// Registry publishing — client-signed, direct uploads.
//
// Providers are published in three steps, in order: the SHA256SUMS manifest,
// its detached signature (verified server-side against a registered GPG key —
// the trust gate), then each platform zip (validated against the signed
// manifest as it is received). The server never re-signs; the publisher owns
// the signature. Modules are a single tarball upload (the server extracts the
// interface and triggers linked-workspace impact runs).
//
// All uploads go to the authenticated Terrapod API (not presigned object
// storage), so the bearer token is required and the request Content-Type is
// informational only.

const (
	providerPublishBase = "/api/terrapod/v1/registry-providers/private/default"
	modulePublishBase   = "/api/terrapod/v1/registry-modules/private/default"
)

// UploadProviderSHASUMS uploads the client-built SHA256SUMS manifest for a
// provider version (the first publish step). Upserts the version.
func (c *Client) UploadProviderSHASUMS(ctx context.Context, name, version string, shasums []byte) error {
	path := fmt.Sprintf("%s/%s/versions/%s/shasums", providerPublishBase, name, version)
	_, err := c.PutRaw(ctx, path, "text/plain", shasums)
	return err
}

// UploadProviderSignature uploads the detached SHA256SUMS signature and
// triggers server-side verification against a registered GPG key. Returns a
// *ValidationError (HTTP 422) if the signature is missing its manifest, is from
// an unregistered key, or fails verification.
func (c *Client) UploadProviderSignature(ctx context.Context, name, version string, sig []byte) error {
	path := fmt.Sprintf("%s/%s/versions/%s/shasums.sig", providerPublishBase, name, version)
	_, err := c.PutRaw(ctx, path, "application/pgp-signature", sig)
	return err
}

// UploadProviderPlatform uploads one built+zipped provider platform binary. The
// server validates the zip's sha against the signed manifest before accepting
// it; a mismatch (or uploading before the signature is verified) returns a
// *ValidationError (HTTP 422). goos/goarch are the Go OS/arch (e.g. linux,
// arm64).
func (c *Client) UploadProviderPlatform(ctx context.Context, name, version, goos, goarch string, zip []byte) error {
	path := fmt.Sprintf("%s/%s/versions/%s/platforms/%s/%s", providerPublishBase, name, version, goos, goarch)
	_, err := c.PutRaw(ctx, path, "application/zip", zip)
	return err
}

// UploadModuleVersion uploads a gzipped module source tarball for a version.
// Upserts the version; the server extracts the module interface and triggers
// runs on linked workspaces.
func (c *Client) UploadModuleVersion(ctx context.Context, name, provider, version string, tarball []byte) error {
	path := fmt.Sprintf("%s/%s/%s/versions/%s/upload", modulePublishBase, name, provider, version)
	_, err := c.PutRaw(ctx, path, "application/gzip", tarball)
	return err
}
