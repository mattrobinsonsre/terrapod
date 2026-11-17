package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
)

// Operator uploads — putting a vetted artifact into the package cache.
//
// A third posture beside the pull-through and the warm, and the only one that
// works while the node is SEALED: an operator who has already approved an
// artifact asserts that it may be served. So no publisher signature is required
// and requiring one would defeat the feature, because the likeliest upload is
// something built, patched or re-packaged in-house that no upstream key covers.
// What stands in for the signature is PROVENANCE — the row records
// `operator-upload`, who uploaded it, and an optional reference of their own.
//
// One method per cache rather than one taking an ecosystem string, because each
// ecosystem's coordinates genuinely differ and a single signature would have to
// take them as a map, pushing the server's refusal to the only place a caller
// cannot see it: run time.
//
// INDEX DOCUMENTS are the other half, and skipping them is the easy mistake. An
// artifact upload supplies bytes; npm, Galaxy and NuGet answer "what versions
// exist" from a cached index document and nowhere else, so an uploaded artifact
// with no document beside it is present, recorded, and unresolvable on exactly
// the sealed node it was uploaded for.

const packageUploadBase = "/api/v1/admin/package-cache"

// PackageUploadOptions carries what is common to every upload.
type PackageUploadOptions struct {
	// Reference is the operator's own ticket, change or approval id. Free text,
	// stored verbatim, and the thing that makes the row answerable in an audit.
	Reference string
	// Replace permits overwriting artifact bytes already held at these
	// coordinates. Without it an existing artifact is a *ConflictError: what a
	// replacement displaces may be what a run already depends on, and on a
	// sealed node nothing can fetch the old bytes back.
	Replace bool
}

func (o *PackageUploadOptions) fields() map[string]string {
	out := map[string]string{}
	if o == nil {
		return out
	}
	if o.Reference != "" {
		out["reference"] = o.Reference
	}
	if o.Replace {
		out["replace"] = strconv.FormatBool(true)
	}
	return out
}

