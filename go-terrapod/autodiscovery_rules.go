package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
)

// AutodiscoveryRule is the platform-level config that auto-creates
// workspaces when a PR or default-branch push touches a path matching
// the rule's pattern. See terrapod #283 / docs/autodiscovery.md.
//
// The set of template fields (run-task-templates, notification-
// templates, etc.) is open-ended on the API side, so the SDK exposes
// the create/update payload as raw map[string]any. The Attributes
// field on the result is the parsed server response, so callers can
// read computed fields like created-at/updated-at without re-parsing
// the underlying JSON:API document.
type AutodiscoveryRule struct {
	ID         string
	Attributes map[string]any
	CreatedAt  string
	UpdatedAt  string
}

// CreateAutodiscoveryRule creates a new rule with the given JSON:API
// attribute map. The map keys are the hyphenated wire names — same
// shape as the server stores. The SDK doesn't validate the contents;
// the server returns 422 on invalid attribute names.
func (c *Client) CreateAutodiscoveryRule(ctx context.Context, attrs map[string]any) (*AutodiscoveryRule, error) {
	body, err := MarshalResource("autodiscovery-rules", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create autodiscovery-rule: %w", err)
	}
	data, err := c.Post(ctx, "/api/v1/autodiscovery-rules", body)
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRule(data)
}

