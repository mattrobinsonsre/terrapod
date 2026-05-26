package tfe

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/writer"
)

// ReadCurrentState downloads the current state version blob for the
// given workspace. Returns (raw, lineage, serial, nil) on success,
// or (*writer.ErrNoStateForWorkspace) when the workspace has never
// been applied (no state to migrate). All other failures (auth,
// network, body too large, ...) bubble up wrapped.
//
// The download URL TFE returns is short-lived (signed) and points at
// archivist; the SDK fetches it with a fresh http.Client to avoid
// pulling go-tfe's retry/UA wrapper through the binary blob.
func (c *Client) ReadCurrentState(ctx context.Context, workspaceID string) ([]byte, string, int64, error) {
	sv, err := c.API.StateVersions.ReadCurrentWithOptions(ctx, workspaceID, &tfe.StateVersionCurrentOptions{})
	if err != nil {
		if errors.Is(err, tfe.ErrResourceNotFound) {
			return nil, "", 0, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: workspaceID}
		}
		return nil, "", 0, fmt.Errorf("read current state for %s: %w", workspaceID, err)
	}
	if sv == nil {
		return nil, "", 0, &writer.ErrNoStateForWorkspace{WorkspaceSourceID: workspaceID}
	}
	if sv.DownloadURL == "" {
		return nil, "", 0, fmt.Errorf("state version %s has no download URL", sv.ID)
	}

	raw, err := fetchSignedURL(ctx, sv.DownloadURL)
	if err != nil {
		return nil, "", 0, fmt.Errorf("download state for %s: %w", workspaceID, err)
	}

	// go-tfe's StateVersion shape doesn't expose `lineage` — but
	// every well-formed Terraform state document carries it at the
	// top level. Parse just that field; if it's missing we surface
	// an error so the writer never POSTs an empty lineage (Terrapod
	// rejects empty lineage at create time).
	lineage, err := extractLineage(raw)
	if err != nil {
		return nil, "", 0, fmt.Errorf("extract lineage for %s: %w", workspaceID, err)
	}
	return raw, lineage, int64(sv.Serial), nil
}

// extractLineage parses just the lineage field out of a serialised
// Terraform state document. The document is JSON; only the lineage
// key is read so unfamiliar fields (resource-specific extensions,
// versions we don't know about) don't trip us up.
func extractLineage(raw []byte) (string, error) {
	var doc struct {
		Lineage string `json:"lineage"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return "", err
	}
	if doc.Lineage == "" {
		return "", errors.New("state document has empty lineage")
	}
	return doc.Lineage, nil
}

// StateReader is the writer.StateReader-compatible callback that
// resolves source IDs (TFE workspace UUIDs, as stamped into the IR
// at workspace-emit time) to state blobs. Returned shape matches the
// writer's contract: nil raw + *ErrNoStateForWorkspace is a normal
// "never applied" case, not an error.
func (c *Client) StateReader() writer.StateReader {
	return func(ctx context.Context, workspaceSourceID string) ([]byte, string, int64, error) {
		return c.ReadCurrentState(ctx, workspaceSourceID)
	}
}

// fetchSignedURL pulls bytes from a presigned URL with a context-
// scoped http.Client. Bounded at 256 MB — bigger states are
// pathological and worth surfacing as an error rather than oom-ing
// the migrator.
func fetchSignedURL(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, fmt.Errorf("download URL returned HTTP %d: %s", resp.StatusCode, string(body))
	}
	const maxStateBytes = 256 << 20
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxStateBytes+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > maxStateBytes {
		return nil, fmt.Errorf("state body exceeds %d-byte safety cap; refusing to migrate", maxStateBytes)
	}
	return raw, nil
}