// PackageUpload is what the server made of an upload.
type PackageUpload struct {
	// File is the resulting cache row, the same resource the listing shows.
	File CachedPackageFile
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

func (c *Client) upload(
	ctx context.Context,
	path string,
	coords map[string]string,
	fileName string,
	content []byte,
	opts *PackageUploadOptions,
) (*PackageUpload, error) {
	fields := opts.fields()
	for k, v := range coords {
		fields[k] = v
	}
	data, err := c.PostMultipart(ctx, path, fields, "file", fileName, content)
	if err != nil {
		return nil, err
	}
	resource, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse package upload response: %w", err)
	}
	out := &PackageUpload{File: CachedPackageFile{ID: resource.ID}}
	if err := decodeResourceAttributes(resource.Attributes, &out.File); err != nil {
		return nil, fmt.Errorf("decode uploaded package file %q: %w", resource.ID, err)
	}
	// The meta sits beside `data` and says what the write DID, which the
	// resource itself cannot: an identical re-upload and a first upload describe
	// the same row.
	var envelope struct {
		Meta struct {
			Replaced       bool   `json:"replaced"`
			Unchanged      bool   `json:"unchanged"`
			PreviousDigest string `json:"previous-digest"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(data, &envelope); err == nil {
		out.Replaced = envelope.Meta.Replaced
		out.Unchanged = envelope.Meta.Unchanged
		out.PreviousDigest = envelope.Meta.PreviousDigest
	}
	return out, nil
}

// UploadPyPIPackage uploads a vetted PyPI distribution. The filename is supplied
// rather than derived, because a wheel's name encodes its compatibility tags and
// only the builder knows them.
func (c *Client) UploadPyPIPackage(ctx context.Context, project, filename string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/pypi/artifacts",
		map[string]string{"project": project, "filename": filename}, filename, content, opts)
}

// UploadNPMPackage uploads a vetted npm tarball. The filename is DERIVED by the
// server from the name and version, because npm's own registry derives it and
// the serve path matches on the derived form.
//
// See UploadNPMIndex: without a packument a sealed node cannot resolve this.
func (c *Client) UploadNPMPackage(ctx context.Context, name, version string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/npm/artifacts",
		map[string]string{"name": name, "version": version}, "package.tgz", content, opts)
}

// UploadNuGetPackage uploads a vetted .nupkg. See UploadNuGetIndex.
func (c *Client) UploadNuGetPackage(ctx context.Context, packageID, version string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/nuget/artifacts",
		map[string]string{"package_id": packageID, "version": version}, "package.nupkg", content, opts)
}

// UploadGoModuleFile uploads ONE file of a Go module version. A module version
// is three files to the proxy protocol — ".info", ".mod" and ".zip" — with
// separate bytes, so each is uploaded in its own right exactly as each is
// fetched in its own right. Uploading the ".zip" alone will not satisfy
// `go mod download`.
func (c *Client) UploadGoModuleFile(ctx context.Context, module, version, suffix string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/go/artifacts",
		map[string]string{"module": module, "version": version, "suffix": suffix},
		"module"+suffix, content, opts)
}

// UploadGalaxyCollection uploads a vetted Ansible collection tarball. See
// UploadGalaxyIndex — Galaxy needs TWO index documents.
func (c *Client) UploadGalaxyCollection(ctx context.Context, namespace, name, version string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/galaxy/artifacts",
		map[string]string{"namespace": namespace, "name": name, "version": version},
		"collection.tar.gz", content, opts)
}

// UploadPulumiPlugin uploads a vetted Pulumi plugin tarball. kind is the plugin
// kind (resource, language, analyzer, converter); os/arch are Pulumi's own
// values (e.g. linux, amd64).
//
// Needs no index document: plugins are addressed by filename.
func (c *Client) UploadPulumiPlugin(ctx context.Context, kind, name, version, goos, arch string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/pulumi/artifacts",
		map[string]string{"kind": kind, "name": name, "version": version, "os": goos, "arch": arch},
		"plugin.tar.gz", content, opts)
}

// UploadNPMIndex uploads a vetted packument, which is what makes an uploaded npm
// tarball RESOLVABLE on a sealed node: the dependency ranges live in this
// document and in no other file, so without one the install is told the package
// is not cached.
//
// Refused with a *ValidationError if it is not valid JSON — a sealed node parses
// it on every read, so one that does not parse would fail every later
// resolution rather than this request.
func (c *Client) UploadNPMIndex(ctx context.Context, name string, document []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/npm/index",
		map[string]string{"name": name}, "packument.json", document, opts)
}

// UploadNuGetIndex uploads a vetted NuGet versions index, so a sealed node can
// restore an uploaded package.
func (c *Client) UploadNuGetIndex(ctx context.Context, packageID string, document []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/nuget/index",
		map[string]string{"package_id": packageID}, "index.json", document, opts)
}

// UploadGalaxyIndex uploads one of a collection's two index documents. document
// is "collection" (the collection record) or "versions" (the version list); a
// sealed node reads them separately, so one without the other leaves half the
// path answering 404.
func (c *Client) UploadGalaxyIndex(ctx context.Context, namespace, name, document string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/galaxy/index",
		map[string]string{"namespace": namespace, "name": name, "document": document},
		document+".json", content, opts)
}

// UploadGoIndex uploads a module index document. document is "list" (a
// newline-separated version list, served as text) or "latest" (JSON).
//
// Needed only to DISCOVER a version — `go get module` with no version, or
// `go list -m -versions`. A pinned `go mod download` asks for the three module
// files directly and never consults these.
func (c *Client) UploadGoIndex(ctx context.Context, module, document string, content []byte, opts *PackageUploadOptions) (*PackageUpload, error) {
	return c.upload(ctx, packageUploadBase+"/go/index",
		map[string]string{"module": module, "document": document}, "index", content, opts)
}
