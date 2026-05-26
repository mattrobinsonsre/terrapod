// Atlantis-side state migration. Atlantis itself doesn't manage
// state — every workspace declares its own backend in its Terraform
// HCL. This file walks each project's HCL with the shared hcl
// package, finds the backend declaration, and downloads the current
// state via the appropriate native cloud-vendor SDK.
//
// Supported backends today: local, s3 (incl. minio via --s3-endpoint-
// url-equivalent on the operator's AWS_CONFIG), gcs, azurerm. Other
// kinds (consul, etcd, http, ...) are surfaced as skipped items in
// the report — the operator migrates state for those by hand.
package atlantis

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/Azure/azure-sdk-for-go/sdk/azidentity"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	gcs "cloud.google.com/go/storage"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/hcl"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/writer"
)

// StateOptions controls Atlantis-side state fetching. Most fields
// are optional — the SDK clients default to the operator's ambient
// credentials (env vars, IRSA, gcloud, Azure CLI login) — but the
// fields exist as explicit overrides for testing (minio in
// particular needs S3Endpoint + ForcePathStyle).
type StateOptions struct {
	// S3Endpoint, if set, overrides the AWS S3 endpoint URL. Set this
	// to the minio endpoint URL (e.g. "http://localhost:9000") for
	// smoke tests against minio.
	S3Endpoint string

	// S3ForcePathStyle uses path-style addressing (bucket in path
	// rather than subdomain). Required for minio; AWS S3 itself
	// works either way.
	S3ForcePathStyle bool

	// S3Region overrides the resolved region. Used in conjunction
	// with S3Endpoint for minio (whose region is arbitrary).
	S3Region string

	// S3AccessKey / S3SecretKey override the credential chain — set
	// these for minio smoke testing. Production migrations should
	// leave these empty and rely on the operator's ambient creds.
	S3AccessKey string
	S3SecretKey string
}

// StateReader returns a writer.StateReader that resolves
// "<repo-url>:<dir>" SourceIDs (the shape Emit stamps on each
// workspace) to the underlying backend's state bytes.
func (s *Source) StateReader(opts StateOptions) writer.StateReader {
	return func(ctx context.Context, workspaceSourceID string) ([]byte, string, int64, error) {
		dir, err := s.projectDirForSourceID(workspaceSourceID)
		if err != nil {
			return nil, "", 0, err
		}
		backend, err := hcl.DetectBackend(dir)
		if err != nil {
			return nil, "", 0, fmt.Errorf("detect backend for %s: %w", workspaceSourceID, err)
		}
		raw, err := fetchStateForBackend(ctx, backend, dir, opts)
		if err != nil {
			return nil, "", 0, err
		}
		lineage, serial, err := parseLineageAndSerial(raw)
		if err != nil {
			return nil, "", 0, fmt.Errorf("parse state for %s: %w", workspaceSourceID, err)
		}
		return raw, lineage, serial, nil
	}
}

// projectDirForSourceID — the Emit step stamps each workspace's
// SourceID as "<repo-url>:<dir-relative-to-repo-root>". This reverses
// the encoding to find the absolute on-disk project directory under
// SourcePath.
func (s *Source) projectDirForSourceID(sourceID string) (string, error) {
	if s == nil || s.SourcePath == "" {
		return "", errors.New("atlantis source not loaded — call LoadDirectory first")
	}
	idx := strings.LastIndex(sourceID, ":")
	if idx < 0 {
		return "", fmt.Errorf("malformed atlantis source id %q (expected <repo>:<dir>)", sourceID)
	}
	rel := sourceID[idx+1:]
	if rel == "" || rel == "." {
		return s.SourcePath, nil
	}
	return filepath.Join(s.SourcePath, rel), nil
}

func fetchStateForBackend(ctx context.Context, backend *hcl.Backend, projectDir string, opts StateOptions) ([]byte, error) {
	if backend == nil {
		// No backend block ⇒ implicit local backend ⇒
		// terraform.tfstate in the project directory.
		return readLocalState(projectDir, "")
	}
	switch backend.Kind {
	case hcl.BackendLocal:
		return readLocalState(projectDir, backend.Settings["path"])
	case hcl.BackendS3:
		return readS3State(ctx, backend.Settings, opts)
	case hcl.BackendGCS:
		return readGCSState(ctx, backend.Settings)
	case hcl.BackendAzureRM:
		return readAzureState(ctx, backend.Settings)
	case hcl.BackendRemote, hcl.BackendCloud:
		return nil, fmt.Errorf("backend %q is TFE/HCP — rerun with --source=tfe", backend.Kind)
	default:
		return nil, fmt.Errorf("backend %q not yet supported for state migration; migrate state manually", backend.Kind)
	}
}

// ── Local backend ────────────────────────────────────────────────────

func readLocalState(projectDir, configuredPath string) ([]byte, error) {
	path := configuredPath
	if path == "" {
		path = "terraform.tfstate"
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(projectDir, path)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: projectDir}
		}
		return nil, fmt.Errorf("read local state %s: %w", path, err)
	}
	if len(raw) == 0 {
		return nil, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: projectDir}
	}
	return raw, nil
}

// ── S3 backend (also minio) ──────────────────────────────────────────

