package terrapod

import (
	"context"
	"testing"
)

// The two non-package caches (#2599). Each method is a mapping from typed
// arguments onto a multipart body, so a method that posted a well-formed body
// to the wrong PATH, or a coordinate under the wrong FIELD NAME, would still
// return 201 against a permissive server and store the artifact where no
// install ever asks for it. `uploadServer` (package_cache_uploads_test.go)
// captures both, which is why these assert on the capture rather than only on
// the decoded result.

const binaryUploadBody = `{
  "data": {
    "id": "bin-1",
    "type": "cached-binaries",
    "attributes": {
      "tool": "tofu", "version": "1.9.0", "os": "linux", "arch": "arm64",
      "shasum": "abc123", "attestation": "operator-upload",
      "uploaded-by": "admin@example.com", "upload-reference": "CHG-7",
      "cached-at": "2026-11-17T00:00:00Z"
    }
  },
  "meta": {"replaced": false, "unchanged": false}
}`

func TestUploadEngineBinaryPostsTheRightCoordinates(t *testing.T) {
	c, got := uploadServer(t, binaryUploadBody)
	up, err := c.UploadEngineBinary(context.Background(),
		"tofu", "1.9.0", "linux", "arm64", "tofu_1.9.0_linux_arm64.zip",
		[]byte("binary bytes"), &PackageUploadOptions{Reference: "CHG-7"})
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	if got.path != "/api/v1/admin/binary-cache/artifacts" {
		t.Errorf("posted to %q, not the binary cache", got.path)
	}
	// `os`, not `os_`: the route's Python parameter is `os_` with an
	// `alias="os"`, and posting the parameter name is a 422 whose message is
	// about the form rather than about the name that caused it.
	for field, want := range map[string]string{
		"tool": "tofu", "version": "1.9.0", "os": "linux", "arch": "arm64",
		"reference": "CHG-7",
	} {
		if got.fields[field] != want {
			t.Errorf("field %q = %q, want %q", field, got.fields[field], want)
		}
	}
	if _, ok := got.fields["os_"]; ok {
		t.Error("posted the Python parameter name `os_` instead of the wire alias `os`")
	}
	if got.fileName != "tofu_1.9.0_linux_arm64.zip" {
		t.Errorf("file name %q", got.fileName)
	}
	if up.Binary.ID != "bin-1" || up.Binary.Attestation != "operator-upload" {
		t.Errorf("decoded %+v", up.Binary)
	}
	if up.Binary.UploadReference != "CHG-7" {
		t.Errorf("upload-reference decoded as %q", up.Binary.UploadReference)
	}
}

const providerUploadBody = `{
  "data": {
    "id": "prov-1",
    "type": "cached-providers",
    "attributes": {
      "hostname": "registry.terraform.io", "namespace": "hashicorp",
      "provider-type": "aws", "version": "5.0.0", "os": "linux", "arch": "amd64",
      "filename": "terraform-provider-aws_5.0.0_linux_amd64.zip",
      "shasum": "def456", "h1-hash": "h1:deadbeef=",
      "attestation": "operator-upload", "uploaded-by": "admin@example.com",
      "upload-reference": "", "cached-at": "2026-11-17T00:00:00Z"
    }
  },
  "meta": {"replaced": true, "unchanged": false, "previous-digest": "sha256:old"}
}`

func TestUploadProviderPackagePostsTheRightCoordinates(t *testing.T) {
	c, got := uploadServer(t, providerUploadBody)
	up, err := c.UploadProviderPackage(context.Background(),
		"registry.terraform.io", "hashicorp", "aws", "5.0.0", "linux", "amd64",
		"terraform-provider-aws_5.0.0_linux_amd64.zip",
		[]byte("zip bytes"), &PackageUploadOptions{Replace: true})
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	if got.path != "/api/v1/admin/provider-cache/artifacts" {
		t.Errorf("posted to %q, not the provider cache", got.path)
	}
	for field, want := range map[string]string{
		"hostname": "registry.terraform.io", "namespace": "hashicorp",
		"type": "aws", "version": "5.0.0", "os": "linux", "arch": "amd64",
		"replace": "true",
	} {
		if got.fields[field] != want {
			t.Errorf("field %q = %q, want %q", field, got.fields[field], want)
		}
	}
	// The wire field is `type` (the route's alias for `type_`) while the
	// RESPONSE attribute is `provider-type`. The asymmetry is deliberate and is
	// exactly the kind of thing a round-trip assertion has to pin: posting
	// `provider-type` would drop the coordinate entirely.
	if _, ok := got.fields["provider-type"]; ok {
		t.Error("posted `provider-type` as a form field; the route reads `type`")
	}
	if up.Provider.ProviderType != "aws" {
		t.Errorf("provider-type decoded as %q, want aws", up.Provider.ProviderType)
	}
	// The hash the consumer checks, computed by the server.
	if up.Provider.H1Hash != "h1:deadbeef=" {
		t.Errorf("h1-hash decoded as %q", up.Provider.H1Hash)
	}
	if !up.Replaced || up.PreviousDigest != "sha256:old" {
		t.Errorf("meta not decoded: replaced=%v previous=%q", up.Replaced, up.PreviousDigest)
	}
}

func TestNeitherUploadSendsAnH1ItWasGiven(t *testing.T) {
	// The server computes the `h1:` hash from the archive and never takes it
	// from the caller: a caller able to supply it could supply a hash matching
	// bytes nobody vetted, and `init` would accept them against a lock file
	// that looks correct. There is no argument for it, so the assertion is that
	// no such field reaches the wire however the method is called.
	c, got := uploadServer(t, providerUploadBody)
	if _, err := c.UploadProviderPackage(context.Background(),
		"registry.terraform.io", "hashicorp", "aws", "5.0.0", "linux", "amd64",
		"p.zip", []byte("zip"), nil); err != nil {
		t.Fatalf("upload: %v", err)
	}
	for _, forbidden := range []string{"h1", "h1-hash", "h1_hash", "shasum", "digest"} {
		if _, ok := got.fields[forbidden]; ok {
			t.Errorf("the client supplied %q; the server must derive it", forbidden)
		}
	}
}

func TestUploadOptionsAreOmittedWhenUnset(t *testing.T) {
	// A nil options pointer must not post empty `reference`/`replace` fields:
	// `replace=""` is a value the server has to interpret, and an empty
	// reference would overwrite a stored one on a replace.
	c, got := uploadServer(t, binaryUploadBody)
	if _, err := c.UploadEngineBinary(context.Background(),
		"opa", "1.0.0", "darwin", "arm64", "opa", []byte("x"), nil); err != nil {
		t.Fatalf("upload: %v", err)
	}
	for _, absent := range []string{"reference", "replace"} {
		if _, ok := got.fields[absent]; ok {
			t.Errorf("posted %q with nil options", absent)
		}
	}
}
