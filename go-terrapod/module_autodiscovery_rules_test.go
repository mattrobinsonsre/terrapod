package terrapod

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

const moduleRuleJSON = `{"data":{"id":"modrule-11111111-1111-1111-1111-111111111111","type":"module-autodiscovery-rules","attributes":{
  "name":"mg","vcs-connection-id":"vcs-22222222-2222-2222-2222-222222222222",
  "repo-url":"https://github.com/org/terraform-azurerm-mg","branch":"","pattern":"**/*.tf",
  "ignore-patterns":["legacy/**"],"enabled":true,"name-template":"","provider":"",
  "vcs-tag-pattern":"v*","labels":{"team":"platform"},"owner-email":"",
  "first-scan-at":null,"last-scanned-sha":"","created-at":"2026-09-14T10:00:00Z","updated-at":"2026-09-14T10:00:00Z"}}}`

type captured struct {
	method, path, query string
	body                map[string]any
}

// moduleRuleServer answers every request with (status, body) and records it.
func moduleRuleServer(t *testing.T, status int, body string) (*Client, *captured) {
	t.Helper()
	got := &captured{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.method, got.path, got.query = r.Method, r.URL.Path, r.URL.RawQuery
		raw, _ := io.ReadAll(r.Body)
		got.body = nil
		if len(raw) > 0 {
			var doc struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Errorf("request body is not JSON:API: %s", raw)
			}
			got.body = doc.Data.Attributes
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, got
}

func sp(s string) *string { return &s }

func TestCreateModuleAutodiscoveryRule(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusCreated, moduleRuleJSON)
	ignore := []string{"legacy/**"}
	r, err := c.CreateModuleAutodiscoveryRule(t.Context(), ModuleAutodiscoveryRuleRequest{
		Name:            sp("mg"),
		VCSConnectionID: sp("vcs-2222"),
		RepoURL:         sp("https://github.com/org/terraform-azurerm-mg"),
		Pattern:         sp("**/*.tf"),
		IgnorePatterns:  &ignore,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPost || got.path != "/api/terrapod/v1/module-autodiscovery-rules" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if got.body["pattern"] != "**/*.tf" || got.body["repo-url"] == nil {
		t.Errorf("sent %+v", got.body)
	}
	if _, ok := got.body["provider"]; ok {
		t.Errorf("an unset field was sent: %+v", got.body)
	}
	if r.ID != "modrule-11111111-1111-1111-1111-111111111111" || r.Name != "mg" || !r.Enabled {
		t.Errorf("rule: %+v", r)
	}
	if len(r.IgnorePatterns) != 1 || r.Labels["team"] != "platform" || r.VCSTagPattern != "v*" {
		t.Errorf("rule fields: %+v", r)
	}
	if r.FirstScanAt != "" {
		t.Errorf("a null first-scan-at should read as empty, got %q", r.FirstScanAt)
	}
}

func TestUpdateModuleAutodiscoveryRuleSendsOnlyWhatIsSet(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, moduleRuleJSON)
	off := false
	if _, err := c.UpdateModuleAutodiscoveryRule(t.Context(), "modrule-1", ModuleAutodiscoveryRuleRequest{Enabled: &off}); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPatch || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if len(got.body) != 1 || got.body["enabled"] != false {
		t.Errorf("PATCH should carry only enabled, sent %+v", got.body)
	}
}

func TestUpdateModuleAutodiscoveryRuleCanClearLists(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, moduleRuleJSON)
	var none []string
	if _, err := c.UpdateModuleAutodiscoveryRule(t.Context(), "modrule-1", ModuleAutodiscoveryRuleRequest{IgnorePatterns: &none}); err != nil {
		t.Fatal(err)
	}
	if v, ok := got.body["ignore-patterns"].([]any); !ok || len(v) != 0 {
		t.Errorf("a pointer to a nil slice should send [], sent %#v", got.body["ignore-patterns"])
	}
}