// GetAutodiscoveryRule reads a rule by id.
func (c *Client) GetAutodiscoveryRule(ctx context.Context, id string) (*AutodiscoveryRule, error) {
	data, err := c.Get(ctx, "/api/v1/autodiscovery-rules/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRule(data)
}

// UpdateAutodiscoveryRule patches a rule. Attribute map semantics
// mirror create: attributes present in the map are updated, absent
// keys are left alone.
func (c *Client) UpdateAutodiscoveryRule(ctx context.Context, id string, attrs map[string]any) (*AutodiscoveryRule, error) {
	body, err := MarshalResourceWithID(id, "autodiscovery-rules", attrs)
	if err != nil {
		return nil, fmt.Errorf("marshal update autodiscovery-rule: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v1/autodiscovery-rules/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRule(data)
}

// PropagateOptions are the per-request options for a propagating rule update.
type PropagateOptions struct {
	// DryRun previews the propagation and persists neither the rule nor any workspace.
	DryRun bool
}

// WorkspaceRef names one workspace in a propagation report.
type WorkspaceRef struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// CustomisedField is one skipped field: the workspace's value, the rule's old one and its new one.
type CustomisedField struct {
	Current  any `json:"current"`
	Previous any `json:"previous"`
	New      any `json:"new"`
}

// CustomisedWorkspace is a workspace whose fields were skipped because it no longer holds the rule's previous value.
type CustomisedWorkspace struct {
	ID     string                     `json:"id"`
	Name   string                     `json:"name"`
	Fields map[string]CustomisedField `json:"fields"`
}

// Propagation is what a propagating rule update did, or with DryRun would do, to the rule's workspaces.
type Propagation struct {
	DryRun           bool                  `json:"dry_run"`
	Matched          int                   `json:"matched"`
	WouldChange      []WorkspaceChange     `json:"would_change"`
	Customised       []CustomisedWorkspace `json:"customised"`
	Unchanged        []WorkspaceRef        `json:"unchanged"`
	SkippedLifecycle int                   `json:"skipped_lifecycle"`
}

// UpdateAutodiscoveryRuleWithPropagation patches a rule and rolls its changed fields out to existing workspaces;
// a server that omits the report returns the rule with an error.
func (c *Client) UpdateAutodiscoveryRuleWithPropagation(ctx context.Context, id string, attrs map[string]any, opts PropagateOptions) (*AutodiscoveryRule, *Propagation, error) {
	options := map[string]any{"propagate": true}
	if opts.DryRun {
		options["dry_run"] = true
	}
	body, err := MarshalResourceWithOptions(id, "autodiscovery-rules", attrs, nil, options)
	if err != nil {
		return nil, nil, fmt.Errorf("marshal update autodiscovery-rule: %w", err)
	}
	data, err := c.Patch(ctx, "/api/v1/autodiscovery-rules/"+url.PathEscape(id), body)
	if err != nil {
		return nil, nil, err
	}
	rule, err := parseAutodiscoveryRule(data)
	if err != nil {
		return nil, nil, err
	}
	var out struct {
		Meta struct {
			Propagation *Propagation `json:"propagation"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return rule, nil, fmt.Errorf("parse autodiscovery-rule propagation: %w", err)
	}
	if out.Meta.Propagation == nil {
		return rule, nil, fmt.Errorf("the server did not report a propagation; the rule was updated but may not have propagated")
	}
	return rule, out.Meta.Propagation, nil
}

// DeleteAutodiscoveryRule removes a rule. Workspaces already
// autodiscovered from this rule continue to exist; they only lose
// the link back to the source rule.
func (c *Client) DeleteAutodiscoveryRule(ctx context.Context, id string) error {
	return c.Delete(ctx, "/api/v1/autodiscovery-rules/"+url.PathEscape(id))
}

// ── Internal helpers ─────────────────────────────────────────────────

func parseAutodiscoveryRule(body []byte) (*AutodiscoveryRule, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse autodiscovery-rule response: %w", err)
	}
	rule := &AutodiscoveryRule{
		ID:         res.ID,
		Attributes: map[string]any{},
		CreatedAt:  GetStringAttr(res, "created-at"),
		UpdatedAt:  GetStringAttr(res, "updated-at"),
	}
	for k, raw := range res.Attributes {
		var v any
		if err := json.Unmarshal(raw, &v); err == nil {
			rule.Attributes[k] = v
		}
	}
	return rule, nil
}

// autodiscoveryRulesPath is the collection, named once so the paging
// helper and the single-shot list cannot disagree about it.
const autodiscoveryRulesPath = "/api/v1/autodiscovery-rules"

// AutodiscoveryPreviewEntry is one workspace a rule would create.
//
// The keys here are snake_case where the rest of the API is kebab-case,
// because these rows are DATA nested inside the preview document's
// `entries` attribute rather than JSON:API attributes of their own. The
// web preview panel reads them in this shape, so the spelling is a wire
// contract rather than an oversight to tidy.
type AutodiscoveryPreviewEntry struct {
	WorkspaceName string `json:"workspace_name"`
	// WorkingDirectory is the repository-relative directory the rule
	// matched, which becomes the workspace's working directory.
	WorkingDirectory string `json:"working_directory"`
	// Collision is true when a workspace of that name already exists
	// and was NOT created by autodiscovery -- the rule will not take it
	// over, so the entry is reported and skipped.
	Collision bool `json:"collision"`
	// ExistingAutodiscovered is true when the rule has already created
	// this workspace, so a scan would leave it alone rather than
	// create it again.
	ExistingAutodiscovered bool `json:"existing_autodiscovered"`
}

// AutodiscoveryRulePreview is a dry run: what a rule finds right now,
// having created nothing.
type AutodiscoveryRulePreview struct {
	// Ref is the branch actually walked, which is the rule's own branch
	// when it sets one and the repository's default branch otherwise.
	Ref string `json:"ref"`
	// FilesWalked is how many paths the walk returned, so a zero with
	// no entries distinguishes "nothing matched" from "nothing to match".
	FilesWalked int                         `json:"files-walked"`
	Entries     []AutodiscoveryPreviewEntry `json:"entries"`
}

// AutodiscoveryScan is the result of an on-demand scan, which unlike a
// preview DOES create workspaces.
type AutodiscoveryScan struct {
	Ref         string `json:"ref"`
	FilesWalked int    `json:"files-walked"`
	// WorkspacesCreated and WorkspaceIDs cover NEWLY created workspaces
	// only. A rule that has already materialised everything it matches
	// reports zero, which is success rather than a no-op to investigate.
	WorkspacesCreated int      `json:"workspaces-created"`
	WorkspaceIDs      []string `json:"workspace-ids"`
}

// ListAutodiscoveryRules reads one page of rules. Terrapod's list
// convention is that omitting the paging params returns the whole
// collection, so this returns everything unless the server has a
// bounded default for it.
func (c *Client) ListAutodiscoveryRules(ctx context.Context) ([]AutodiscoveryRule, error) {
	data, err := c.Get(ctx, autodiscoveryRulesPath)
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryRuleList(data)
}

// ListAllAutodiscoveryRules pages through every rule, for a caller that
// must not depend on the server's "absent means all" default.
func (c *Client) ListAllAutodiscoveryRules(ctx context.Context) ([]AutodiscoveryRule, error) {
	const pageSize = 100
	all := []AutodiscoveryRule{}
	for page := 1; ; page++ {
		q := url.Values{}
		q.Set("page[number]", strconv.Itoa(page))
		q.Set("page[size]", strconv.Itoa(pageSize))
		data, err := c.Get(ctx, autodiscoveryRulesPath+"?"+q.Encode())
		if err != nil {
			return nil, err
		}
		rules, err := parseAutodiscoveryRuleList(data)
		if err != nil {
			return nil, err
		}
		all = append(all, rules...)
		if len(rules) < pageSize {
			return all, nil
		}
	}
}

// PreviewAutodiscoveryRule dry-runs a SAVED rule: it walks the
// repository and reports what it would create, creating nothing.
func (c *Client) PreviewAutodiscoveryRule(ctx context.Context, id string) (*AutodiscoveryRulePreview, error) {
	data, err := c.Get(ctx, autodiscoveryRulesPath+"/"+url.PathEscape(id)+"/preview")
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryPreview(data)
}

// PreviewUnsavedAutodiscoveryRule dry-runs an UNSAVED rule from the
// attribute map that would create it, so a caller can see what a rule
// would do before committing to it. Takes the same attribute shape as
// CreateAutodiscoveryRule.
func (c *Client) PreviewUnsavedAutodiscoveryRule(ctx context.Context, attrs map[string]any) (*AutodiscoveryRulePreview, error) {
	body, err := MarshalResource("autodiscovery-rules", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal preview autodiscovery-rule: %w", err)
	}
	data, err := c.Post(ctx, autodiscoveryRulesPath+"/preview", body)
	if err != nil {
		return nil, err
	}
	return parseAutodiscoveryPreview(data)
}

// ScanAutodiscoveryRule runs a full scan and MATERIALISES the
// workspaces it finds. Unlike a preview this writes, and it runs even
// on a disabled rule -- the API is explicit that an on-demand scan does
// not silently no-op because the rule's own polling is switched off.
func (c *Client) ScanAutodiscoveryRule(ctx context.Context, id string) (*AutodiscoveryScan, error) {
	data, err := c.Post(ctx, autodiscoveryRulesPath+"/"+url.PathEscape(id)+"/scan", nil)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse autodiscovery-rule scan response: %w", err)
	}
	out := &AutodiscoveryScan{
		Ref:               GetStringAttr(res, "ref"),
		FilesWalked:       int(GetIntAttr(res, "files-walked")),
		WorkspacesCreated: int(GetIntAttr(res, "workspaces-created")),
	}
	if raw, ok := res.Attributes["workspace-ids"]; ok {
		if err := json.Unmarshal(raw, &out.WorkspaceIDs); err != nil {
			return nil, fmt.Errorf("parse autodiscovery-rule scan workspace-ids: %w", err)
		}
	}
	return out, nil
}

func parseAutodiscoveryPreview(body []byte) (*AutodiscoveryRulePreview, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse autodiscovery-rule preview response: %w", err)
	}
	out := &AutodiscoveryRulePreview{
		Ref:         GetStringAttr(res, "ref"),
		FilesWalked: int(GetIntAttr(res, "files-walked")),
	}
	if raw, ok := res.Attributes["entries"]; ok {
		if err := json.Unmarshal(raw, &out.Entries); err != nil {
			return nil, fmt.Errorf("parse autodiscovery-rule preview entries: %w", err)
		}
	}
	return out, nil
}

func parseAutodiscoveryRuleList(body []byte) ([]AutodiscoveryRule, error) {
	list, err := ParseResourceList(body)
	if err != nil {
		return nil, fmt.Errorf("parse autodiscovery-rule list response: %w", err)
	}
	out := make([]AutodiscoveryRule, 0, len(list))
	for i := range list {
		res := &list[i]
		rule := AutodiscoveryRule{
			ID:         res.ID,
			Attributes: map[string]any{},
			CreatedAt:  GetStringAttr(res, "created-at"),
			UpdatedAt:  GetStringAttr(res, "updated-at"),
		}
		for k, raw := range res.Attributes {
			var v any
			if err := json.Unmarshal(raw, &v); err == nil {
				rule.Attributes[k] = v
			}
		}
		out = append(out, rule)
	}
	return out, nil
}
