package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
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

	// Rate-limit budget as last observed from provider response headers
	// (#1334). Pointers, because nil ("the server does not report a rate
	// limit") and 0 ("no budget left") mean opposite things and must not
	// collapse into the same zero value. RateLimitObservedAt says when the
	// reading was taken — these ride along on calls Terrapod was making
	// anyway, so they are an observation, not a live query.
	RateLimit           *int64 `json:"rate-limit,omitempty"`
	RateLimitRemaining  *int64 `json:"rate-limit-remaining,omitempty"`
	RateLimitResource   string `json:"rate-limit-resource,omitempty"`
	RateLimitResetAt    string `json:"rate-limit-reset-at,omitempty"`
	RateLimitObservedAt string `json:"rate-limit-observed-at,omitempty"`

	// Consumption — how fast the budget is being spent, and by what (#1339).
	//
	// The budget fields above cannot answer whether a configuration is
	// straining the limit: the budget refills on a fixed window, so it reads
	// healthy right after a reset however fast it is being spent. These say
	// how fast, whether that lands badly before the refill, and which repos,
	// workspaces or modules are responsible.
	//
	// Saturation is one of idle, comfortable, tight, will_exhaust, exhausted.
	// It is empty when nothing has been observed for the connection.
	CallsPerHour      *int64          `json:"calls-per-hour,omitempty"`
	RateWindowMinutes *int64          `json:"rate-window-minutes,omitempty"`
	SecondsToReset    *int64          `json:"seconds-to-reset,omitempty"`
	Saturation        string          `json:"saturation,omitempty"`
	ExhaustsInSeconds *int64          `json:"exhausts-in-seconds,omitempty"`
	TopConsumers      []VCSConsumer   `json:"top-consumers,omitempty"`
	LabelTotals       []VCSLabelTotal `json:"label-totals,omitempty"`
}

// VCSConsumer is one repo, workspace, module or policy set and the number of
// provider calls attributed to it over the rate window.
type VCSConsumer struct {
	Name  string `json:"name"`
	Kind  string `json:"kind"` // workspace | module | policy-set | subsystem
	Calls int64  `json:"calls"`
}

// VCSLabelTotal is one label and the calls attributed to consumers carrying
// it. Labels are how an estate divides, so this is what says where to split a
// connection whose budget has been outgrown.
type VCSLabelTotal struct {
	Label string `json:"label"` // "key=value", as displayed
	Key   string `json:"key"`
	Value string `json:"value"`
	Calls int64  `json:"calls"`
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

// ListAllVCSConnections fetches every VCS connection, paging through the whole
// collection so the caller doesn't have to. It loops on the server's
// meta.total-pages (falling back to "a short page ends it" when the server
// omits meta), so it stays correct even if the endpoint imposes a page size.
// Prefer this over ListVCSConnections when correctness across a large,
// paginated collection matters (e.g. dedup on migration).
func (c *Client) ListAllVCSConnections(ctx context.Context) ([]VCSConnection, error) {
	const pageSize = 100
	var all []VCSConnection
	for page := 1; ; page++ {
		q := url.Values{}
		q.Set("page[number]", strconv.Itoa(page))
		q.Set("page[size]", strconv.Itoa(pageSize))
		data, err := c.Get(ctx, "/api/terrapod/v1/vcs-connections?"+q.Encode())
		if err != nil {
			return nil, err
		}
		resources, err := ParseResourceList(data)
		if err != nil {
			return nil, err
		}
		for i := range resources {
			all = append(all, *vcsConnFromResource(&resources[i]))
		}
		meta, _ := parseListMeta(data)
		if meta.TotalPages > 0 {
			if page >= meta.TotalPages {
				break
			}
		} else if len(resources) < pageSize {
			break
		}
	}
	return all, nil
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
		RateLimit:            getOptionalIntAttr(res, "rate-limit"),
		RateLimitRemaining:   getOptionalIntAttr(res, "rate-limit-remaining"),
		RateLimitResource:    GetStringAttr(res, "rate-limit-resource"),
		RateLimitResetAt:     GetStringAttr(res, "rate-limit-reset-at"),
		RateLimitObservedAt:  GetStringAttr(res, "rate-limit-observed-at"),
		CallsPerHour:         getOptionalIntAttr(res, "calls-per-hour"),
		RateWindowMinutes:    getOptionalIntAttr(res, "rate-window-minutes"),
		SecondsToReset:       getOptionalIntAttr(res, "seconds-to-reset"),
		Saturation:           GetStringAttr(res, "saturation"),
		ExhaustsInSeconds:    getOptionalIntAttr(res, "exhausts-in-seconds"),
		TopConsumers:         decodeJSONAttr[[]VCSConsumer](res, "top-consumers"),
		LabelTotals:          decodeJSONAttr[[]VCSLabelTotal](res, "label-totals"),
		GithubAccountLogin:   GetStringAttr(res, "github-account-login"),
		GithubAccountType:    GetStringAttr(res, "github-account-type"),
		CreatedAt:            GetStringAttr(res, "created-at"),
		UpdatedAt:            GetStringAttr(res, "updated-at"),
	}
}

// decodeJSONAttr decodes a structured attribute (a list of objects) into T.
// A missing, null or malformed attribute yields the zero value rather than an
// error: these are observability extras, and a client on a newer SDK than the
// server it is talking to must still get a usable connection back.
func decodeJSONAttr[T any](res *Resource, name string) T {
	var out T
	if res == nil || res.Attributes == nil {
		return out
	}
	raw, ok := res.Attributes[name]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return out
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		var zero T
		return zero
	}
	return out
}

// getOptionalIntAttr distinguishes an absent or null attribute from a zero.
// For a rate-limit budget that difference is the whole point: nil means the
// server does not report one, 0 means it is exhausted.
func getOptionalIntAttr(res *Resource, name string) *int64 {
	if res == nil || res.Attributes == nil {
		return nil
	}
	raw, ok := res.Attributes[name]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var n int64
	if err := json.Unmarshal(raw, &n); err != nil {
		return nil
	}
	return &n
}