func TestGetAndDeleteModuleAutodiscoveryRule(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, moduleRuleJSON)
	r, err := c.GetModuleAutodiscoveryRule(t.Context(), "modrule-1")
	if err != nil || r.Name != "mg" {
		t.Fatalf("get: %+v %v", r, err)
	}
	if got.method != http.MethodGet || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1" {
		t.Errorf("requested %s %s", got.method, got.path)
	}

	c, got = moduleRuleServer(t, http.StatusNoContent, "")
	if err := c.DeleteModuleAutodiscoveryRule(t.Context(), "modrule-1"); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodDelete {
		t.Errorf("requested %s", got.method)
	}
}

func TestModuleAutodiscoveryRuleNotFound(t *testing.T) {
	c, _ := moduleRuleServer(t, http.StatusNotFound,
		`{"errors":[{"status":"404","detail":"module autodiscovery rule not found"}],"detail":"module autodiscovery rule not found"}`)
	_, err := c.GetModuleAutodiscoveryRule(t.Context(), "modrule-missing")
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestListModuleAutodiscoveryRules(t *testing.T) {
	list := `{"data":[` + moduleRuleJSON[len(`{"data":`):len(moduleRuleJSON)-1] + `],"meta":{"pagination":{"current-page":1,"page-size":100,"total-count":1,"total-pages":1}}}`
	c, got := moduleRuleServer(t, http.StatusOK, list)
	rules, err := c.ListAllModuleAutodiscoveryRules(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 1 || rules[0].ID == "" {
		t.Fatalf("rules: %+v", rules)
	}
	if got.query == "" {
		t.Error("ListAll should page explicitly")
	}

	one, err := c.ListModuleAutodiscoveryRules(t.Context())
	if err != nil || len(one) != 1 {
		t.Fatalf("list: %+v %v", one, err)
	}
}

func TestPreviewModuleAutodiscoveryRule(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, `{"data":{"type":"module-autodiscovery-rule-previews","attributes":{
	  "ref":"main","files-walked":12,"entries":[
	    {"subdirectory":"","name":"mg","provider":"azurerm","registered-as":{"name":"mg","provider":"azurerm"},"collision":false,"missing-provider":false},
	    {"subdirectory":"modules/create","name":"mg-create","provider":"azurerm","registered-as":null,"collision":true,"missing-provider":false}]}}}`)
	p, err := c.PreviewModuleAutodiscoveryRule(t.Context(), "modrule-1")
	if err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodGet || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1/preview" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if p.Ref != "main" || p.FilesWalked != 12 || len(p.Entries) != 2 {
		t.Fatalf("preview: %+v", p)
	}
	if p.Entries[0].RegisteredAs == nil || p.Entries[1].RegisteredAs != nil || !p.Entries[1].Collision {
		t.Errorf("entries: %+v", p.Entries)
	}
}

func TestPreviewUnsavedModuleAutodiscoveryRule(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK,
		`{"data":{"type":"module-autodiscovery-rule-previews","attributes":{"ref":"main","files-walked":0,"entries":[]}}}`)
	p, err := c.PreviewUnsavedModuleAutodiscoveryRule(t.Context(), ModuleAutodiscoveryRuleRequest{
		Name: sp("try"), VCSConnectionID: sp("vcs-1"), RepoURL: sp("https://github.com/org/r"), Pattern: sp("**/*.tf"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPost || got.path != "/api/terrapod/v1/module-autodiscovery-rules/preview" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if p.Entries == nil {
		t.Error("an empty preview should have a non-nil Entries")
	}
}

const scanJSON = `{"data":{"type":"module-autodiscovery-rule-scans","attributes":{
  "ref":"main","files-walked":12,"modules-registered":1,
  "modules":[{"id":"m-1","name":"mg-create","provider":"azurerm","subdirectory":"modules/create"}],
  "skipped":[{"subdirectory":"","reason":"already-registered"}]}}}`

func TestScanModuleAutodiscoveryRuleRegistersAll(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, scanJSON)
	s, err := c.ScanModuleAutodiscoveryRule(t.Context(), "modrule-1", nil)
	if err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPost || got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-1/scan" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if _, ok := got.body["subdirectories"]; ok {
		t.Errorf("nil subdirectories means all; nothing should be sent: %+v", got.body)
	}
	if s.ModulesRegistered != 1 || s.Modules[0].Subdirectory != "modules/create" || s.Skipped[0].Reason != "already-registered" {
		t.Errorf("scan: %+v", s)
	}
}

