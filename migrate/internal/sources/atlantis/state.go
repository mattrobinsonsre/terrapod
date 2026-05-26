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
	"github.com/aws/aws-sdk-go-v2/service/s3"
	gcs "cloud.google.com/go/storage"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/hcl"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/writer"
)

// StateOptions controls Atlantis-side state fetching. Credentials
// are NEVER provided here — every cloud SDK we use (aws-sdk-go-v2,
// cloud.google.com/go/storage, azure-sdk-for-go) has a credential
// chain that already covers env vars, profile files, SSO sessions,
// instance roles, IRSA, ADC, and CLI logins. Reinventing any of that
// inside this tool would just produce a worse version.
//
// The fields below are the *non-credential* overrides the chains
// can't infer: a custom endpoint URL (minio / LocalStack / VPC
// endpoint) and S3 path-style addressing (mandatory for minio).
// For minio smoke tests, operators point AWS_ACCESS_KEY_ID /
// AWS_SECRET_ACCESS_KEY (or an AWS_PROFILE) at the minio creds —
// the SDK picks them up the same way it would for real S3.
type StateOptions struct {
	// S3Endpoint, if set, overrides the AWS S3 endpoint URL. Set
	// this to the minio endpoint URL (e.g. "http://localhost:9000")
	// for smoke tests against minio.
	S3Endpoint string

	// S3ForcePathStyle uses path-style addressing (bucket in path
	// rather than subdomain). Required for minio; AWS S3 itself
	// works either way.
	S3ForcePathStyle bool

	// S3Region overrides the resolved region. Useful for minio
	// (whose region is arbitrary) and for explicit pinning when the
	// backend HCL omits a region.
	S3Region string
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

// projectDirForSourceID resolves a workspace's SourceID to the
// absolute on-disk project directory under SourcePath. SourceID is
// what ProjectIdentifier emits — either the project's `name:` from
// atlantis.yaml or its `dir` (with an optional `/<workspace>` suffix
// when the project uses non-default Terraform workspaces).
//
// We walk the parsed atlantis.yaml to find the project the SourceID
// belongs to (matching name first, then dir+workspace shape) and
// return that project's directory joined with SourcePath. Tolerates
// the dir-only IDs since the Atlantis Emit step stamps those
// directly.
func (s *Source) projectDirForSourceID(sourceID string) (string, error) {
	if s == nil || s.SourcePath == "" {
		return "", errors.New("atlantis source not loaded — call LoadDirectory first")
	}
	if s.AtlantisYAML != nil {
		for _, p := range s.AtlantisYAML.Projects {
			if ProjectIdentifier(p) == sourceID {
				return resolveProjectDir(s.SourcePath, p.Dir)
			}
		}
	}
	// Fallback for synthetic source IDs (tests, ad-hoc fixtures):
	// treat the suffix after the last `:` as a directory relative
	// to SourcePath.
	if idx := strings.LastIndex(sourceID, ":"); idx >= 0 {
		return resolveProjectDir(s.SourcePath, sourceID[idx+1:])
	}
	return "", fmt.Errorf("atlantis source id %q does not match any project in atlantis.yaml", sourceID)
}

// resolveProjectDir joins a project's relative `dir` to SourcePath
// and rejects any result that escapes SourcePath (path traversal).
// A malicious or buggy atlantis.yaml with `dir: ../../etc` would
// otherwise let the migrator read arbitrary local files and upload
// them as terraform state, where anyone with workspace read access
// could retrieve them.
func resolveProjectDir(sourcePath, dir string) (string, error) {
	dir = strings.TrimSpace(dir)
	if dir == "" || dir == "." {
		return sourcePath, nil
	}
	if filepath.IsAbs(dir) {
		return "", fmt.Errorf("atlantis project dir %q must be relative to the repo root", dir)
	}
	joined := filepath.Join(sourcePath, dir)
	cleaned := filepath.Clean(joined)
	rel, err := filepath.Rel(sourcePath, cleaned)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("atlantis project dir %q escapes the repo root", dir)
	}
	return cleaned, nil
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
	if filepath.IsAbs(path) {
		return nil, fmt.Errorf("local backend path %q must be relative to the project directory", configuredPath)
	}
	joined := filepath.Clean(filepath.Join(projectDir, path))
	rel, err := filepath.Rel(projectDir, joined)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return nil, fmt.Errorf("local backend path %q escapes the project directory", configuredPath)
	}
	path = joined
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
	if wkpfx := settings["workspace_key_prefix"]; wkpfx != "" {
		return nil, fmt.Errorf("s3 backend declares workspace_key_prefix=%q — multi-workspace state migration not yet supported; flatten to one tofu workspace per project or open an issue", wkpfx)
	}
	region := opts.S3Region
	if region == "" {
		region = settings["region"]
	}
	if region == "" {
		region = "us-east-1"
	}

	cfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRegion(region))
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
	// GCS terraform backend stores state at <prefix>/<workspace>.tfstate
	// — the migrator assumes the "default" tofu workspace because
	// Atlantis usage almost universally maps one project → one
	// implicit-default workspace. Operators using
	// non-default tofu workspaces under one Atlantis project are a
	// niche case we surface explicitly: any `workspace_key_prefix`
	// setting in the backend HCL is a strong indicator the operator
	// has more than one workspace per project. Refuse to guess and
	// tell them.
	if wkpfx := settings["workspace_key_prefix"]; wkpfx != "" {
		return nil, fmt.Errorf("gcs backend declares workspace_key_prefix=%q — multi-workspace state migration not yet supported; export each workspace's state with `terraform state pull` and import via terrapod-migrate's local backend path, or open an issue", wkpfx)
	}
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
