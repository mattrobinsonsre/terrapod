package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
)

// DeletedWorkspace is a workspace that has been deleted but whose state is
// still recoverable (#1253).
//
// Deleting a workspace removes its rows — state-version rows go by CASCADE —
// but not its state blobs. A marker written at delete time is what makes those
// blobs findable again, and what gives the retention reaper something to age;
// this type is that marker as the API serves it.
//
// The window is finite. Once DeletedAt is older than the server's
// deleted_workspace_retention_days the state is reaped and the workspace is
// gone for good, so RestorableUntil is the field a caller should act on.
type DeletedWorkspace struct {
	WorkspaceID   string `json:"workspace-id"`
	WorkspaceName string `json:"workspace-name"`
	DeletedAt     string `json:"deleted-at"`
	DeletedBy     string `json:"deleted-by"`

	// MarkerReason distinguishes how the marker came to exist: "deleted" means
	// it was written by the delete itself and DeletedAt is the true deletion
	// time; "discovered-orphaned" means the reaper found state with no marker
	// (a workspace deleted before this feature shipped, or one whose marker
	// write failed) and DeletedAt is merely when it was first seen.
	MarkerReason string `json:"marker-reason"`

	LastSerial int    `json:"last-serial"`
	Lineage    string `json:"lineage"`

	// StateVersionsAvailable is counted from storage at request time, not
	// taken from the marker — a partial reap or an incomplete replication
	// shows up here and nowhere else.
	StateVersionsAvailable int      `json:"state-versions-available"`
	AgeDays                *float64 `json:"age-days"`

	// RestorableUntil is empty when retention is disabled (0 days), meaning
	// the state is never reaped automatically.
	RestorableUntil string `json:"restorable-until"`

	Settings      map[string]any             `json:"settings"`
	VariableNames []DeletedWorkspaceVariable `json:"variable-names"`
}

// DeletedWorkspaceVariable names a variable the deleted workspace had.
//
// Names only, never values: the marker is a plain object in the bucket and is
// replicated to any standby, so a value there would be a secret at rest
// outside the encryption boundary. The names are what an operator needs in
// order to know what to recreate after a restore.
type DeletedWorkspaceVariable struct {
	Key       string `json:"key"`
	Category  string `json:"category"`
	Sensitive bool   `json:"sensitive"`
}

// RestoredWorkspace is the outcome of a restore.
//
// The restore produces a NEW workspace rather than reviving the original in
// place, so ID is not RestoredFrom. Suppressed and DroppedReferences describe
// what was deliberately not carried across, and are the point of the response:
// a restored workspace comes back inert and it is the caller's job to decide
// what to switch back on.
type RestoredWorkspace struct {
	ID                    string           `json:"-"` // from the resource id, not attributes
	Name                  string           `json:"name"`
	RestoredFrom          string           `json:"restored-from"`
	StateVersionsRestored int              `json:"state-versions-restored"`
	StateVersionsSkipped  []map[string]any `json:"state-versions-skipped"`

	// Suppressed names settings that were true in the snapshot but forced off:
	// auto-apply, drift detection, and the VCS connection. A workspace that
	// applied the moment it was restored — against infrastructure that may
	// have drifted or been partly destroyed since the delete — is the failure
	// mode this prevents.
	Suppressed []string `json:"suppressed"`

	// DroppedReferences names things that pointed at other resources and were
	// not re-attached, because the id may since have been reused by something
	// entirely different.
	DroppedReferences []map[string]any `json:"dropped-references"`
}

// DeletedWorkspaceListOptions controls paging of the undelete list.
type DeletedWorkspaceListOptions struct {
	PageNumber int
	PageSize   int
}

// DeletedWorkspaceList is one page of deleted workspaces plus its pagination.
type DeletedWorkspaceList struct {
	Items []DeletedWorkspace
	Meta  ListMeta
}

// ListDeletedWorkspaces returns one page of recoverable deleted workspaces,
// newest deletion first. Requires platform admin.
func (c *Client) ListDeletedWorkspaces(ctx context.Context, opts DeletedWorkspaceListOptions) (*DeletedWorkspaceList, error) {
	q := url.Values{}
	if opts.PageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(opts.PageNumber))
	}
	if opts.PageSize > 0 {
		q.Set("page[size]", strconv.Itoa(opts.PageSize))
	}
	path := "/api/terrapod/v1/deleted-workspaces"
	if encoded := q.Encode(); encoded != "" {
		path += "?" + encoded
	}
	body, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data []struct {
			Attributes DeletedWorkspace `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("decode deleted workspaces: %w", err)
	}
	out := &DeletedWorkspaceList{Items: make([]DeletedWorkspace, 0, len(doc.Data))}
	for _, d := range doc.Data {
		out.Items = append(out.Items, d.Attributes)
	}
	meta, err := parseListMeta(body)
	if err == nil {
		out.Meta = meta
	}
	return out, nil
}

// ListAllDeletedWorkspaces pages through every recoverable deleted workspace.
func (c *Client) ListAllDeletedWorkspaces(ctx context.Context) ([]DeletedWorkspace, error) {
	var all []DeletedWorkspace
	for page := 1; ; page++ {
		list, err := c.ListDeletedWorkspaces(ctx, DeletedWorkspaceListOptions{PageNumber: page, PageSize: 100})
		if err != nil {
			return nil, err
		}
		all = append(all, list.Items...)
		if len(list.Items) == 0 || page >= list.Meta.TotalPages {
			return all, nil
		}
	}
}

// GetDeletedWorkspace fetches one deleted workspace's marker.
func (c *Client) GetDeletedWorkspace(ctx context.Context, workspaceID string) (*DeletedWorkspace, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/deleted-workspaces/"+workspaceID)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			Attributes DeletedWorkspace `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("decode deleted workspace: %w", err)
	}
	return &doc.Data.Attributes, nil
}

// RestoreDeletedWorkspace recovers a deleted workspace's state into a new
// workspace. Requires platform admin.
//
// name is optional; when empty the original name is reused, suffixed if it has
// been taken since. Returns a ConflictError when no state could be recovered —
// the retention window has passed and the blobs are gone — which is reported
// as a failure rather than an empty success so a caller is never handed a bare
// workspace and told the restore worked.
func (c *Client) RestoreDeletedWorkspace(ctx context.Context, workspaceID, name string) (*RestoredWorkspace, error) {
	attrs := map[string]any{}
	if name != "" {
		attrs["name"] = name
	}
	payload, err := json.Marshal(map[string]any{
		"data": map[string]any{"type": "workspaces", "attributes": attrs},
	})
	if err != nil {
		return nil, fmt.Errorf("encode restore request: %w", err)
	}
	body, err := c.Post(ctx, "/api/terrapod/v1/deleted-workspaces/"+workspaceID+"/restore", payload)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			ID         string            `json:"id"`
			Attributes RestoredWorkspace `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("decode restored workspace: %w", err)
	}
	out := doc.Data.Attributes
	out.ID = doc.Data.ID
	return &out, nil
}
