package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
)

// Module autodiscovery rules (#1584, #1620): the module registry's counterpart
// to workspace autodiscovery rules. A rule's repo-url names one repository, an
// org or group, or a pattern over one namespace's repositories (such as
// https://github.com/acme/terraform-*); the server classifies it when the rule
// is saved and reports the result as TargetKind. The rule also carries a glob
// pattern (minus ignore patterns) over the Terraform files, and how each module
// it finds is named. A scan registers the modules it finds — the root and any
// submodules — and the registry poller registers new ones as they appear.
// Platform admin only.

// The kinds of target a rule's repo-url can name, as reported in TargetKind.
const (
	ModuleAutodiscoveryTargetRepository = "repository"
	ModuleAutodiscoveryTargetNamespace  = "namespace"
	ModuleAutodiscoveryTargetPattern    = "pattern"
)

// ModuleAutodiscoveryRule is a rule as the server reports it. ID carries the
// "modrule-" prefix.
type ModuleAutodiscoveryRule struct {
	ID              string `json:"id"`
	Name            string `json:"name"`
	VCSConnectionID string `json:"vcs-connection-id"`
	RepoURL         string `json:"repo-url"`
	// TargetKind is what RepoURL names: "repository", "namespace" (an org or
	// group) or "pattern". Read-only; decided when the rule is saved.
	TargetKind     string            `json:"target-kind"`
	Branch         string            `json:"branch"`
	Pattern        string            `json:"pattern"`
	IgnorePatterns []string          `json:"ignore-patterns"`
	Enabled        bool              `json:"enabled"`
	NameTemplate   string            `json:"name-template"`
	Provider       string            `json:"provider"`
	VCSTagPattern  string            `json:"vcs-tag-pattern"`
	Labels         map[string]string `json:"labels"`
	OwnerEmail     string            `json:"owner-email"`
	// FirstScanAt is when the rule first read its repository (RFC3339), or
	// empty if it never has.
	FirstScanAt string `json:"first-scan-at"`
	// LastScannedSHA is a single-repository rule's head at its last scan;
	// empty for an org-wide rule, whose heads are per repository.
	LastScannedSHA string `json:"last-scanned-sha"`
	// LastEnumeratedAt is when an org-wide rule last listed its repositories
	// (RFC3339), or empty.
	LastEnumeratedAt string `json:"last-enumerated-at"`
	// LastError is why the rule's last poll failed (its target was deleted, or
	// could not be listed), or empty.
	LastError string `json:"last-error"`
	CreatedAt string `json:"created-at"`
	UpdatedAt string `json:"updated-at"`
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

// ModuleAutodiscoveryPreview is what a rule finds. A single-repository rule's
// preview reads its repository live. An org-wide rule's saved preview is served
// from what the poller last found, a page of repositories at a time, unless one
// repository is asked for (ModuleAutodiscoveryPreviewOptions.Repository).
type ModuleAutodiscoveryPreview struct {
	// Ref is the branch read; empty for a stored org-wide preview.
	Ref         string                         `json:"ref"`
	FilesWalked int                            `json:"files-walked"`
	Entries     []ModuleAutodiscoveryCandidate `json:"entries"`
	// TargetKind is what the rule's repo-url names (see ModuleAutodiscoveryRule).
	TargetKind string `json:"target-kind"`
	// Repositories groups Entries: one per repository on this page, each with
	// its status, including repositories that yielded no candidates.
	Repositories []ModuleAutodiscoveryPreviewRepository `json:"repositories"`
	// ListingComplete is false when an org-wide listing stopped at the
	// server's repository cap, so some repositories are not shown.
	ListingComplete bool `json:"listing-complete"`
	// Pagination is the page of repositories, for an org-wide preview; nil
	// when the preview is not paged.
	Pagination *ListMeta `json:"pagination,omitempty"`
}

// ModuleAutodiscoveryPreviewRepository is one repository in a preview.
type ModuleAutodiscoveryPreviewRepository struct {
	// Repository is the path (owner/repo, or group/subgroup/project).
	Repository string `json:"repository"`
	RepoURL    string `json:"repo-url"`
	Ref        string `json:"ref"`
	// Status: "active", "archived", "empty", "no-branch", "out-of-scope",
	// "covered" (a single-repository rule already names it) or "error".
	Status string `json:"status"`
	// Origin: "baseline" (existed when the rule took its baseline) or "new".
	Origin string `json:"origin"`
	// Error is why the repository could not be read, or empty.
	Error string `json:"error"`
}

// ModuleAutodiscoveryPreviewOptions narrows a saved rule's preview.
type ModuleAutodiscoveryPreviewOptions struct {
	// Repository reads one of an org-wide rule's repositories live (a path or
	// URL) instead of the stored candidates.
	Repository string
	// PageNumber and PageSize page an org-wide rule's repositories. Zero
	// leaves the server's default.
	PageNumber int
	PageSize   int
}

func (o ModuleAutodiscoveryPreviewOptions) query() string {
	q := url.Values{}
	if o.Repository != "" {
		q.Set("repository", o.Repository)
	}
	if o.PageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(o.PageNumber))
	}
	if o.PageSize > 0 {
		q.Set("page[size]", strconv.Itoa(o.PageSize))
	}
	if len(q) == 0 {
		return ""
	}
	return "?" + q.Encode()
}

