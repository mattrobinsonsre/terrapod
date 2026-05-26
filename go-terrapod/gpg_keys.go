package terrapod

import (
	"context"
	"fmt"
	"net/url"
)

// GPGKey is a public PGP key uploaded for provider signing. The
// ascii-armored body is write-only — the API stores it and never
// echoes it back; KeyID (extracted by the server from the armor) is
// the field callers should use to identify the key.
type GPGKey struct {
	ID        string `json:"id"`
	KeyID     string `json:"key-id"`
	Namespace string `json:"namespace,omitempty"`
	Source    string `json:"source,omitempty"`
	SourceURL string `json:"source-url,omitempty"`
	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// CreateGPGKeyRequest is the input shape for CreateGPGKey.
// Namespace defaults to "default" and Source to "terrapod" when
// empty.
type CreateGPGKeyRequest struct {
	ASCIIArmor string
	Namespace  string
	Source     string
	SourceURL  string
}

// CreateGPGKey registers a new GPG public key.
func (c *Client) CreateGPGKey(ctx context.Context, req CreateGPGKeyRequest) (*GPGKey, error) {
	if req.Namespace == "" {
		req.Namespace = "default"
	}
	if req.Source == "" {
		req.Source = "terrapod"
	}
	attrs := map[string]any{
		"ascii-armor": req.ASCIIArmor,
		"namespace":   req.Namespace,
		"source":      req.Source,
	}
	if req.SourceURL != "" {
		attrs["source-url"] = req.SourceURL
	}
	body, err := MarshalResource("gpg-keys", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create gpg-key: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/gpg-keys", body)
	if err != nil {
		return nil, err
	}
	return parseGPGKey(data)
}

// GetGPGKey reads a GPG key by id.
func (c *Client) GetGPGKey(ctx context.Context, id string) (*GPGKey, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/gpg-keys/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseGPGKey(data)
}

// ListGPGKeys returns every registered key.
func (c *Client) ListGPGKeys(ctx context.Context) ([]GPGKey, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/gpg-keys")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]GPGKey, 0, len(resources))
	for i := range resources {
		out = append(out, *gpgKeyFromResource(&resources[i]))
	}
	return out, nil
}

// DeleteGPGKey removes a key. Providers signed by the key can still
// be served from cache; new uploads can no longer reference the key.
func (c *Client) DeleteGPGKey(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/gpg-keys/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseGPGKey(body []byte) (*GPGKey, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse gpg-key response: %w", err)
	}
	return gpgKeyFromResource(res), nil
}

func gpgKeyFromResource(res *Resource) *GPGKey {
	return &GPGKey{
		ID:        res.ID,
		KeyID:     GetStringAttr(res, "key-id"),
		Namespace: GetStringAttr(res, "namespace"),
		Source:    GetStringAttr(res, "source"),
		SourceURL: GetStringAttr(res, "source-url"),
		CreatedAt: GetStringAttr(res, "created-at"),
		UpdatedAt: GetStringAttr(res, "updated-at"),
	}
}
