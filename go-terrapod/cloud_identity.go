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
	Keys []OIDCSigningKey `json:"keys"`
	// SigningKID is tagged, like every other field here, because this struct is
	// marshalled straight to an MCP agent as the tool's structured output. An
	// untagged field reaches it as `SigningKID`, so a tool description naming
	// anything else sends the agent looking for a key that is not there.
	SigningKID string `json:"signing-kid"`
}

// OIDCSigningKeyRotation is the result of rotating the signing key: the key
// that was added, and the server's note about when it begins signing.
type OIDCSigningKeyRotation struct {
	Key OIDCSigningKey `json:"key"`
	// Note is the server's own wording about the propagation window. Surfaced
	// verbatim rather than restated here, because the window is the operator's
	// configuration and only the server knows its value.
	Note string `json:"note"`
}

// OIDCAudienceDefaults is the deployment-wide audience catalogue that a
// workspace's own `OIDCAudiences` map merges OVER, per key (#1901).
//
// It exists because the merge is otherwise unobservable: a workspace read
// returns the merged map with no marker saying which entries the workspace owns,
// so without the catalogue beside it an operator cannot tell an inherited entry
// from one of their own -- and a consumer that writes the merged value back
// wholesale promotes every inherited entry into an override.
//
// IssuerEnabled is reported separately because an empty catalogue and a
// disabled issuer are different states that produce the same symptom ("my
// workspace minted nothing"), and only one of them is fixed by adding audiences.
type OIDCAudienceDefaults struct {
	Audiences     map[string][]string `json:"audiences"`
	IssuerEnabled bool                `json:"issuer-enabled"`
}

// GetOIDCAudienceDefaults reports the deployment-wide audience catalogue.
//
// Any authenticated user. A workspace read already discloses that workspace's
// merged audiences to anyone who can read it, so this adds only the entries a
// workspace does not override -- and knowing an audience grants nothing on its
// own, because the federation target's own trust policy is the gate and minting
// needs a phase-bound runner token scoped to a run.
//
// Audiences is empty when the deployment configures no catalogue, which is the
// default and is not an error.
func (c *Client) GetOIDCAudienceDefaults(ctx context.Context) (*OIDCAudienceDefaults, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/oidc/audience-defaults")
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			Attributes OIDCAudienceDefaults `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse oidc audience defaults response: %w", err)
	}
	out := doc.Data.Attributes
	if out.Audiences == nil {
		// A nil map and an empty one mean the same thing here, and a consumer
		// ranging over the result should not have to tell them apart.
		out.Audiences = map[string][]string{}
	}
	return &out, nil
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