func TestScanModuleAutodiscoveryRuleSendsTheChosenSubset(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, scanJSON)
	if _, err := c.ScanModuleAutodiscoveryRule(t.Context(), "modrule-1", []string{"modules/create", ""}); err != nil {
		t.Fatal(err)
	}
	sub, ok := got.body["subdirectories"].([]any)
	if !ok || len(sub) != 2 || sub[0] != "modules/create" || sub[1] != "" {
		t.Errorf("sent %#v", got.body["subdirectories"])
	}
}

func TestScanModuleAutodiscoveryRuleRefusal(t *testing.T) {
	c, _ := moduleRuleServer(t, http.StatusUnprocessableEntity,
		`{"errors":[{"status":"422","detail":"not a candidate of this rule: 'nope'"}],"detail":"not a candidate of this rule: 'nope'"}`)
	if _, err := c.ScanModuleAutodiscoveryRule(t.Context(), "modrule-1", []string{"nope"}); err == nil {
		t.Fatal("expected an error for a 422")
	}
}

// ── Org-wide rules (#1620) ───────────────────────────────────────────

const orgRuleJSON = `{"data":{"id":"modrule-33333333-3333-3333-3333-333333333333","type":"module-autodiscovery-rules","attributes":{
  "name":"org","vcs-connection-id":"vcs-2","repo-url":"https://github.com/org/terraform-*","target-kind":"pattern",
  "branch":"","pattern":"**/*.tf","ignore-patterns":[],"enabled":true,"name-template":"{owner}-{repo}","provider":"",
  "vcs-tag-pattern":"v*","labels":{},"owner-email":"","first-scan-at":"2026-09-15T10:00:00Z","last-scanned-sha":"",
  "last-enumerated-at":"2026-09-15T11:00:00Z","last-error":"the org could not be listed",
  "created-at":"2026-09-15T10:00:00Z","updated-at":"2026-09-15T10:00:00Z"}}}`

func TestModuleAutodiscoveryRuleReadsTheOrgFields(t *testing.T) {
	c, _ := moduleRuleServer(t, http.StatusOK, orgRuleJSON)
	r, err := c.GetModuleAutodiscoveryRule(t.Context(), "modrule-3")
	if err != nil {
		t.Fatal(err)
	}
	if r.TargetKind != ModuleAutodiscoveryTargetPattern || r.LastEnumeratedAt != "2026-09-15T11:00:00Z" || r.LastError == "" {
		t.Errorf("org fields: %+v", r)
	}

	// A rule from a server that predates the fields reads them as empty.
	c, _ = moduleRuleServer(t, http.StatusOK, moduleRuleJSON)
	r, err = c.GetModuleAutodiscoveryRule(t.Context(), "modrule-1")
	if err != nil {
		t.Fatal(err)
	}
	if r.TargetKind != "" || r.LastEnumeratedAt != "" || r.LastError != "" {
		t.Errorf("absent org fields should be empty: %+v", r)
	}
}