// ModuleAutodiscoveryCandidate is one directory the rule counts as a module.
// The repository root has an empty Subdirectory.
type ModuleAutodiscoveryCandidate struct {
	// Repository and RepoURL name the repository the directory is in.
	Repository   string `json:"repository"`
	RepoURL      string `json:"repo-url"`
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
	// RepositoriesScanned is how many repositories the scan registered from.
	RepositoriesScanned int `json:"repositories-scanned"`
}

// ModuleAutodiscoveryModule is a module a scan registered.
type ModuleAutodiscoveryModule struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Provider     string `json:"provider"`
	Subdirectory string `json:"subdirectory"`
	Repository   string `json:"repository"`
	RepoURL      string `json:"repo-url"`
}

// ModuleAutodiscoverySkip is a candidate a scan did not register, and why:
// "already-registered", "name-taken" or "missing-provider". Repository and
// RepoURL are set on a scan's skips; a repository's stored LastSkips carry
// only Subdirectory and Reason.
type ModuleAutodiscoverySkip struct {
	Repository   string `json:"repository,omitempty"`
	RepoURL      string `json:"repo-url,omitempty"`
	Subdirectory string `json:"subdirectory"`
	Reason       string `json:"reason"`
}

// ModuleAutodiscoverySelection picks what an org-wide scan registers from one
// repository: every candidate when Subdirectories is nil, otherwise just those
// (the repository root is "").
type ModuleAutodiscoverySelection struct {
	// Repository is the repository's path or URL.
	Repository     string
	Subdirectories []string
}

// ModuleAutodiscoveryRepository is one repository a rule looks at, with the
// state its polls keep. ID carries the "modrepo-" prefix.
type ModuleAutodiscoveryRepository struct {
	ID            string `json:"id"`
	Repository    string `json:"repository"`
	RepoURL       string `json:"repo-url"`
	VCSRepoID     string `json:"vcs-repo-id"`
	DefaultBranch string `json:"default-branch"`
	// Origin: "baseline" (it existed when the rule took its baseline, so
	// nothing registers until it is scanned) or "new" (created afterwards, so
	// its modules register automatically).
	Origin string `json:"origin"`
	// Status: "active", "archived", "empty", "no-branch", "out-of-scope",
	// "covered" or "error".
	Status             string                                  `json:"status"`
	LastScannedSHA     string                                  `json:"last-scanned-sha"`
	SeenSubdirectories []string                                `json:"seen-subdirectories"`
	Candidates         []ModuleAutodiscoveryStoredCandidate    `json:"candidates"`
	LastSkips          []ModuleAutodiscoverySkip               `json:"last-skips"`
	PreviousPaths      []ModuleAutodiscoveryRepositoryPrevious `json:"previous-paths"`
	// Timestamps are RFC3339, or empty when unset.
	RepoCreatedAt string `json:"repo-created-at"`
	FirstSeenAt   string `json:"first-seen-at"`
	LastCheckedAt string `json:"last-checked-at"`
	NextCheckAt   string `json:"next-check-at"`
	FailureCount  int    `json:"failure-count"`
	LastError     string `json:"last-error"`
}

