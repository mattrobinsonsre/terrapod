package terrapod

import (
	"context"
	"encoding/json"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// capturedUpload is what the fake server saw: the path, the form fields and the file
// part. Asserting on these is the point -- every one of these methods is a
// mapping from typed arguments onto a multipart body, and a method that posted
// a well-formed body to the wrong path, or put a coordinate in the wrong field,
// would still return 201 against a permissive server.
type capturedUpload struct {
	path        string
	method      string
	fields      map[string]string
	fileName    string
	fileContent []byte
	contentType string
}

func uploadServer(t *testing.T, body string) (*Client, *capturedUpload) {
	t.Helper()
	got := &capturedUpload{fields: map[string]string{}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.path = r.URL.Path
		got.method = r.Method
		got.contentType = r.Header.Get("Content-Type")

		mediaType, params, err := mime.ParseMediaType(got.contentType)
		if err != nil || !strings.HasPrefix(mediaType, "multipart/") {
			t.Errorf("not a multipart request: %q (%v)", got.contentType, err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		mr := multipart.NewReader(r.Body, params["boundary"])
		for {
			part, err := mr.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatalf("read part: %v", err)
			}
			data, _ := io.ReadAll(part)
			if part.FormName() == "file" {
				got.fileName = part.FileName()
				got.fileContent = data
			} else {
				got.fields[part.FormName()] = string(data)
			}
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return newCacheClient(t, srv), got
}

const uploadBody = `{
  "data": {
    "type": "cached-package-files",
    "id": "f1",
    "attributes": {
      "ecosystem": "npm",
      "name": "@scope/pkg",
      "version": "1.3.0",
      "filename": "pkg-1.3.0.tgz",
      "size": 1234,
      "digest": "sha256:abc",
      "attestation": "operator-upload",
      "uploaded-by": "admin@example.com",
      "upload-reference": "CHG-1",
      "replaced-at": "2026-01-01T00:00:00Z",
      "replaced-digest": "sha256:old"
    }
  },
  "meta": {"replaced": true, "unchanged": false, "previous-digest": "sha256:old"}
}`

func TestUploadRoutesAndFields(t *testing.T) {
	// One case per method. The expectations are written out rather than derived
	// from the methods, which would assert only that they agree with themselves.
	cases := []struct {
		name   string
		call   func(*Client) (*PackageUpload, error)
		path   string
		fields map[string]string
	}{
		{
			name: "pypi artifact",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadPyPIPackage(context.Background(), "Flask", "flask-3.0.0-py3-none-any.whl", []byte("w"), nil)
			},
			path:   "/api/v1/admin/package-cache/pypi/artifacts",
			fields: map[string]string{"project": "Flask", "filename": "flask-3.0.0-py3-none-any.whl"},
		},
		{
			name: "npm artifact",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadNPMPackage(context.Background(), "@scope/pkg", "1.3.0", []byte("t"), nil)
			},
			path:   "/api/v1/admin/package-cache/npm/artifacts",
			fields: map[string]string{"name": "@scope/pkg", "version": "1.3.0"},
		},
		{
			name: "nuget artifact",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadNuGetPackage(context.Background(), "Newtonsoft.Json", "13.0.3", []byte("n"), nil)
			},
			path:   "/api/v1/admin/package-cache/nuget/artifacts",
			fields: map[string]string{"package_id": "Newtonsoft.Json", "version": "13.0.3"},
		},
		{
			name: "go module file",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadGoModuleFile(context.Background(), "github.com/foo/bar", "v1.2.3", ".zip", []byte("z"), nil)
			},
			path:   "/api/v1/admin/package-cache/go/artifacts",
			fields: map[string]string{"module": "github.com/foo/bar", "version": "v1.2.3", "suffix": ".zip"},
		},
		{
			name: "galaxy collection",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadGalaxyCollection(context.Background(), "community", "general", "9.2.0", []byte("g"), nil)
			},
			path:   "/api/v1/admin/package-cache/galaxy/artifacts",
			fields: map[string]string{"namespace": "community", "name": "general", "version": "9.2.0"},
		},
		{
			name: "pulumi plugin",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadPulumiPlugin(context.Background(), "resource", "aws", "6.0.0", "linux", "amd64", []byte("p"), nil)
			},
			path: "/api/v1/admin/package-cache/pulumi/artifacts",
			fields: map[string]string{
				"kind": "resource", "name": "aws", "version": "6.0.0", "os": "linux", "arch": "amd64",
			},
		},
		{
			name: "npm index",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadNPMIndex(context.Background(), "@scope/pkg", []byte(`{}`), nil)
			},
			path:   "/api/v1/admin/package-cache/npm/index",
			fields: map[string]string{"name": "@scope/pkg"},
		},
		{
			name: "nuget index",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadNuGetIndex(context.Background(), "Newtonsoft.Json", []byte(`{}`), nil)
			},
			path:   "/api/v1/admin/package-cache/nuget/index",
			fields: map[string]string{"package_id": "Newtonsoft.Json"},
		},
		{
			name: "galaxy index",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadGalaxyIndex(context.Background(), "community", "general", "versions", []byte(`{}`), nil)
			},
			path:   "/api/v1/admin/package-cache/galaxy/index",
			fields: map[string]string{"namespace": "community", "name": "general", "document": "versions"},
		},
		{
			name: "go index",
			call: func(c *Client) (*PackageUpload, error) {
				return c.UploadGoIndex(context.Background(), "github.com/foo/bar", "list", []byte("v1.0.0\n"), nil)
			},
			path:   "/api/v1/admin/package-cache/go/index",
			fields: map[string]string{"module": "github.com/foo/bar", "document": "list"},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			client, got := uploadServer(t, uploadBody)
			if _, err := tc.call(client); err != nil {
				t.Fatalf("upload: %v", err)
			}
			if got.method != http.MethodPost {
				t.Errorf("method = %q, want POST", got.method)
			}
			if got.path != tc.path {
				t.Errorf("path = %q, want %q", got.path, tc.path)
			}
			for k, want := range tc.fields {
				if got.fields[k] != want {
					t.Errorf("field %q = %q, want %q", k, got.fields[k], want)
				}
			}
			// No stray coordinate from another ecosystem: a method that sent an
			// extra field would be one whose signature does not match its route.
			for k := range got.fields {
				if _, expected := tc.fields[k]; !expected && k != "reference" && k != "replace" {
					t.Errorf("unexpected field %q = %q", k, got.fields[k])
				}
			}
		})
	}
}

