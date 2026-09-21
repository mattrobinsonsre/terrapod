package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
)

// VaultStatus is the admin view of every configured Vault instance (#1663).
//
// It is read from a sample the server takes once a minute, never by contacting
// Vault on the request, so it is cheap to poll. Enabled false means the Vault
// value source is off and Instances is empty.
//
// Requires platform admin or audit.
type VaultStatus struct {
	Enabled bool `json:"enabled"`
	// SampledAt is when the last sample was taken, RFC3339; empty before the
	// first one.
	SampledAt string `json:"sampled-at"`
	// UnavailableReason is set when there is no usable sample yet ("not
	// sampled yet", "cache unreachable"). Instances are still listed, with
	// their probe fields nil — unknown, not failing.
	UnavailableReason string                `json:"unavailable-reason"`
	Instances         []VaultInstanceStatus `json:"instances"`
}

// VaultInstanceStatus is one Vault instance's last sampled state.
//
// Every probe field is a pointer: nil means "not known" (never sampled, or not
// attempted — a sealed Vault is never logged in to), which is deliberately
// different from false.
type VaultInstanceStatus struct {
	Name       string `json:"name"`
	Default    bool   `json:"default"`
	Address    string `json:"address"`
	Namespace  string `json:"namespace"`
	AuthMethod string `json:"auth-method"`
	AuthMount  string `json:"auth-mount"`
	AuthRole   string `json:"auth-role"`
	// TLSTrust is instance-ca, global-bundle, default or skip-verify.
	TLSTrust string `json:"tls-trust"`

	Reachable   *bool  `json:"reachable"`
	Initialized *bool  `json:"initialized"`
	Sealed      *bool  `json:"sealed"`
	Standby     *bool  `json:"standby"`
	Version     string `json:"version"`
	HealthError string `json:"health-error"`

	LoginOK    *bool  `json:"login-ok"`
	LoginError string `json:"login-error"`
	// TTLSeconds is the remaining TTL of Terrapod's Vault token.
	TTLSeconds *int64 `json:"ttl-seconds"`
	CheckedAt  string `json:"checked-at"`

	// LastError is the last failed resolution against this instance, from any
	// run, or nil. Names and causes only — never a value.
	LastError *VaultResolutionError `json:"last-error"`
}

// VaultResolutionError is one recorded resolution failure.
type VaultResolutionError struct {
	// Class is the failure's kind, e.g. VaultDenied, VaultUnavailable,
	// VaultNotFound.
	Class   string `json:"class"`
	Message string `json:"message"`
	At      string `json:"at"`
}

