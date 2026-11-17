package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// Operator uploads for the two caches that are NOT package ecosystems (#2599).
//
// These are the artifacts a sealed deployment most needs and can least do
// without: the engine binary, without which it cannot run at all, and the
// providers, without which `init` cannot resolve. Both are separate caches with
// their own routes, so they are not reachable through the package-cache upload
// methods and are not a matter of passing a different ecosystem string.
//
// The provider is the only upload whose bytes the CONSUMER verifies: `init`
// checks the `h1:` hash against the lock file. The SERVER computes that hash
// from the archive and never accepts it from the caller, so there is no hash
// argument here and adding one would be the hole the design closes -- a caller
// able to supply the hash alongside the bytes could supply a hash matching
// bytes nobody vetted, and `init` would accept them against a lock file that
// looks perfectly correct.
//
// `PackageUploadOptions` is shared rather than duplicated: `Reference` and
// `Replace` mean the same thing for every cache, and `Replace` carries the same
// warning -- what a replacement displaces may be what a run already depends on,
// and on a sealed node nothing can fetch the old bytes back.
//
// NOTE ON COVERAGE: the SDK models these two caches only for uploading. There
// is no `ListBinaryCache` or `ListProviderCache`, which predates this file --
// `binary_cache.go` carries warm-bulk alone. So `CachedBinaryRow` and
// `CachedProviderRow` describe what an UPLOAD returns, not the full listing
// shape, and should not be mistaken for complete coverage of either cache.

const (
	binaryUploadPath   = "/api/v1/admin/binary-cache/artifacts"
	providerUploadPath = "/api/v1/admin/provider-cache/artifacts"
)

// CachedBinaryRow is the engine-binary cache row an upload returned.
type CachedBinaryRow struct {
	ID      string `json:"-"`
	Tool    string `json:"tool"`
	Version string `json:"version"`
	OS      string `json:"os"`
	Arch    string `json:"arch"`
	Shasum  string `json:"shasum"`
	// Attestation records HOW the artifact was admitted. `operator-upload` here
	// means a person vouched for it, in place of the publisher's signed
	// SHA256SUMS that the fetch path checks -- which for a binary EXECUTED on
	// every run is the most consequential thing the row says.
	Attestation     string `json:"attestation"`
	UploadedBy      string `json:"uploaded-by"`
	UploadReference string `json:"upload-reference"`
	CachedAt        string `json:"cached-at"`
}

// CachedProviderRow is the provider cache row an upload returned.
type CachedProviderRow struct {
	ID        string `json:"-"`
	Hostname  string `json:"hostname"`
	Namespace string `json:"namespace"`
	// ProviderType is `provider-type` on the wire, not `type`: in JSON:API the
	// resource's own `type` is a reserved member of the resource object, so an
	// attribute of the same name is ambiguous to a client walking the document.
	// The listing route has always used this spelling and the upload matches it,
	// because one resource must not go by two names depending on which route
	// returned it.
	ProviderType string `json:"provider-type"`
	Version      string `json:"version"`
	OS           string `json:"os"`
	Arch         string `json:"arch"`
	Filename     string `json:"filename"`
	Shasum       string `json:"shasum"`
	// H1Hash is what `terraform init` checks against the lock file. Computed by
	// the server from the uploaded archive, never supplied by the caller.
	H1Hash          string `json:"h1-hash"`
	Attestation     string `json:"attestation"`
	UploadedBy      string `json:"uploaded-by"`
	UploadReference string `json:"upload-reference"`
	CachedAt        string `json:"cached-at"`
}

// uploadMeta is what the write DID, which the row itself cannot say: an
// identical re-upload and a first upload describe the same row.
type uploadMeta struct {
	Replaced       bool
	Unchanged      bool
	PreviousDigest string
}

