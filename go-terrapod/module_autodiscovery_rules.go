package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
)

// Module autodiscovery rules (#1584): the module registry's counterpart to
// workspace autodiscovery rules. A rule names one repository on a VCS
// connection, a glob pattern (minus ignore patterns) over its Terraform files,
// and how each module it finds is named. A scan registers the modules it finds
// — the root and any submodules — and the registry poller registers new ones
// as they appear on the tracked branch. Platform admin only.

// ModuleAutodiscoveryRule is a rule as the server reports it. ID carries the
// "modrule-" prefix.
type ModuleAutodiscoveryRule struct {
	ID              string            `json:"id"`
	Name            string            `json:"name"`
	VCSConnectionID string            `json:"vcs-connection-id"`
	RepoURL         string            `json:"repo-url"`
	Branch          string            `json:"branch"`
	Pattern         string            `json:"pattern"`
	IgnorePatterns  []string          `json:"ignore-patterns"`
	Enabled         bool              `json:"enabled"`
	NameTemplate    string            `json:"name-template"`
	Provider        string            `json:"provider"`
	VCSTagPattern   string            `json:"vcs-tag-pattern"`
	Labels          map[string]string `json:"labels"`
	OwnerEmail      string            `json:"owner-email"`
	// FirstScanAt is when the rule first read its repository (RFC3339), or
	// empty if it never has.
	FirstScanAt    string `json:"first-scan-at"`
	LastScannedSHA string `json:"last-scanned-sha"`
	CreatedAt      string `json:"created-at"`
	UpdatedAt      string `json:"updated-at"`
}

// ModuleAutodiscoveryRuleRequest is the create/update input. Only non-nil
// fields are sent, so an update leaves everything else as it was. Create needs
// Name, VCSConnectionID, RepoURL and Pattern.
type ModuleAutodiscoveryRuleRequest struct {
	Name            *string
	VCSConnectionID *string
	RepoURL         *string
	Branch          *string
	Pattern         *string
	IgnorePatterns  *[]string
	Enabled         *bool
	NameTemplate    *string
	Provider        *string
	VCSTagPattern   *string
	Labels          *map[string]string
	OwnerEmail      *string
}

// ModuleAutodiscoveryPreview is what a rule finds in its repository.
type ModuleAutodiscoveryPreview struct {
	Ref         string                         `json:"ref"`
	FilesWalked int                            `json:"files-walked"`
	Entries     []ModuleAutodiscoveryCandidate `json:"entries"`
}

// ModuleAutodiscoveryCandidate is one directory the rule counts as a module.
// The repository root has an empty Subdirectory.
type ModuleAutodiscoveryCandidate struct {
	Subdirectory string `json:"subdirectory"`
	Name         string `json:"name"`
	Provider     string `json:"provider"`
	// RegisteredAs is the module already registered from this directory, or
	// nil. A scan skips it.
	RegisteredAs *RegisteredModuleRef `json:"registered-as,omitempty"`
	// Collision: the name and provider already belong to another module.
	Collision bool `json:"collision"`
	// MissingProvider: no provider is set and the repository name implies none.
	MissingProvider bool `json:"missing-provider"`
}

// RegisteredModuleRef names an existing registry module.
type RegisteredModuleRef struct {
	Name     string `json:"name"`
	Provider string `json:"provider"`
}

// ModuleAutodiscoveryScan is the outcome of a scan.
type ModuleAutodiscoveryScan struct {
	Ref               string                      `json:"ref"`
	FilesWalked       int                         `json:"files-walked"`
	ModulesRegistered int                         `json:"modules-registered"`
	Modules           []ModuleAutodiscoveryModule `json:"modules"`
	Skipped           []ModuleAutodiscoverySkip   `json:"skipped"`
}

// ModuleAutodiscoveryModule is a module a scan registered.
type ModuleAutodiscoveryModule struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Provider     string `json:"provider"`
	Subdirectory string `json:"subdirectory"`
}

// ModuleAutodiscoverySkip is a candidate a scan did not register, and why:
// "already-registered", "name-taken" or "missing-provider".
type ModuleAutodiscoverySkip struct {
	Subdirectory string `json:"subdirectory"`
	Reason       string `json:"reason"`
}

const moduleRulesPath = "/api/terrapod/v1/module-autodiscovery-rules"