func readS3State(ctx context.Context, settings map[string]string, opts StateOptions) ([]byte, error) {
	bucket := settings["bucket"]
	key := settings["key"]
	if bucket == "" || key == "" {
		return nil, fmt.Errorf("s3 backend missing bucket/key (have %v)", settings)
	}
	region := opts.S3Region
	if region == "" {
		region = settings["region"]
	}
	if region == "" {
		region = "us-east-1"
	}

	loadOpts := []func(*awsconfig.LoadOptions) error{
		awsconfig.WithRegion(region),
	}
	if opts.S3AccessKey != "" && opts.S3SecretKey != "" {
		loadOpts = append(loadOpts, awsconfig.WithCredentialsProvider(
			credentials.NewStaticCredentialsProvider(opts.S3AccessKey, opts.S3SecretKey, ""),
		))
	}
	cfg, err := awsconfig.LoadDefaultConfig(ctx, loadOpts...)
	if err != nil {
		return nil, fmt.Errorf("s3: load aws config: %w", err)
	}
	clientOpts := []func(*s3.Options){}
	if opts.S3Endpoint != "" {
		ep := opts.S3Endpoint
		clientOpts = append(clientOpts, func(o *s3.Options) {
			o.BaseEndpoint = &ep
		})
	}
	if opts.S3ForcePathStyle {
		clientOpts = append(clientOpts, func(o *s3.Options) {
			o.UsePathStyle = true
		})
	}
	client := s3.NewFromConfig(cfg, clientOpts...)

	out, err := client.GetObject(ctx, &s3.GetObjectInput{Bucket: &bucket, Key: &key})
	if err != nil {
		// Treat 404 as "no state yet" — operator may have just
		// created the workspace without applying.
		if isS3NotFound(err) {
			return nil, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: fmt.Sprintf("s3://%s/%s", bucket, key)}
		}
		return nil, fmt.Errorf("s3 GetObject %s/%s: %w", bucket, key, err)
	}
	defer func() { _ = out.Body.Close() }()

	return readBounded(out.Body, "s3")
}

func isS3NotFound(err error) bool {
	// The aws-sdk-go-v2 error tree is verbose; the simplest robust
	// check is a string match on the API's NoSuchKey code. We keep
	// the match narrow so other 4xx errors (auth) still bubble up
	// as failures.
	return err != nil && strings.Contains(err.Error(), "NoSuchKey")
}

// ── GCS backend ──────────────────────────────────────────────────────

func readGCSState(ctx context.Context, settings map[string]string) ([]byte, error) {
	bucket := settings["bucket"]
	prefix := settings["prefix"]
	if bucket == "" {
		return nil, fmt.Errorf("gcs backend missing bucket (have %v)", settings)
	}
	// GCS terraform backend stores state at <prefix>/default.tfstate
	// (with `default` being the terraform workspace; we don't yet
	// support workspaces-other-than-default — those are rare in
	// Atlantis flows and tracked as future work).
	objectName := "default.tfstate"
	if prefix != "" {
		objectName = strings.TrimSuffix(prefix, "/") + "/" + objectName
	}

	client, err := gcs.NewClient(ctx)
	if err != nil {
		return nil, fmt.Errorf("gcs: new client: %w", err)
	}
	defer func() { _ = client.Close() }()

	r, err := client.Bucket(bucket).Object(objectName).NewReader(ctx)
	if err != nil {
		if errors.Is(err, gcs.ErrObjectNotExist) {
			return nil, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: fmt.Sprintf("gcs://%s/%s", bucket, objectName)}
		}
		return nil, fmt.Errorf("gcs read %s/%s: %w", bucket, objectName, err)
	}
	defer func() { _ = r.Close() }()
	return readBounded(r, "gcs")
}

// ── Azure backend ────────────────────────────────────────────────────

func readAzureState(ctx context.Context, settings map[string]string) ([]byte, error) {
	account := settings["storage_account_name"]
	container := settings["container_name"]
	key := settings["key"]
	if account == "" || container == "" || key == "" {
		return nil, fmt.Errorf("azurerm backend missing storage_account_name/container_name/key (have %v)", settings)
	}
	serviceURL := fmt.Sprintf("https://%s.blob.core.windows.net/", account)
	cred, err := azidentity.NewDefaultAzureCredential(nil)
	if err != nil {
		return nil, fmt.Errorf("azure: default credential: %w", err)
	}
	client, err := azblob.NewClient(serviceURL, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("azure: new client: %w", err)
	}
	resp, err := client.DownloadStream(ctx, container, key, nil)
	if err != nil {
		// azblob doesn't expose a typed not-found; match on the
		// service code in the wrapped error message.
		if strings.Contains(err.Error(), "BlobNotFound") {
			return nil, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: fmt.Sprintf("azure://%s/%s/%s", account, container, key)}
		}
		return nil, fmt.Errorf("azure read %s/%s/%s: %w", account, container, key, err)
	}
	defer func() { _ = resp.Body.Close() }()
	return readBounded(resp.Body, "azure")
}

// ── Helpers ──────────────────────────────────────────────────────────

const maxStateBytes = 256 << 20

func readBounded(r io.Reader, label string) ([]byte, error) {
	buf := &bytes.Buffer{}
	n, err := io.Copy(buf, io.LimitReader(r, maxStateBytes+1))
	if err != nil {
		return nil, fmt.Errorf("%s read: %w", label, err)
	}
	if n > maxStateBytes {
		return nil, fmt.Errorf("%s state exceeds %d-byte safety cap", label, maxStateBytes)
	}
	if buf.Len() == 0 {
		return nil, fmt.Errorf("%s returned an empty state body", label)
	}
	return buf.Bytes(), nil
}

func parseLineageAndSerial(raw []byte) (string, int64, error) {
	// Minimal JSON decoder for just the two fields we need; the rest
	// of the state document goes through to Terrapod verbatim.
	type stateHead struct {
		Lineage string `json:"lineage"`
		Serial  int64  `json:"serial"`
	}
	var head stateHead
	if err := json.Unmarshal(raw, &head); err != nil {
		return "", 0, err
	}
	if head.Lineage == "" {
		return "", 0, errors.New("state document missing lineage")
	}
	return head.Lineage, head.Serial, nil
}