func TestUploadSendsTheBytesAndTheOptions(t *testing.T) {
	client, got := uploadServer(t, uploadBody)
	content := []byte("the vetted bytes")

	_, err := client.UploadNPMPackage(context.Background(), "pkg", "1.0.0", content,
		&PackageUploadOptions{Reference: "CHG-42", Replace: true})
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	if string(got.fileContent) != string(content) {
		t.Errorf("file part = %q, want %q", got.fileContent, content)
	}
	if got.fields["reference"] != "CHG-42" {
		t.Errorf("reference = %q", got.fields["reference"])
	}
	if got.fields["replace"] != "true" {
		t.Errorf("replace = %q, want true", got.fields["replace"])
	}
}

func TestUploadOmitsTheOptionsItWasNotGiven(t *testing.T) {
	// `replace` absent is not the same as `replace=false` to a server reading a
	// form: sending it unconditionally would be harmless here and is exactly the
	// habit that makes a later default change invisible.
	client, got := uploadServer(t, uploadBody)
	if _, err := client.UploadNPMPackage(context.Background(), "pkg", "1.0.0", []byte("x"), nil); err != nil {
		t.Fatalf("upload: %v", err)
	}
	if _, present := got.fields["replace"]; present {
		t.Errorf("replace was sent with no options given: %q", got.fields["replace"])
	}
	if _, present := got.fields["reference"]; present {
		t.Errorf("reference was sent with no options given: %q", got.fields["reference"])
	}
}

func TestUploadParsesTheRowAndTheMeta(t *testing.T) {
	client, _ := uploadServer(t, uploadBody)
	out, err := client.UploadNPMPackage(context.Background(), "@scope/pkg", "1.3.0", []byte("t"), nil)
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	if out.File.ID != "f1" || out.File.Attestation != "operator-upload" {
		t.Errorf("row = %+v", out.File)
	}
	if out.File.UploadedBy != "admin@example.com" || out.File.UploadReference != "CHG-1" {
		t.Errorf("provenance not parsed: %+v", out.File)
	}
	if out.File.ReplacedDigest != "sha256:old" || out.File.ReplacedAt == "" {
		t.Errorf("replacement record not parsed: %+v", out.File)
	}
	// The meta is what says the write REPLACED something; the row alone cannot,
	// since a first upload and a replacement describe the same row.
	if !out.Replaced || out.Unchanged || out.PreviousDigest != "sha256:old" {
		t.Errorf("meta = %+v", out)
	}
}

func TestUploadClassifiesAConflict(t *testing.T) {
	// 409 is the deliberate-replacement rule: without Replace, existing bytes are
	// refused rather than overwritten. A caller has to be able to tell that from
	// a transport failure.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"status":"409","detail":"already held"}]}`))
	}))
	defer srv.Close()

	_, err := newCacheClient(t, srv).UploadNPMPackage(context.Background(), "pkg", "1.0.0", []byte("x"), nil)
	if err == nil {
		t.Fatal("expected an error")
	}
	if _, ok := err.(*ConflictError); !ok {
		t.Errorf("err = %T (%v), want *ConflictError", err, err)
	}
}

func TestPostMultipartIsDeterministic(t *testing.T) {
	// The retry replays the body, so identical input must produce identical
	// bytes -- which is only true because the fields are written in sorted
	// order.
	var bodies []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		// The boundary is random per request; strip it so the comparison is of
		// the field ORDER rather than of the separator.
		_, params, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
		bodies = append(bodies, strings.ReplaceAll(string(data), params["boundary"], "B"))
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(uploadBody))
	}))
	defer srv.Close()
	client := newCacheClient(t, srv)

	for range 2 {
		if _, err := client.UploadPulumiPlugin(context.Background(),
			"resource", "aws", "6.0.0", "linux", "amd64", []byte("p"), nil); err != nil {
			t.Fatalf("upload: %v", err)
		}
	}
	if len(bodies) != 2 || bodies[0] != bodies[1] {
		t.Errorf("multipart body is not deterministic:\n%q\n%q", bodies[0], bodies[1])
	}
}

func TestUploadResponseShapeIsTolerated(t *testing.T) {
	// A server that omits the meta is not an error: the row is what the caller
	// asked for, and an older server predating the meta still answers usefully.
	client, _ := uploadServer(t, `{"data":{"type":"cached-package-files","id":"f2","attributes":{"ecosystem":"npm"}}}`)
	out, err := client.UploadNPMIndex(context.Background(), "pkg", []byte(`{}`), nil)
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	if out.File.ID != "f2" || out.Replaced || out.Unchanged {
		t.Errorf("out = %+v", out)
	}
	var sanity map[string]any
	if err := json.Unmarshal([]byte(uploadBody), &sanity); err != nil {
		t.Fatalf("the fixture itself is not valid JSON: %v", err)
	}
}