const orgPreviewJSON = `{"data":{"type":"module-autodiscovery-rule-previews","attributes":{
  "ref":"","files-walked":0,"target-kind":"namespace","listing-complete":false,
  "entries":[
    {"repository":"org/terraform-aws-a","repo-url":"https://github.com/org/terraform-aws-a","subdirectory":"","name":"a","provider":"aws","registered-as":null,"collision":false,"missing-provider":false},
    {"repository":"org/terraform-aws-b","repo-url":"https://github.com/org/terraform-aws-b","subdirectory":"modules/x","name":"b-x","provider":"aws","registered-as":null,"collision":false,"missing-provider":false}],
  "repositories":[
    {"repository":"org/terraform-aws-a","repo-url":"https://github.com/org/terraform-aws-a","ref":"main","status":"active","origin":"baseline","error":""},
    {"repository":"org/terraform-aws-b","repo-url":"https://github.com/org/terraform-aws-b","ref":"main","status":"error","origin":"new","error":"tree listing failed"}]}},
  "meta":{"pagination":{"current-page":2,"page-size":2,"total-count":5,"total-pages":3}}}`

func TestPreviewModuleAutodiscoveryRuleGroupsByRepository(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, orgPreviewJSON)
	p, err := c.PreviewModuleAutodiscoveryRuleWithOptions(t.Context(), "modrule-3",
		ModuleAutodiscoveryPreviewOptions{PageNumber: 2, PageSize: 2})
	if err != nil {
		t.Fatal(err)
	}
	if got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-3/preview" ||
		got.query != "page%5Bnumber%5D=2&page%5Bsize%5D=2" {
		t.Errorf("requested %s ?%s", got.path, got.query)
	}
	if p.TargetKind != ModuleAutodiscoveryTargetNamespace || p.ListingComplete {
		t.Errorf("preview: %+v", p)
	}
	if len(p.Entries) != 2 || p.Entries[1].Repository != "org/terraform-aws-b" || p.Entries[1].RepoURL == "" {
		t.Errorf("entries: %+v", p.Entries)
	}
	if len(p.Repositories) != 2 || p.Repositories[1].Status != "error" || p.Repositories[1].Error == "" || p.Repositories[1].Origin != "new" {
		t.Errorf("repositories: %+v", p.Repositories)
	}
	if p.Pagination == nil || p.Pagination.TotalPages != 3 || p.Pagination.CurrentPage != 2 {
		t.Errorf("pagination: %+v", p.Pagination)
	}
}

func TestPreviewModuleAutodiscoveryRuleReadsOneRepositoryLive(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK,
		`{"data":{"type":"module-autodiscovery-rule-previews","attributes":{"ref":"main","files-walked":3,"entries":[],"target-kind":"namespace","repositories":[],"listing-complete":true}}}`)
	p, err := c.PreviewModuleAutodiscoveryRuleWithOptions(t.Context(), "modrule-3",
		ModuleAutodiscoveryPreviewOptions{Repository: "org/terraform-aws-a"})
	if err != nil {
		t.Fatal(err)
	}
	if got.query != "repository=org%2Fterraform-aws-a" {
		t.Errorf("query %q", got.query)
	}
	if p.Pagination != nil {
		t.Errorf("an unpaged preview should have no pagination: %+v", p.Pagination)
	}
}

func TestPreviewModuleAutodiscoveryRuleFromAnOlderServer(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK,
		`{"data":{"type":"module-autodiscovery-rule-previews","attributes":{"ref":"main","files-walked":1,"entries":[]}}}`)
	p, err := c.PreviewModuleAutodiscoveryRule(t.Context(), "modrule-1")
	if err != nil {
		t.Fatal(err)
	}
	if got.query != "" {
		t.Errorf("a plain preview should send no query, sent %q", got.query)
	}
	if !p.ListingComplete || p.Repositories == nil {
		t.Errorf("an older server's preview is complete, with empty repositories: %+v", p)
	}
}

