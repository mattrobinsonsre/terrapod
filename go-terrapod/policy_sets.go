package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
)

// PolicySet is an OPA policy set that scopes a collection of .rego
// policies to workspaces via label/name selectors.
type PolicySet struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	Description      string `json:"description,omitempty"`
	EnforcementLevel string `json:"enforcement-level"`
	Enabled          bool   `json:"enabled"`

	// SharedEvaluation loads the set's policies, helpers and data files into
	// ONE opa eval so they can share (#1842). Opt-in: false keeps a set
	// evaluating one policy at a time, which is what every set did before.
	//
	// Results become per-SET when it is on. Every file shares
	// `package terrapod`, so evaluated together OPA cannot say which file
	// produced which denial, and a per-policy breakdown would be invented.
	SharedEvaluation bool `json:"shared-evaluation"`
	// SupportFileNames are the data files and helpers the VCS sync found, so
	// an operator can see their files were picked up. Read-only; the content
	// is not served here, only the runner's policy bundle carries it.
	SupportFileNames []string `json:"support-file-names"`

	GlobalScope bool  `json:"global-scope"`
	PolicyCount int64 `json:"policy-count"`

	// AllowLabels/DenyLabels scope the set to workspaces when GlobalScope is
	// false. They were settable through Create/Update from the start but never
	// read back, so a caller could scope a set and then not see the scoping it
	// had applied — undetectable drift for anything managing a set as code.
	//
	// The shape is a Role's, deliberately rather than incidentally: the server
	// matches both with the same code (policy_set_service._labels_match), so
	// one key binds one accepted value here exactly as it does there.
	AllowLabels map[string]string `json:"allow-labels,omitempty"`
	DenyLabels  map[string]string `json:"deny-labels,omitempty"`

	// AllowNames/DenyNames scope the set by workspace NAME. They had the same
	// write-but-never-read gap the label rules had, with a worse consequence:
	// the provider's Read left the prior state in place, so changing a name
	// rule elsewhere showed no diff, and after `terraform import` the rules
	// read as null and the next apply PATCHed them to `[]` — silently erasing
	// the scoping, with no plan beforehand to show it happening.
	AllowNames []string `json:"allow-names,omitempty"`
	DenyNames  []string `json:"deny-names,omitempty"`

	// Source discriminator: "inline" (default) or "vcs".
	Source string `json:"source"`

	// VCS fields (populated when Source == "vcs").
	// VCSConnectionID includes the "vcs-" prefix (e.g. "vcs-<uuid>"),
	// matching the format used by VCS connection endpoints.
	VCSConnectionID  string `json:"vcs-connection-id,omitempty"`
	VCSRepoURL       string `json:"vcs-repo-url,omitempty"`
	VCSBranch        string `json:"vcs-branch,omitempty"`
	PolicyPath       string `json:"policy-path,omitempty"`
	VCSLastCommitSHA string `json:"vcs-last-commit-sha,omitempty"`
	VCSLastSyncedAt  string `json:"vcs-last-synced-at,omitempty"`
	VCSLastError     string `json:"vcs-last-error,omitempty"`

	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// CreatePolicySetRequest is the input shape for CreatePolicySet.
type CreatePolicySetRequest struct {
	Name             string
	Description      string
	EnforcementLevel string
	Enabled          bool
	SharedEvaluation bool
	GlobalScope      bool
	AllowLabels      map[string]string
	AllowNames       []string
	DenyLabels       map[string]string
	DenyNames        []string

	// VCS fields (set Source to "vcs" to create a VCS-backed set).
	Source          string
	VCSConnectionID string
	VCSRepoURL      string
	VCSBranch       string
	PolicyPath      string
}

// UpdatePolicySetRequest is the partial-update shape.
type UpdatePolicySetRequest struct {
	Name             *string
	Description      *string
	EnforcementLevel *string
	Enabled          *bool
	SharedEvaluation *bool
	GlobalScope      *bool
	AllowLabels      map[string]string
	AllowNames       []string
	DenyLabels       map[string]string
	DenyNames        []string
	VCSRepoURL       *string
	VCSBranch        *string
	PolicyPath       *string
}

