package terrapod

import (
	"context"
	"crypto/md5" //nolint:gosec // not a security primitive — TFE-compatible checksum
	"encoding/hex"
	"fmt"
	"net/url"
	"time"
)

// StateVersion is the Terrapod-side record of a Terraform state
// version. The raw state JSON is referenced by ID and uploaded
// separately via UploadStateContent (the standard TFE two-step
// create-then-PUT pattern).
type StateVersion struct {
	ID                string `json:"id"`
	Serial            int64  `json:"serial"`
	Lineage           string `json:"lineage"`
	MD5               string `json:"md5,omitempty"`
	StateSize         int64  `json:"state-size,omitempty"`
	HostedStateDLURL  string `json:"hosted-state-download-url,omitempty"`
	CreatedAt         string `json:"created-at,omitempty"`
}

// CreateStateVersionRequest is the input shape for CreateStateVersion.
// Serial + Lineage MUST come from the upstream state — Terrapod
// rejects a state version with a serial that already exists on the
// workspace unless Force=true.
type CreateStateVersionRequest struct {
	Serial  int64
	Lineage string
	MD5     string
	Force   bool
}

// CreateStateVersion creates a state version record on the given
// workspace. The returned StateVersion's ID is the upload target
// for UploadStateContent.
func (c *Client) CreateStateVersion(ctx context.Context, workspaceID string, req CreateStateVersionRequest) (*StateVersion, error) {
	attrs := map[string]any{
		"serial":  req.Serial,
		"lineage": req.Lineage,
		"md5":     req.MD5,
		"force":   req.Force,
	}
	body, err := MarshalResource("state-versions", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create state-version: %w", err)
	}
	data, err := c.Post(ctx,
		fmt.Sprintf("/api/v2/workspaces/%s/state-versions", url.PathEscape(workspaceID)),
		body)
	if err != nil {
		return nil, err
	}
	return parseStateVersion(data)
}

// UploadStateContent uploads the raw state bytes to the state
// version's content endpoint. The endpoint accepts the bytes as-is
// (no JSON:API envelope) and does not check the bearer token — the
// state version ID is the capability. This matches the TFE V2
// behaviour that go-tfe expects.
func (c *Client) UploadStateContent(ctx context.Context, stateVersionID string, raw []byte) error {
	_, err := c.PutRaw(ctx,
		fmt.Sprintf("/api/v2/state-versions/%s/content", url.PathEscape(stateVersionID)),
		"application/octet-stream",
		raw)
	return err
}

// CreateAndUploadState is a convenience that pairs the two-step flow
// into a single call. Computes the MD5 from the raw bytes when MD5
// is empty in the request; otherwise uses the caller's value (useful
// when a source-side checksum is already known).
//
// On upload failure, the partially-created state version record is
// best-effort deleted so a subsequent retry can re-create at the
// same serial without colliding. A failed rollback is reported in
// the wrapped error so the operator can clean up manually.
func (c *Client) CreateAndUploadState(ctx context.Context, workspaceID string, raw []byte, req CreateStateVersionRequest) (*StateVersion, error) {
	if req.MD5 == "" {
		sum := md5.Sum(raw) //nolint:gosec
		req.MD5 = hex.EncodeToString(sum[:])
	}
	sv, err := c.CreateStateVersion(ctx, workspaceID, req)
	if err != nil {
		return nil, fmt.Errorf("create state version: %w", err)
	}
	if err := c.UploadStateContent(ctx, sv.ID, raw); err != nil {
		// Best-effort rollback. We use a fresh background context with
		// a short timeout because the caller's ctx may itself be the
		// cause of the upload failure (cancel / deadline) and we still
		// want to clean up the orphan record. Failures are surfaced
		// via the wrapped error message so operators see what's left
		// behind on Terrapod.
		rbCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if rbErr := c.DeleteStateVersion(rbCtx, sv.ID); rbErr != nil {
			return nil, fmt.Errorf("upload state content (sv=%s): %w; rollback of orphan record also failed: %v", sv.ID, err, rbErr)
		}
		return nil, fmt.Errorf("upload state content (sv=%s): %w", sv.ID, err)
	}
	return sv, nil
}

// DeleteStateVersion removes a non-current state version record.
// Used by CreateAndUploadState to roll back an orphaned record when
// the /content PUT fails after the metadata row was created. Path
// is the Terrapod-native management endpoint, not the TFE V2 surface.
func (c *Client) DeleteStateVersion(ctx context.Context, stateVersionID string) error {
	return c.Delete(ctx,
		fmt.Sprintf("/api/terrapod/v1/state-versions/%s/manage", url.PathEscape(stateVersionID)))
}

// GetCurrentStateVersion reads the current state version for a
// workspace. Returns nil + *NotFoundError when the workspace has no
// state yet.
func (c *Client) GetCurrentStateVersion(ctx context.Context, workspaceID string) (*StateVersion, error) {
	data, err := c.Get(ctx,
		fmt.Sprintf("/api/v2/workspaces/%s/current-state-version", url.PathEscape(workspaceID)))
	if err != nil {
		return nil, err
	}
	return parseStateVersion(data)
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseStateVersion(body []byte) (*StateVersion, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse state-version response: %w", err)
	}
	return &StateVersion{
		ID:               res.ID,
		Serial:           GetIntAttr(res, "serial"),
		Lineage:          GetStringAttr(res, "lineage"),
		MD5:              GetStringAttr(res, "md5"),
		StateSize:        GetIntAttr(res, "state-size"),
		HostedStateDLURL: GetStringAttr(res, "hosted-state-download-url"),
		CreatedAt:        GetStringAttr(res, "created-at"),
	}, nil
}