// GetVaultStatus reports the sampled status of every configured Vault instance.
//
// Requires platform admin or audit.
func (c *Client) GetVaultStatus(ctx context.Context) (*VaultStatus, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/admin/vault")
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data []struct {
			Attributes VaultInstanceStatus `json:"attributes"`
		} `json:"data"`
		Meta struct {
			Vault struct {
				Enabled           bool    `json:"enabled"`
				SampledAt         *string `json:"sampled-at"`
				UnavailableReason *string `json:"unavailable-reason"`
			} `json:"vault"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, err
	}
	out := &VaultStatus{Enabled: doc.Meta.Vault.Enabled}
	if doc.Meta.Vault.SampledAt != nil {
		out.SampledAt = *doc.Meta.Vault.SampledAt
	}
	if doc.Meta.Vault.UnavailableReason != nil {
		out.UnavailableReason = *doc.Meta.Vault.UnavailableReason
	}
	for _, d := range doc.Data {
		out.Instances = append(out.Instances, d.Attributes)
	}
	return out, nil
}

// VaultReferenceCheckOptions says what to check: a reference, or a stored
// variable's reference. Exactly one of Reference and VariableID.
type VaultReferenceCheckOptions struct {
	// Reference is a Vault reference as a variable's value holds it, e.g.
	// {"mount": "secret", "path": "apps/x", "field": "token"}.
	Reference map[string]any
	// VariableID checks the reference a vault-sourced variable already holds.
	VariableID string
	// Key is the variable key a file name defaults to. Optional; defaults to
	// the variable's own key when VariableID is given.
	Key string
}

// VaultReferenceCheckItem is one step of a reference check.
type VaultReferenceCheckItem struct {
	// Name is parses, instance, path-allowed, readable or fields-present.
	Name string `json:"name"`
	// Status is pass, fail, skipped or unknown (Vault could not answer).
	Status string `json:"status"`
	Detail string `json:"detail"`
}

// VaultReferenceCheck is what a check found (#1663). It never contains a
// secret value: Keys holds key NAMES, and only for kv-v2 — a dynamic engine is
// never read, because every read of one mints a credential.
type VaultReferenceCheck struct {
	ID                   string                    `json:"id"`
	OK                   bool                      `json:"ok"`
	VaultEnabled         bool                      `json:"vault-enabled"`
	Parses               bool                      `json:"parses"`
	ParseError           string                    `json:"parse-error"`
	Instance             string                    `json:"instance"`
	InstanceKnown        *bool                     `json:"instance-known"`
	Engine               string                    `json:"engine"`
	ReadPath             string                    `json:"read-path"`
	PathAllowed          *bool                     `json:"path-allowed"`
	Readable             *bool                     `json:"readable"`
	Capabilities         []string                  `json:"capabilities"`
	RequiredCapabilities []string                  `json:"required-capabilities"`
	Keys                 []string                  `json:"keys"`
	FieldsPresent        *bool                     `json:"fields-present"`
	MissingFields        []string                  `json:"missing-fields"`
	Notes                []string                  `json:"notes"`
	Checks               []VaultReferenceCheckItem `json:"checks"`
}

// CheckWorkspaceVaultReference checks a Vault reference against a workspace,
// without resolving it.
//
// Requires var:write on the workspace; the key listing additionally needs
// run:plan. Rate-limited per user.
func (c *Client) CheckWorkspaceVaultReference(
	ctx context.Context, workspaceID string, opts VaultReferenceCheckOptions,
) (*VaultReferenceCheck, error) {
	return c.checkVaultReference(ctx,
		"/api/terrapod/v1/workspaces/"+url.PathEscape(AddPrefix(workspaceID, "ws-"))+"/vault-reference-checks", opts)
}

// CheckVariableSetVaultReference checks a Vault reference for a variable set.
//
// Requires platform admin, as writing a variable-set variable does.
func (c *Client) CheckVariableSetVaultReference(
	ctx context.Context, varsetID string, opts VaultReferenceCheckOptions,
) (*VaultReferenceCheck, error) {
	return c.checkVaultReference(ctx,
		"/api/terrapod/v1/varsets/"+url.PathEscape(AddPrefix(varsetID, "varset-"))+"/vault-reference-checks", opts)
}

func (c *Client) checkVaultReference(
	ctx context.Context, path string, opts VaultReferenceCheckOptions,
) (*VaultReferenceCheck, error) {
	if (opts.Reference == nil) == (opts.VariableID == "") {
		return nil, errors.New("give exactly one of Reference and VariableID")
	}
	attrs := map[string]any{}
	if opts.Reference != nil {
		attrs["reference"] = opts.Reference
	} else {
		attrs["variable-id"] = AddPrefix(opts.VariableID, "var-")
	}
	if opts.Key != "" {
		attrs["key"] = opts.Key
	}
	payload, err := MarshalResource("vault-reference-checks", attrs, nil)
	if err != nil {
		return nil, err
	}
	body, err := c.Post(ctx, path, payload)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			ID         string              `json:"id"`
			Attributes VaultReferenceCheck `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, err
	}
	out := doc.Data.Attributes
	out.ID = doc.Data.ID
	return &out, nil
}