// CreatePolicySet creates a new policy set.
func (c *Client) CreatePolicySet(ctx context.Context, req CreatePolicySetRequest) (*PolicySet, error) {
	body, err := MarshalResource("policy-sets", policySetCreateAttrs(req), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create policy-set: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/policy-sets", body)
	if err != nil {
		return nil, err
	}
	return parsePolicySet(data)
}

// GetPolicySet reads a policy set by id.
func (c *Client) GetPolicySet(ctx context.Context, id string) (*PolicySet, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/policy-sets/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parsePolicySet(data)
}

// ListPolicySets returns all policy sets.
func (c *Client) ListPolicySets(ctx context.Context) ([]PolicySet, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/policy-sets")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]PolicySet, 0, len(resources))
	for i := range resources {
		out = append(out, *policySetFromResource(&resources[i]))
	}
	return out, nil
}

// UpdatePolicySet patches a policy set.
func (c *Client) UpdatePolicySet(ctx context.Context, id string, req UpdatePolicySetRequest) (*PolicySet, error) {
	body, err := MarshalResourceWithID(id, "policy-sets", policySetUpdateAttrs(req))
	if err != nil {
		return nil, fmt.Errorf("marshal update policy-set: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/policy-sets/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parsePolicySet(data)
}

// DeletePolicySet removes a policy set and its policies.
func (c *Client) DeletePolicySet(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/terrapod/v1/policy-sets/"+url.PathEscape(id))
}

// SyncPolicySet triggers an immediate sync of a VCS-sourced policy set.
// Returns the policy set state at the time of enqueue (sync happens
// asynchronously). Returns an error if the policy set is not VCS-sourced.
func (c *Client) SyncPolicySet(ctx context.Context, id string) (*PolicySet, error) {
	path := fmt.Sprintf("/api/terrapod/v1/policy-sets/%s/actions/sync", url.PathEscape(id))
	data, err := c.Post(ctx, path, nil)
	if err != nil {
		return nil, err
	}
	return parsePolicySet(data)
}

// ── Internal helpers ─────────────────────────────────────────────────

func policySetCreateAttrs(req CreatePolicySetRequest) map[string]any {
	attrs := map[string]any{
		"name":              req.Name,
		"enabled":           req.Enabled,
		"shared-evaluation": req.SharedEvaluation,
		"global-scope":      req.GlobalScope,
	}
	// Omitted rather than sent empty, like every other optional field here.
	// The server reads `attrs.get("enforcement-level", "advisory")`, so a key
	// present with "" does not fall back to the default -- it reaches the
	// allow-list and 422s. Sending it unconditionally made the minimal
	// documented block (`resource "terrapod_policy_set" "x" { name = "x" }`)
	// fail outright, because the provider leaves an unset Optional+Computed
	// attribute unknown and ValueString() renders that as "".
	if req.EnforcementLevel != "" {
		attrs["enforcement-level"] = req.EnforcementLevel
	}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	if len(req.AllowLabels) > 0 {
		attrs["allow-labels"] = req.AllowLabels
	}
	if len(req.AllowNames) > 0 {
		attrs["allow-names"] = req.AllowNames
	}
	if len(req.DenyLabels) > 0 {
		attrs["deny-labels"] = req.DenyLabels
	}
	if len(req.DenyNames) > 0 {
		attrs["deny-names"] = req.DenyNames
	}
	if req.Source != "" {
		attrs["source"] = req.Source
	}
	if req.VCSConnectionID != "" {
		attrs["vcs-connection-id"] = req.VCSConnectionID
	}
	if req.VCSRepoURL != "" {
		attrs["vcs-repo-url"] = req.VCSRepoURL
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	if req.PolicyPath != "" {
		attrs["policy-path"] = req.PolicyPath
	}
	return attrs
}

func policySetUpdateAttrs(req UpdatePolicySetRequest) map[string]any {
	attrs := map[string]any{}
	if req.Name != nil {
		attrs["name"] = *req.Name
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	if req.SharedEvaluation != nil {
		attrs["shared-evaluation"] = *req.SharedEvaluation
	}
	if req.EnforcementLevel != nil {
		attrs["enforcement-level"] = *req.EnforcementLevel
	}
	if req.Enabled != nil {
		attrs["enabled"] = *req.Enabled
	}
	if req.GlobalScope != nil {
		attrs["global-scope"] = *req.GlobalScope
	}
	if req.AllowLabels != nil {
		attrs["allow-labels"] = req.AllowLabels
	}
	if req.AllowNames != nil {
		attrs["allow-names"] = req.AllowNames
	}
	if req.DenyLabels != nil {
		attrs["deny-labels"] = req.DenyLabels
	}
	if req.DenyNames != nil {
		attrs["deny-names"] = req.DenyNames
	}
	if req.VCSRepoURL != nil {
		attrs["vcs-repo-url"] = *req.VCSRepoURL
	}
	if req.VCSBranch != nil {
		attrs["vcs-branch"] = *req.VCSBranch
	}
	if req.PolicyPath != nil {
		attrs["policy-path"] = *req.PolicyPath
	}
	return attrs
}

func parsePolicySet(body []byte) (*PolicySet, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse policy-set response: %w", err)
	}
	return policySetFromResource(res), nil
}

// labelRuleAttr reads a label rule off a JSON:API resource. It differs from
// GetMapAttr in one respect that matters to a caller managing scope as code:
// an empty object decodes to nil, not to an empty map. A set with no scoping
// and a set scoped to nothing are different things, and only the first is what
// `{}` means.
func labelRuleAttr(res *Resource, name string) map[string]string {
	rule := GetMapAttr(res, name)
	if len(rule) == 0 {
		return nil
	}
	return rule
}

func policySetFromResource(res *Resource) *PolicySet {
	return &PolicySet{
		ID:               res.ID,
		Name:             GetStringAttr(res, "name"),
		Description:      GetStringAttr(res, "description"),
		EnforcementLevel: GetStringAttr(res, "enforcement-level"),
		Enabled:          GetBoolAttr(res, "enabled"),
		SharedEvaluation: GetBoolAttr(res, "shared-evaluation"),
		SupportFileNames: GetListAttr(res, "support-file-names"),
		GlobalScope:      GetBoolAttr(res, "global-scope"),
		PolicyCount:      GetIntAttr(res, "policy-count"),
		AllowLabels:      labelRuleAttr(res, "allow-labels"),
		DenyLabels:       labelRuleAttr(res, "deny-labels"),
		AllowNames:       GetListAttr(res, "allow-names"),
		DenyNames:        GetListAttr(res, "deny-names"),
		Source:           GetStringAttr(res, "source"),
		VCSConnectionID:  GetStringAttr(res, "vcs-connection-id"),
		VCSRepoURL:       GetStringAttr(res, "vcs-repo-url"),
		VCSBranch:        GetStringAttr(res, "vcs-branch"),
		PolicyPath:       GetStringAttr(res, "policy-path"),
		VCSLastCommitSHA: GetStringAttr(res, "vcs-last-commit-sha"),
		VCSLastSyncedAt:  GetStringAttr(res, "vcs-last-synced-at"),
		VCSLastError:     GetStringAttr(res, "vcs-last-error"),
		CreatedAt:        GetStringAttr(res, "created-at"),
		UpdatedAt:        GetStringAttr(res, "updated-at"),
	}
}

// ── Individual policies ──────────────────────────────────────────────
//
// A Policy is a single .rego document inside an inline (non-VCS) policy
// set. VCS-sourced sets manage their policies from the linked repository,
// so Add/Update/Delete on those return a 409 ConflictError.

// Policy is a single Rego policy within a policy set.
type Policy struct {
	ID          string `json:"id"`
	PolicySetID string `json:"policy-set-id"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	Rego        string `json:"rego"`
	CreatedAt   string `json:"created-at,omitempty"`
	UpdatedAt   string `json:"updated-at,omitempty"`
}

// AddPolicyRequest is the input shape for AddPolicy.
type AddPolicyRequest struct {
	Name        string
	Description string
	Rego        string
}

// UpdatePolicyRequest is the partial-update shape for UpdatePolicy.
type UpdatePolicyRequest struct {
	Name        *string
	Description *string
	Rego        *string
}

// AddPolicy adds a Rego policy to an inline policy set. The server runs
// `opa check` on the Rego and returns 422 on a syntax error, 409 if the
// set already has a policy with that name, and 409 if the set is
// VCS-sourced.
func (c *Client) AddPolicy(ctx context.Context, policySetID string, req AddPolicyRequest) (*Policy, error) {
	attrs := map[string]any{"name": req.Name, "rego": req.Rego}
	if req.Description != "" {
		attrs["description"] = req.Description
	}
	body, err := MarshalResource("policies", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal add policy: %w", err)
	}
	path := fmt.Sprintf("/api/terrapod/v1/policy-sets/%s/policies", url.PathEscape(policySetID))
	data, err := c.Post(ctx, path, body)
	if err != nil {
		return nil, err
	}
	return parsePolicy(data)
}

// UpdatePolicy patches a single policy. A changed Rego is re-validated
// (422 on syntax error); 409 if the parent set is VCS-sourced.
func (c *Client) UpdatePolicy(ctx context.Context, policyID string, req UpdatePolicyRequest) (*Policy, error) {
	attrs := map[string]any{}
	if req.Name != nil {
		attrs["name"] = *req.Name
	}
	if req.Description != nil {
		attrs["description"] = *req.Description
	}
	if req.Rego != nil {
		attrs["rego"] = *req.Rego
	}
	body, err := MarshalResourceWithID(policyID, "policies", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update policy: %w", err)
	}
	data, err := c.Patch(ctx, "/api/terrapod/v1/policies/"+url.PathEscape(policyID), body)
	if err != nil {
		return nil, err
	}
	return parsePolicy(data)
}

// DeletePolicy removes a single policy. 409 if the parent set is VCS-sourced.
func (c *Client) DeletePolicy(ctx context.Context, policyID string) error {
	return c.Delete(ctx, "/api/terrapod/v1/policies/"+url.PathEscape(policyID))
}

func parsePolicy(body []byte) (*Policy, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse policy response: %w", err)
	}
	return policyFromResource(res), nil
}

func policyFromResource(res *Resource) *Policy {
	return &Policy{
		ID:          res.ID,
		PolicySetID: GetRelationshipID(res, "policy-set"),
		Name:        GetStringAttr(res, "name"),
		Description: GetStringAttr(res, "description"),
		Rego:        GetStringAttr(res, "rego"),
		CreatedAt:   GetStringAttr(res, "created-at"),
		UpdatedAt:   GetStringAttr(res, "updated-at"),
	}
}

// ListPolicies returns the policies of an inline policy set.
//
// There is no `GET /policies/{id}` — the server never grew one, because the
// admin UI reads a set and its policies together. `GET /policy-sets/{id}`
// embeds them in `relationships.policies.data` as full resource objects
// rather than as linkage, which is a deviation from JSON:API's usual shape
// but is what the endpoint does; this reads them from there.
//
// A consumer that needs one policy fetches the set and picks it out. That is
// one request either way, since the set is where they live.
func (c *Client) ListPolicies(ctx context.Context, policySetID string) ([]Policy, error) {
	data, err := c.Get(ctx, "/api/terrapod/v1/policy-sets/"+url.PathEscape(policySetID))
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse policy-set response: %w", err)
	}

	rel, ok := res.Relationships["policies"]
	if !ok || len(rel.Data) == 0 {
		// A set with no policies, or a VCS set that has not synced yet.
		return nil, nil
	}

	var embedded []Resource
	if err := json.Unmarshal(rel.Data, &embedded); err != nil {
		return nil, fmt.Errorf("parse embedded policies: %w", err)
	}

	out := make([]Policy, 0, len(embedded))
	for i := range embedded {
		out = append(out, *policyFromResource(&embedded[i]))
	}
	return out, nil
}

// GetPolicy returns one policy of a set, or a NotFoundError when the set no
// longer holds it — which is what a consumer tracking a single policy needs
// in order to tell "deleted elsewhere" from "the request failed".
func (c *Client) GetPolicy(ctx context.Context, policySetID, policyID string) (*Policy, error) {
	policies, err := c.ListPolicies(ctx, policySetID)
	if err != nil {
		return nil, err
	}
	for i := range policies {
		if policies[i].ID == policyID {
			return &policies[i], nil
		}
	}
	return nil, &NotFoundError{Resource: "policy", ID: policyID}
}
