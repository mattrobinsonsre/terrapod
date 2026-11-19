package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newAutodiscFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/autodiscovery-rules":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{
			  "name":"monorepo","pattern":"services/*","branch":"main","enabled":true,
			  "created-at":"2025-01-01T00:00:00Z"
			}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v1/autodiscovery-rules/"):
			_, _ = w.Write([]byte(`{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{"name":"monorepo","pattern":"services/*","enabled":true}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{"name":"monorepo","pattern":"services/*","enabled":false}}}`))
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestCreateAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	r, err := c.CreateAutodiscoveryRule(t.Context(), map[string]any{
		"name":     "monorepo",
		"pattern":  "services/*",
		"branch":   "main",
		"repo-url": "https://github.com/org/repo",
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.ID != "ad-aaa" {
		t.Errorf("rule: %+v", r)
	}
	if r.Attributes["pattern"] != "services/*" {
		t.Errorf("attrs: %+v", r.Attributes)
	}
	if r.CreatedAt == "" {
		t.Errorf("created-at not parsed: %+v", r)
	}
}

func TestGetAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	r, err := c.GetAutodiscoveryRule(t.Context(), "ad-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if r.Attributes["enabled"] != true {
		t.Errorf("attrs: %+v", r.Attributes)
	}
}

func TestUpdateAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	r, err := c.UpdateAutodiscoveryRule(t.Context(), "ad-aaa", map[string]any{
		"enabled": false,
	})
	if err != nil {
		t.Fatal(err)
	}
	if r.Attributes["enabled"] != false {
		t.Errorf("attrs: %+v", r.Attributes)
	}
}

func TestDeleteAutodiscoveryRule(t *testing.T) {
	c := newAutodiscFixture(t)
	if err := c.DeleteAutodiscoveryRule(t.Context(), "ad-aaa"); err != nil {
		t.Error(err)
	}
}

const propagationResponse = `{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{"name":"monorepo","engine-version":"1.9"}},
"meta":{"propagation":{"dry_run":true,"matched":3,
"would_change":[{"id":"ws-1","name":"a","diff":{"engine_version":{"from":"1.8","to":"1.9"}}}],
"customised":[{"id":"ws-2","name":"b","fields":{"engine-version":{"current":"1.5","previous":"1.8","new":"1.9"}}}],
"unchanged":[{"id":"ws-3","name":"c"}],"skipped_lifecycle":2}}}`

func newPropagationFixture(t *testing.T, response string) (*Client, *map[string]json.RawMessage) {
	t.Helper()
	var posted map[string]json.RawMessage
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &posted)
		if r.Method != http.MethodPatch || r.URL.Path != "/api/v1/autodiscovery-rules/ad-aaa" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(response))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &posted
}

func TestUpdateAutodiscoveryRuleWithPropagation(t *testing.T) {
	c, posted := newPropagationFixture(t, propagationResponse)
	rule, p, err := c.UpdateAutodiscoveryRuleWithPropagation(t.Context(), "ad-aaa",
		map[string]any{"engine-version": "1.9"}, PropagateOptions{DryRun: true})
	if err != nil {
		t.Fatal(err)
	}
	if string((*posted)["propagate"]) != "true" || string((*posted)["dry_run"]) != "true" {
		t.Errorf("options not sent: %v", *posted)
	}
	var data struct {
		ID         string         `json:"id"`
		Type       string         `json:"type"`
		Attributes map[string]any `json:"attributes"`
	}
	if err := json.Unmarshal((*posted)["data"], &data); err != nil {
		t.Fatal(err)
	}
	if data.ID != "ad-aaa" || data.Type != "autodiscovery-rules" || data.Attributes["engine-version"] != "1.9" {
		t.Errorf("data: %+v", data)
	}
	if rule.ID != "ad-aaa" || rule.Attributes["engine-version"] != "1.9" {
		t.Errorf("rule: %+v", rule)
	}
	if !p.DryRun || p.Matched != 3 || p.SkippedLifecycle != 2 {
		t.Errorf("propagation: %+v", p)
	}
	if len(p.WouldChange) != 1 || p.WouldChange[0].Name != "a" || p.WouldChange[0].Diff["engine_version"] == nil {
		t.Errorf("would_change: %+v", p.WouldChange)
	}
	if len(p.Customised) != 1 || p.Customised[0].ID != "ws-2" {
		t.Fatalf("customised: %+v", p.Customised)
	}
	f := p.Customised[0].Fields["engine-version"]
	if f.Current != "1.5" || f.Previous != "1.8" || f.New != "1.9" {
		t.Errorf("customised field: %+v", f)
	}
	if len(p.Unchanged) != 1 || p.Unchanged[0] != (WorkspaceRef{ID: "ws-3", Name: "c"}) {
		t.Errorf("unchanged: %+v", p.Unchanged)
	}
}

func TestUpdateAutodiscoveryRuleWithPropagationSendsNoDryRunByDefault(t *testing.T) {
	c, posted := newPropagationFixture(t, propagationResponse)
	if _, _, err := c.UpdateAutodiscoveryRuleWithPropagation(t.Context(), "ad-aaa",
		map[string]any{"engine-version": "1.9"}, PropagateOptions{}); err != nil {
		t.Fatal(err)
	}
	if string((*posted)["propagate"]) != "true" {
		t.Errorf("propagate not sent: %v", *posted)
	}
	if _, ok := (*posted)["dry_run"]; ok {
		t.Errorf("dry_run sent: %v", *posted)
	}
}

func TestUpdateAutodiscoveryRuleWithPropagationNeedsTheServerReport(t *testing.T) {
	c, _ := newPropagationFixture(t, `{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{}}}`)
	rule, p, err := c.UpdateAutodiscoveryRuleWithPropagation(t.Context(), "ad-aaa",
		map[string]any{"engine-version": "1.9"}, PropagateOptions{})
	if err == nil || p != nil || rule == nil {
		t.Fatalf("want rule and an error, got %+v %+v %v", rule, p, err)
	}
}

func TestUpdateAutodiscoveryRuleSendsNoPropagateKey(t *testing.T) {
	c, posted := newPropagationFixture(t, `{"data":{"id":"ad-aaa","type":"autodiscovery-rules","attributes":{}}}`)
	if _, err := c.UpdateAutodiscoveryRule(t.Context(), "ad-aaa", map[string]any{"enabled": false}); err != nil {
		t.Fatal(err)
	}
	if _, ok := (*posted)["propagate"]; ok {
		t.Errorf("propagate sent: %v", *posted)
	}
	if _, ok := (*posted)["dry_run"]; ok {
		t.Errorf("dry_run sent: %v", *posted)
	}
}