// ModuleAutodiscoveryStoredCandidate is a candidate the last poll of a
// repository found.
type ModuleAutodiscoveryStoredCandidate struct {
	Subdirectory string `json:"subdirectory"`
	Name         string `json:"name"`
	Provider     string `json:"provider"`
}

// ModuleAutodiscoveryRepositoryPrevious is a path (and URL) a repository had
// before it was renamed or transferred.
type ModuleAutodiscoveryRepositoryPrevious struct {
	Path string `json:"path"`
	URL  string `json:"url"`
}

// ModuleAutodiscoveryRepositoryListOptions filters and pages a rule's
// repositories.
type ModuleAutodiscoveryRepositoryListOptions struct {
	// Status keeps only repositories in that status (see
	// ModuleAutodiscoveryRepository.Status).
	Status string
	// PageNumber and PageSize page the list. Zero PageSize returns the whole
	// list in one page.
	PageNumber int
	PageSize   int
}

// ModuleAutodiscoveryRepositoryList is one page of a rule's repositories.
type ModuleAutodiscoveryRepositoryList struct {
	Items      []ModuleAutodiscoveryRepository `json:"items"`
	Pagination ListMeta                        `json:"pagination"`
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
// Registers nothing. For an org-wide rule it returns the server's first page
// of repositories; use PreviewModuleAutodiscoveryRuleWithOptions to page or to
// read one repository live.
func (c *Client) PreviewModuleAutodiscoveryRule(ctx context.Context, id string) (*ModuleAutodiscoveryPreview, error) {
	return c.PreviewModuleAutodiscoveryRuleWithOptions(ctx, id, ModuleAutodiscoveryPreviewOptions{})
}

// PreviewModuleAutodiscoveryRuleWithOptions is PreviewModuleAutodiscoveryRule
// with a page of an org-wide rule's repositories, or one of them read live.
// A repository that is not the rule's is a *NotFoundError (org-wide rule) or
// a *ValidationError (single-repository rule).
func (c *Client) PreviewModuleAutodiscoveryRuleWithOptions(ctx context.Context, id string, opts ModuleAutodiscoveryPreviewOptions) (*ModuleAutodiscoveryPreview, error) {
	data, err := c.Get(ctx, moduleRulesPath+"/"+url.PathEscape(id)+"/preview"+opts.query())
	if err != nil {
		return nil, err
	}
	return parseModulePreview(data)
}

// PreviewUnsavedModuleAutodiscoveryRule reports what a rule with these
// attributes would find, without saving it. It takes the same fields as create.
// For an org-wide target it reads the server's first page of repositories
// live; use PreviewUnsavedModuleAutodiscoveryRuleWithOptions to page.
func (c *Client) PreviewUnsavedModuleAutodiscoveryRule(ctx context.Context, req ModuleAutodiscoveryRuleRequest) (*ModuleAutodiscoveryPreview, error) {
	return c.PreviewUnsavedModuleAutodiscoveryRuleWithOptions(ctx, req, ModuleAutodiscoveryPreviewOptions{})
}

// PreviewUnsavedModuleAutodiscoveryRuleWithOptions is
// PreviewUnsavedModuleAutodiscoveryRule with a page of an org-wide target's
// repositories. opts.Repository is not used here: an unsaved rule has no
// stored repositories.
func (c *Client) PreviewUnsavedModuleAutodiscoveryRuleWithOptions(ctx context.Context, req ModuleAutodiscoveryRuleRequest, opts ModuleAutodiscoveryPreviewOptions) (*ModuleAutodiscoveryPreview, error) {
	body, err := MarshalResource("module-autodiscovery-rules", req.attrs(), nil)
	if err != nil {
		return nil, fmt.Errorf("marshal module-autodiscovery preview: %w", err)
	}
	opts.Repository = ""
	data, err := c.Post(ctx, moduleRulesPath+"/preview"+opts.query(), body)
	if err != nil {
		return nil, err
	}
	return parseModulePreview(data)
}

func parseModulePreview(data []byte) (*ModuleAutodiscoveryPreview, error) {
	// A server that predates org-wide rules sends no listing-complete; its
	// listings are always complete.
	out := &ModuleAutodiscoveryPreview{ListingComplete: true}
	if err := decodeAttributes(data, out); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery preview: %w", err)
	}
	if out.Entries == nil {
		out.Entries = []ModuleAutodiscoveryCandidate{}
	}
	if out.Repositories == nil {
		out.Repositories = []ModuleAutodiscoveryPreviewRepository{}
	}
	if meta, err := parseListMeta(data); err == nil && meta.TotalPages > 0 {
		out.Pagination = &meta
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
	return c.postModuleScan(ctx, id, attrs)
}

// ScanModuleAutodiscoveryRuleSelections registers candidates by repository:
// the form an org-wide rule takes (a single-repository rule accepts it too,
// naming its one repository). A nil or empty selections registers every
// current candidate. An org-wide scan registers from what the poller last
// found, with no VCS calls; a repository with nothing to register, or an
// unknown subdirectory, is a *ValidationError and registers nothing.
func (c *Client) ScanModuleAutodiscoveryRuleSelections(ctx context.Context, id string, selections []ModuleAutodiscoverySelection) (*ModuleAutodiscoveryScan, error) {
	attrs := map[string]any{}
	if len(selections) > 0 {
		sel := make([]map[string]any, 0, len(selections))
		for _, s := range selections {
			item := map[string]any{"repository": s.Repository}
			if s.Subdirectories != nil {
				item["subdirectories"] = s.Subdirectories
			}
			sel = append(sel, item)
		}
		attrs["selections"] = sel
	}
	return c.postModuleScan(ctx, id, attrs)
}

func (c *Client) postModuleScan(ctx context.Context, id string, attrs map[string]any) (*ModuleAutodiscoveryScan, error) {
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

// ListModuleAutodiscoveryRuleRepositories returns one page of the repositories
// a rule looks at, by path: one for a single-repository rule once it has been
// polled, one per listed repository for an org-wide rule.
func (c *Client) ListModuleAutodiscoveryRuleRepositories(ctx context.Context, id string, opts ModuleAutodiscoveryRepositoryListOptions) (*ModuleAutodiscoveryRepositoryList, error) {
	q := url.Values{}
	if opts.Status != "" {
		q.Set("filter[status]", opts.Status)
	}
	if opts.PageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(opts.PageNumber))
	}
	if opts.PageSize > 0 {
		q.Set("page[size]", strconv.Itoa(opts.PageSize))
	}
	path := moduleRulesPath + "/" + url.PathEscape(id) + "/repositories"
	if len(q) > 0 {
		path += "?" + q.Encode()
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data []struct {
			ID         string                        `json:"id"`
			Attributes ModuleAutodiscoveryRepository `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse module-autodiscovery repositories: %w", err)
	}
	out := &ModuleAutodiscoveryRepositoryList{Items: make([]ModuleAutodiscoveryRepository, 0, len(doc.Data))}
	for _, d := range doc.Data {
		r := d.Attributes
		r.ID = d.ID
		if r.SeenSubdirectories == nil {
			r.SeenSubdirectories = []string{}
		}
		if r.Candidates == nil {
			r.Candidates = []ModuleAutodiscoveryStoredCandidate{}
		}
		if r.LastSkips == nil {
			r.LastSkips = []ModuleAutodiscoverySkip{}
		}
		if r.PreviousPaths == nil {
			r.PreviousPaths = []ModuleAutodiscoveryRepositoryPrevious{}
		}
		out.Items = append(out.Items, r)
	}
	out.Pagination, _ = parseListMeta(data)
	return out, nil
}

// ListAllModuleAutodiscoveryRuleRepositories pages through every repository a
// rule looks at, optionally only those in one status.
func (c *Client) ListAllModuleAutodiscoveryRuleRepositories(ctx context.Context, id, status string) ([]ModuleAutodiscoveryRepository, error) {
	const pageSize = 100
	all := []ModuleAutodiscoveryRepository{}
	for page := 1; ; page++ {
		list, err := c.ListModuleAutodiscoveryRuleRepositories(ctx, id, ModuleAutodiscoveryRepositoryListOptions{
			Status: status, PageNumber: page, PageSize: pageSize,
		})
		if err != nil {
			return nil, err
		}
		all = append(all, list.Items...)
		if list.Pagination.TotalPages > 0 {
			if page >= list.Pagination.TotalPages {
				break
			}
		} else if len(list.Items) < pageSize {
			break
		}
	}
	return all, nil
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