// ListModuleAutodiscoveryRules returns one page of rules — the whole list when
// the server does not page it. Use ListAllModuleAutodiscoveryRules to be sure.
func (c *Client) ListModuleAutodiscoveryRules(ctx context.Context) ([]ModuleAutodiscoveryRule, error) {
	data, err := c.Get(ctx, moduleRulesPath)
	if err != nil {
		return nil, err
	}
	return parseModuleRuleList(data)
}

// ListAllModuleAutodiscoveryRules pages through every rule.
func (c *Client) ListAllModuleAutodiscoveryRules(ctx context.Context) ([]ModuleAutodiscoveryRule, error) {
	const pageSize = 100
	all := []ModuleAutodiscoveryRule{}
	for page := 1; ; page++ {
		q := url.Values{}
		q.Set("page[number]", strconv.Itoa(page))
		q.Set("page[size]", strconv.Itoa(pageSize))
		data, err := c.Get(ctx, moduleRulesPath+"?"+q.Encode())
		if err != nil {
			return nil, err
		}
		rules, err := parseModuleRuleList(data)
		if err != nil {
			return nil, err
		}
		all = append(all, rules...)
		meta, _ := parseListMeta(data)
		if meta.TotalPages > 0 {
			if page >= meta.TotalPages {
				break
			}
		} else if len(rules) < pageSize {
			break
		}
	}
	return all, nil
}

// CreateModuleAutodiscoveryRule creates a rule. Saving registers nothing: use
// ScanModuleAutodiscoveryRule to register what is already in the repository.
func (c *Client) CreateModuleAutodiscoveryRule(ctx context.Context, req ModuleAutodiscoveryRuleRequest) (*ModuleAutodiscoveryRule, error) {
	body, err := MarshalResource("module-autodiscovery-rules", req.attrs(), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal create module-autodiscovery-rule: %w", err)
	}
	data, err := c.Post(ctx, moduleRulesPath, body)
	if err != nil {
		return nil, err
	}
	return parseModuleRule(data)
}

// GetModuleAutodiscoveryRule reads a rule by id.
func (c *Client) GetModuleAutodiscoveryRule(ctx context.Context, id string) (*ModuleAutodiscoveryRule, error) {
	data, err := c.Get(ctx, moduleRulesPath+"/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	return parseModuleRule(data)
}

// UpdateModuleAutodiscoveryRule changes the fields set in req. Pointing a rule
// at another repository, branch or connection starts it afresh.
func (c *Client) UpdateModuleAutodiscoveryRule(ctx context.Context, id string, req ModuleAutodiscoveryRuleRequest) (*ModuleAutodiscoveryRule, error) {
	body, err := MarshalResourceWithID(id, "module-autodiscovery-rules", req.attrs())
	if err != nil {
		return nil, fmt.Errorf("marshal update module-autodiscovery-rule: %w", err)
	}
	data, err := c.Patch(ctx, moduleRulesPath+"/"+url.PathEscape(id), body)
	if err != nil {
		return nil, err
	}
	return parseModuleRule(data)
}

// DeleteModuleAutodiscoveryRule deletes a rule. Modules it registered stay.
func (c *Client) DeleteModuleAutodiscoveryRule(ctx context.Context, id string) error {
	return c.Delete(ctx, moduleRulesPath+"/"+url.PathEscape(id))
}

// PreviewModuleAutodiscoveryRule reports what a saved rule finds now.
// Registers nothing.
func (c *Client) PreviewModuleAutodiscoveryRule(ctx context.Context, id string) (*ModuleAutodiscoveryPreview, error) {
	data, err := c.Get(ctx, moduleRulesPath+"/"+url.PathEscape(id)+"/preview")
	if err != nil {
		return nil, err
	}
	out := &ModuleAutodiscoveryPreview{}
	if err := decodeAttributes(data, out); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery preview: %w", err)
	}
	if out.Entries == nil {
		out.Entries = []ModuleAutodiscoveryCandidate{}
	}
	return out, nil
}