func TestPreviewModuleAutodiscoveryRuleUnknownRepository(t *testing.T) {
	c, _ := moduleRuleServer(t, http.StatusNotFound,
		`{"errors":[{"status":"404","detail":"'x/y' is not one of this rule's repositories"}],"detail":"'x/y' is not one of this rule's repositories"}`)
	_, err := c.PreviewModuleAutodiscoveryRuleWithOptions(t.Context(), "modrule-3", ModuleAutodiscoveryPreviewOptions{Repository: "x/y"})
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestPreviewUnsavedModuleAutodiscoveryRulePages(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, orgPreviewJSON)
	if _, err := c.PreviewUnsavedModuleAutodiscoveryRuleWithOptions(t.Context(), ModuleAutodiscoveryRuleRequest{
		Name: sp("try"), VCSConnectionID: sp("vcs-1"), RepoURL: sp("https://github.com/org"), Pattern: sp("**/*.tf"),
	}, ModuleAutodiscoveryPreviewOptions{Repository: "ignored", PageNumber: 3}); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPost || got.path != "/api/terrapod/v1/module-autodiscovery-rules/preview" {
		t.Errorf("requested %s %s", got.method, got.path)
	}
	if got.query != "page%5Bnumber%5D=3" {
		t.Errorf("an unsaved preview takes paging only, sent %q", got.query)
	}
}

