package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// VCSConnection is the decoded form of one Terrapod VCS connection
// (the platform-level credentials configuring how Terrapod talks to
// GitHub or GitLab). Workspaces reference connections via the
// vcs-connection-id field.
//
// PrivateKey and Token never appear here — they're write-only on the
// API surface (HasToken indicates whether the connection has one
// configured). Callers managing the resource in Terraform must store
// the configured private key / token in Terraform state separately
// from anything the SDK returns.
type VCSConnection struct {
	ID                   string `json:"id"`
	Name                 string `json:"name"`
	Provider             string `json:"provider"` // "github" | "gitlab"
	ServerURL            string `json:"server-url,omitempty"`
	GithubAppID          int64  `json:"github-app-id,omitempty"`
	GithubInstallationID int64  `json:"github-installation-id,omitempty"`
	Status               string `json:"status,omitempty"`
	HasToken             bool   `json:"has-token"`
	HasWebhookSecret     bool   `json:"has-webhook-secret"`
	GithubAccountLogin   string `json:"github-account-login,omitempty"`
	GithubAccountType    string `json:"github-account-type,omitempty"`
	CreatedAt            string `json:"created-at,omitempty"`
	UpdatedAt            string `json:"updated-at,omitempty"`
}

// CreateVCSConnectionRequest is the input shape for
// Client.CreateVCSConnection. Token + PrivateKey are write-only —
// the server stores them but never echoes them back.
type CreateVCSConnectionRequest struct {
	Name                 string
	Provider             string // "github" | "gitlab"
	ServerURL            string
	GithubAppID          int64
	GithubInstallationID int64
	PrivateKey           string // GitHub App PEM
	Token                string // GitLab PAT
	WebhookSecret        string // GitHub per-connection webhook secret (write-only, optional)
}

// UpdateVCSConnectionRequest patches a VCS connection — the
// supporting Terrapod endpoint shipped in #315. Pointer fields
// preserve "leave alone" semantics. The Provider field is immutable;
// the SDK omits it from the body on update.
type UpdateVCSConnectionRequest struct {
	Name                 string
	ServerURL            string
	GithubAppID          *int64
	GithubInstallationID *int64
	PrivateKey           string // pass non-empty to rotate
	Token                string // pass non-empty to rotate
	// WebhookSecret rotation: pass a non-empty value to set/rotate, an
	// explicit empty string to clear (fall back to the global secret), or
	// leave nil to keep the stored value untouched.
	WebhookSecret *string
}

// CreateVCSConnection registers a new VCS connection. Requires
// admin role on the Terrapod side.
func (c *Client) CreateVCSConnection(ctx context.Context, req CreateVCSConnectionRequest) (*VCSConnection, error) {
	body, err := MarshalResource("vcs-connections", vcsConnCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create vcs-connection: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/vcs-connections", body)
	if err != nil {
		return nil, err
	}
	return parseVCSConnection(data)
}

// GetVCSConnection reads a VCS connection by id.
func (c *Client) GetVCSConnection(ctx context.Context, id string) (*VCSConnection, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/vcs-connections/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseVCSConnection(data)
}

// ListVCSConnections returns every connection. Terrapod doesn't
// paginate this endpoint (the count is small).
func (c *Client) ListVCSConnections(ctx context.Context) ([]VCSConnection, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/vcs-connections")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]VCSConnection, 0, len(resources))
	for i := range resources {
		out = append(out, *vcsConnFromResource(&resources[i]))
	}
	return out, nil
}

// UpdateVCSConnection patches the connection by id. The Provider
// field is immutable and cannot be changed (Terrapod rejects); pass
// non-empty PrivateKey/Token to rotate the credential, empty to
// leave intact.
func (c *Client) UpdateVCSConnection(ctx context.Context, id string, req UpdateVCSConnectionRequest) (*VCSConnection, error) {
	body, err := MarshalResourceWithID(id, "vcs-connections", vcsConnUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update vcs-connection: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/vcs-connections/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseVCSConnection(data)
}

// DeleteVCSConnection removes a VCS connection. Workspaces
// referencing the connection lose their VCS link; the workspaces
// themselves are not deleted.
func (c *Client) DeleteVCSConnection(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/vcs-connections/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func vcsConnCreateAttrs(req CreateVCSConnectionRequest) map[string]any {
	attrs := map[string]any{
		"name":     req.Name,
		"provider": req.Provider,
	}
	if req.ServerURL != "" {
		attrs["server-url"] = req.ServerURL
	}
	if req.GithubAppID != 0 {
		attrs["github-app-id"] = req.GithubAppID
	}
	if req.GithubInstallationID != 0 {
		attrs["github-installation-id"] = req.GithubInstallationID
	}
	if req.PrivateKey != "" {
		attrs["private-key"] = req.PrivateKey
	}
	if req.Token != "" {
		attrs["token"] = req.Token
	}
	if req.WebhookSecret != "" {
		attrs["webhook-secret"] = req.WebhookSecret
	}
	return attrs
}

func vcsConnUpdateAttrs(req UpdateVCSConnectionRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != "" {
		attrs["name"] = req.Name
	}
	if req.ServerURL != "" {
		attrs["server-url"] = req.ServerURL
	}
	if req.GithubAppID != nil {
		attrs["github-app-id"] = *req.GithubAppID
	}
	if req.GithubInstallationID != nil {
		attrs["github-installation-id"] = *req.GithubInstallationID
	}
	// Credentials only sent when caller explicitly rotates — empty
	// string ↦ leave alone. Without this rule a vanilla PATCH that
	// touched only `name` would clear the private key.
	if req.PrivateKey != "" {
		attrs["private-key"] = req.PrivateKey
	}
	if req.Token != "" {
		attrs["token"] = req.Token
	}
	// nil ↦ leave untouched; non-nil (incl. "") ↦ set/clear. The server
	// treats an explicit empty string as "clear" (fall back to global).
	if req.WebhookSecret != nil {
		attrs["webhook-secret"] = *req.WebhookSecret
	}
	return attrs
}

func parseVCSConnection(body []byte) (*VCSConnection, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse vcs-connection response: %w", err)
	}
	return vcsConnFromResource(res), nil
}

func vcsConnFromResource(res *Resource) *VCSConnection {
	return &VCSConnection{
		ID:                   res.ID,
		Name:                 GetStringAttr(res, "name"),
		Provider:             GetStringAttr(res, "provider"),
		ServerURL:            GetStringAttr(res, "server-url"),
		GithubAppID:          GetIntAttr(res, "github-app-id"),
		GithubInstallationID: GetIntAttr(res, "github-installation-id"),
		Status:               GetStringAttr(res, "status"),
		HasToken:             GetBoolAttr(res, "has-token"),
		HasWebhookSecret:     GetBoolAttr(res, "has-webhook-secret"),
		GithubAccountLogin:   GetStringAttr(res, "github-account-login"),
		GithubAccountType:    GetStringAttr(res, "github-account-type"),
		CreatedAt:            GetStringAttr(res, "created-at"),
		UpdatedAt:            GetStringAttr(res, "updated-at"),
	}
}