// PreviewUnsavedModuleAutodiscoveryRule reports what a rule with these
// attributes would find, without saving it. It takes the same fields as create.
func (c *Client) PreviewUnsavedModuleAutodiscoveryRule(ctx context.Context, req ModuleAutodiscoveryRuleRequest) (*ModuleAutodiscoveryPreview, error) {
	body, err := MarshalResource("module-autodiscovery-rules", req.attrs(), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal module-autodiscovery preview: %w", err)
	}
	data, err := c.Post(ctx, moduleRulesPath+"/preview", body)
	if err != nil {
		return nil, err
	}
	out := &ModuleAutodiscoveryPreview{}
	if err := decodeAttributes(data, out); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery preview: %w", err)
	}
	if out.Entries == nil {
		out.Entries = []ModuleAutodiscoveryCandidate{}
	}
	return out, nil
}

// ScanModuleAutodiscoveryRule registers the rule's candidates. A nil
// subdirectories registers all of them; a non-nil one registers just those
// (the repository root is ""). Candidates already registered, or whose name is
// taken, are skipped and reported.
func (c *Client) ScanModuleAutodiscoveryRule(ctx context.Context, id string, subdirectories []string) (*ModuleAutodiscoveryScan, error) {
	attrs := map[string]any{}
	if subdirectories != nil {
		attrs["subdirectories"] = subdirectories
	}
	body, err := MarshalResource("module-autodiscovery-rule-scans", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal module-autodiscovery scan: %w", err)
	}
	data, err := c.Post(ctx, moduleRulesPath+"/"+url.PathEscape(id)+"/scan", body)
	if err != nil {
		return nil, err
	}
	out := &ModuleAutodiscoveryScan{}
	if err := decodeAttributes(data, out); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery scan: %w", err)
	}
	if out.Modules == nil {
		out.Modules = []ModuleAutodiscoveryModule{}
	}
	if out.Skipped == nil {
		out.Skipped = []ModuleAutodiscoverySkip{}
	}
	return out, nil
}

// ── Internal helpers ─────────────────────────────────────────────────

func (r ModuleAutodiscoveryRuleRequest) attrs() map[string]any {
	a := map[string]any{}
	setStr := func(key string, v *string) {
		if v != nil {
			a[key] = *v
		}
	}
	setStr("name", r.Name)
	setStr("vcs-connection-id", r.VCSConnectionID)
	setStr("repo-url", r.RepoURL)
	setStr("branch", r.Branch)
	setStr("pattern", r.Pattern)
	setStr("name-template", r.NameTemplate)
	setStr("provider", r.Provider)
	setStr("vcs-tag-pattern", r.VCSTagPattern)
	setStr("owner-email", r.OwnerEmail)
	if r.IgnorePatterns != nil {
		ip := *r.IgnorePatterns
		if ip == nil {
			ip = []string{}
		}
		a["ignore-patterns"] = ip
	}
	if r.Enabled != nil {
		a["enabled"] = *r.Enabled
	}
	if r.Labels != nil {
		l := *r.Labels
		if l == nil {
			l = map[string]string{}
		}
		a["labels"] = l
	}
	return a
}

// decodeAttributes decodes a single-resource document's attributes into out.
func decodeAttributes(body []byte, out any) error {
	var doc struct {
		Data struct {
			Attributes json.RawMessage `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return err
	}
	if len(doc.Data.Attributes) == 0 {
		return fmt.Errorf("response has no data.attributes")
	}
	return json.Unmarshal(doc.Data.Attributes, out)
}

type moduleRuleDoc struct {
	ID         string                  `json:"id"`
	Attributes ModuleAutodiscoveryRule `json:"attributes"`
}

func (d moduleRuleDoc) rule() ModuleAutodiscoveryRule {
	r := d.Attributes
	r.ID = d.ID
	if r.IgnorePatterns == nil {
		r.IgnorePatterns = []string{}
	}
	if r.Labels == nil {
		r.Labels = map[string]string{}
	}
	return r
}

func parseModuleRule(body []byte) (*ModuleAutodiscoveryRule, error) {
	var doc struct {
		Data moduleRuleDoc `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery-rule response: %w", err)
	}
	if doc.Data.ID == "" {
		return nil, fmt.Errorf("parse module-autodiscovery-rule response: no data.id")
	}
	r := doc.Data.rule()
	return &r, nil
}

func parseModuleRuleList(body []byte) ([]ModuleAutodiscoveryRule, error) {
	var doc struct {
		Data []moduleRuleDoc `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery-rule list: %w", err)
	}
	out := make([]ModuleAutodiscoveryRule, 0, len(doc.Data))
	for _, d := range doc.Data {
		out = append(out, d.rule())
	}
	return out, nil
}