func TestScanModuleAutodiscoveryRuleSelections(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, `{"data":{"type":"module-autodiscovery-rule-scans","attributes":{
	  "ref":"","files-walked":0,"modules-registered":1,"repositories-scanned":2,
	  "modules":[{"id":"m-1","name":"a","provider":"aws","subdirectory":"","repository":"org/terraform-aws-a","repo-url":"https://github.com/org/terraform-aws-a"}],
	  "skipped":[{"repository":"org/terraform-aws-b","repo-url":"https://github.com/org/terraform-aws-b","subdirectory":"modules/x","reason":"name-taken"}]}}}`)
	s, err := c.ScanModuleAutodiscoveryRuleSelections(t.Context(), "modrule-3", []ModuleAutodiscoverySelection{
		{Repository: "org/terraform-aws-a"},
		{Repository: "org/terraform-aws-b", Subdirectories: []string{"modules/x"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	sel, ok := got.body["selections"].([]any)
	if !ok || len(sel) != 2 {
		t.Fatalf("sent %#v", got.body)
	}
	first, second := sel[0].(map[string]any), sel[1].(map[string]any)
	if first["repository"] != "org/terraform-aws-a" {
		t.Errorf("first selection %#v", first)
	}
	if _, ok := first["subdirectories"]; ok {
		t.Errorf("nil subdirectories means all; nothing should be sent: %#v", first)
	}
	if subs, ok := second["subdirectories"].([]any); !ok || len(subs) != 1 || subs[0] != "modules/x" {
		t.Errorf("second selection %#v", second)
	}
	if s.RepositoriesScanned != 2 || s.Modules[0].Repository != "org/terraform-aws-a" || s.Skipped[0].Repository != "org/terraform-aws-b" {
		t.Errorf("scan: %+v", s)
	}
}

func TestScanModuleAutodiscoveryRuleSelectionsEmptyRegistersAll(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, scanJSON)
	if _, err := c.ScanModuleAutodiscoveryRuleSelections(t.Context(), "modrule-3", nil); err != nil {
		t.Fatal(err)
	}
	if len(got.body) != 0 {
		t.Errorf("no selections means all; sent %+v", got.body)
	}
}

func TestScanModuleAutodiscoveryRuleSelectionsRefusal(t *testing.T) {
	c, _ := moduleRuleServer(t, http.StatusUnprocessableEntity,
		`{"errors":[{"status":"422","detail":"'x/y' is not one of this rule's repositories with candidates to register"}],"detail":"x"}`)
	_, err := c.ScanModuleAutodiscoveryRuleSelections(t.Context(), "modrule-3", []ModuleAutodiscoverySelection{{Repository: "x/y"}})
	var ve *ValidationError
	if !errors.As(err, &ve) {
		t.Fatalf("want ValidationError, got %v", err)
	}
}

func repoItem(path, status string) string {
	return `{"id":"modrepo-` + path + `","type":"module-autodiscovery-rule-repositories","attributes":{
	  "repository":"org/` + path + `","repo-url":"https://github.com/org/` + path + `","vcs-repo-id":"42","default-branch":"main",
	  "origin":"baseline","status":"` + status + `","last-scanned-sha":"abc","seen-subdirectories":[""],
	  "candidates":[{"subdirectory":"","name":"` + path + `","provider":"aws"}],"last-skips":[{"subdirectory":"","reason":"name-taken"}],
	  "previous-paths":[{"path":"old/` + path + `","url":"https://github.com/old/` + path + `"}],
	  "repo-created-at":null,"first-seen-at":"2026-09-15T10:00:00Z","last-checked-at":null,"next-check-at":null,
	  "failure-count":0,"last-error":""},"relationships":{"rule":{"data":{"id":"modrule-3","type":"module-autodiscovery-rules"}}}}`
}

func TestListModuleAutodiscoveryRuleRepositories(t *testing.T) {
	c, got := moduleRuleServer(t, http.StatusOK, `{"data":[`+repoItem("a", "active")+`],
	  "meta":{"pagination":{"current-page":1,"page-size":10,"total-count":1,"total-pages":1}}}`)
	list, err := c.ListModuleAutodiscoveryRuleRepositories(t.Context(), "modrule-3",
		ModuleAutodiscoveryRepositoryListOptions{Status: "active", PageSize: 10})
	if err != nil {
		t.Fatal(err)
	}
	if got.path != "/api/terrapod/v1/module-autodiscovery-rules/modrule-3/repositories" ||
		got.query != "filter%5Bstatus%5D=active&page%5Bsize%5D=10" {
		t.Errorf("requested %s ?%s", got.path, got.query)
	}
	if len(list.Items) != 1 || list.Pagination.TotalCount != 1 {
		t.Fatalf("list: %+v", list)
	}
	r := list.Items[0]
	if r.ID != "modrepo-a" || r.Repository != "org/a" || r.Status != "active" || r.Origin != "baseline" || r.VCSRepoID != "42" {
		t.Errorf("repository: %+v", r)
	}
	if len(r.Candidates) != 1 || r.Candidates[0].Provider != "aws" || r.LastSkips[0].Reason != "name-taken" ||
		r.PreviousPaths[0].Path != "old/a" || r.RepoCreatedAt != "" || r.FirstSeenAt == "" {
		t.Errorf("repository state: %+v", r)
	}
}

func TestListAllModuleAutodiscoveryRuleRepositoriesPages(t *testing.T) {
	var queries []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		queries = append(queries, r.URL.RawQuery)
		page := r.URL.Query().Get("page[number]")
		item := repoItem("p"+page, "error")
		_, _ = w.Write([]byte(`{"data":[` + item + `],"meta":{"pagination":{"current-page":` + page +
			`,"page-size":100,"total-count":2,"total-pages":2}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	all, err := c.ListAllModuleAutodiscoveryRuleRepositories(t.Context(), "modrule-3", "error")
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 2 || all[0].Repository != "org/p1" || all[1].Repository != "org/p2" {
		t.Fatalf("all: %+v", all)
	}
	if len(queries) != 2 || queries[0] != "filter%5Bstatus%5D=error&page%5Bnumber%5D=1&page%5Bsize%5D=100" {
		t.Errorf("queries: %v", queries)
	}
}

func TestListModuleAutodiscoveryRuleRepositoriesNotFound(t *testing.T) {
	c, _ := moduleRuleServer(t, http.StatusNotFound,
		`{"errors":[{"status":"404","detail":"module autodiscovery rule not found"}],"detail":"module autodiscovery rule not found"}`)
	_, err := c.ListAllModuleAutodiscoveryRuleRepositories(t.Context(), "modrule-missing", "")
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}
