package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// OIDCSigningKey is one key in the set Terrapod publishes as an OIDC identity
// provider for run identity tokens (#1901).
//
// Public material only — the private half never leaves the API. Kid is an
// RFC 7638 thumbprint, so it is the same value a federation target reads out of
// a token header and matches against the published JWKS.
//
// A rotation is a SET, not a swap, which is why the three timestamps are
// separate and all three matter:
//
//   - CreatedAt — when the key was added and first published.
//   - ActivatesAt — when it starts signing. Later than CreatedAt on purpose: a
//     federation target caches the JWKS on its own schedule, so a token signed
//     with a key it has not fetched yet cannot be verified.
//   - RetiredAt — when it stopped signing. It stays published past that point
//     for its grace window, because the tokens it already signed are still
//     inside their own lifetime.
//
// Signing is the only field that answers "which key is signing right now", and
// it is computed server-side rather than derivable from the timestamps alone.
type OIDCSigningKey struct {
	Kid         string `json:"kid"`
	CreatedAt   string `json:"created-at,omitempty"`
	ActivatesAt string `json:"activates-at,omitempty"`
	RetiredAt   string `json:"retired-at,omitempty"`
	Signing     bool   `json:"signing"`
}

// OIDCSigningKeySet is the published key set plus which member is signing.
//
// SigningKID is empty when nothing is currently signing — the issuer is off, or
// every key's activation window is still ahead of it. Neither is an error, so
// it is a field rather than a returned error.
type OIDCSigningKeySet struct {
	Keys       []OIDCSigningKey
	SigningKID string
}

// OIDCSigningKeyRotation is the result of rotating the signing key: the key
// that was added, and the server's note about when it begins signing.
type OIDCSigningKeyRotation struct {
	Key OIDCSigningKey
	// Note is the server's own wording about the propagation window. Surfaced
	// verbatim rather than restated here, because the window is the operator's
	// configuration and only the server knows its value.
	Note string
}

// ListOIDCSigningKeys reports the published signing keys and which one is
// signing.
//
// Requires platform admin.
func (c *Client) ListOIDCSigningKeys(ctx context.Context) (*OIDCSigningKeySet, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/oidc/signing-keys")
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data []struct {
			Attributes OIDCSigningKey `json:"attributes"`
		} `json:"data"`
		Meta struct {
			SigningKID *string `json:"signing-kid"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse oidc signing keys response: %w", err)
	}
	out := &OIDCSigningKeySet{Keys: make([]OIDCSigningKey, 0, len(doc.Data))}
	for _, d := range doc.Data {
		out.Keys = append(out.Keys, d.Attributes)
	}
	if doc.Meta.SigningKID != nil {
		out.SigningKID = *doc.Meta.SigningKID
	}
	return out, nil
}

// RotateOIDCSigningKey adds a signing key and retires the one currently
// signing. The new key is published immediately and begins signing only after
// the deployment's propagation window, so the returned key's Signing is false.
//
// Requires platform admin. Returns *ConflictError when the deployment signs
// with an operator-supplied key — there is nothing for Terrapod to rotate, and
// replacing it is the operator's own key management.
func (c *Client) RotateOIDCSigningKey(ctx context.Context) (*OIDCSigningKeyRotation, error) {
	body, err := c.Post(ctx, "/api/terrapod/v1/oidc/signing-keys/actions/rotate", nil)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			Attributes OIDCSigningKey `json:"attributes"`
		} `json:"data"`
		Meta struct {
			Note string `json:"note"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse oidc signing key rotation response: %w", err)
	}
	return &OIDCSigningKeyRotation{Key: doc.Data.Attributes, Note: doc.Meta.Note}, nil
}
