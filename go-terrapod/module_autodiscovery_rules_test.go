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