func parseUploadMeta(data []byte) uploadMeta {
	var envelope struct {
		Meta struct {
			Replaced       bool   `json:"replaced"`
			Unchanged      bool   `json:"unchanged"`
			PreviousDigest string `json:"previous-digest"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return uploadMeta{}
	}
	return uploadMeta{
		Replaced:       envelope.Meta.Replaced,
		Unchanged:      envelope.Meta.Unchanged,
		PreviousDigest: envelope.Meta.PreviousDigest,
	}
}

// BinaryUpload is what the server made of an engine-binary upload.
type BinaryUpload struct {
	Binary CachedBinaryRow
	// Replaced is true when the upload displaced bytes already held at these
	// coordinates, which only happens with Replace set.
	Replaced bool
	// Unchanged is true when the uploaded bytes were identical to what was
	// already there, so nothing was destroyed and nothing changed.
	Unchanged bool
	// PreviousDigest is the digest of what the upload displaced. Empty unless
	// Replaced.
	PreviousDigest string
}

// ProviderUpload is what the server made of a provider upload.
type ProviderUpload struct {
	Provider       CachedProviderRow
	Replaced       bool
	Unchanged      bool
	PreviousDigest string
}

// UploadEngineBinary uploads a vetted engine or platform-tool binary: `tofu`,
// `terraform`, `terragrunt`, `opa`, `trivy`, `checkov`. An existing artifact at
// these coordinates is a *ConflictError unless opts.Replace is set.
func (c *Client) UploadEngineBinary(
	ctx context.Context,
	tool, version, osName, arch, fileName string,
	content []byte,
	opts *PackageUploadOptions,
) (*BinaryUpload, error) {
	fields := opts.fields()
	fields["tool"] = tool
	fields["version"] = version
	// `os`, not `os_`: the route declares the Python parameter `os_` with an
	// `alias="os"`, and the alias is what the wire carries.
	fields["os"] = osName
	fields["arch"] = arch

	data, err := c.PostMultipart(ctx, binaryUploadPath, fields, "file", fileName, content)
	if err != nil {
		return nil, err
	}
	resource, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse binary upload response: %w", err)
	}
	out := &BinaryUpload{Binary: CachedBinaryRow{ID: resource.ID}}
	if err := decodeResourceAttributes(resource.Attributes, &out.Binary); err != nil {
		return nil, fmt.Errorf("decode uploaded binary %q: %w", resource.ID, err)
	}
	meta := parseUploadMeta(data)
	out.Replaced, out.Unchanged, out.PreviousDigest = meta.Replaced, meta.Unchanged, meta.PreviousDigest
	return out, nil
}

// UploadProviderPackage uploads a vetted provider archive. The archive must be
// a real zip: the server reads it to compute the `h1:` hash `init` checks, and
// refuses anything it cannot read rather than storing bytes no engine can
// install.
//
// `providerType` is the provider's type, e.g. `aws`.
func (c *Client) UploadProviderPackage(
	ctx context.Context,
	hostname, namespace, providerType, version, osName, arch, fileName string,
	content []byte,
	opts *PackageUploadOptions,
) (*ProviderUpload, error) {
	fields := opts.fields()
	fields["hostname"] = hostname
	fields["namespace"] = namespace
	// `type`, not `type_`, for the same alias reason as `os` above.
	fields["type"] = providerType
	fields["version"] = version
	fields["os"] = osName
	fields["arch"] = arch

	data, err := c.PostMultipart(ctx, providerUploadPath, fields, "file", fileName, content)
	if err != nil {
		return nil, err
	}
	resource, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse provider upload response: %w", err)
	}
	out := &ProviderUpload{Provider: CachedProviderRow{ID: resource.ID}}
	if err := decodeResourceAttributes(resource.Attributes, &out.Provider); err != nil {
		return nil, fmt.Errorf("decode uploaded provider %q: %w", resource.ID, err)
	}
	meta := parseUploadMeta(data)
	out.Replaced, out.Unchanged, out.PreviousDigest = meta.Replaced, meta.Unchanged, meta.PreviousDigest
	return out, nil
}
