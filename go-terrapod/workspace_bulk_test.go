package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestSearchWorkspaces(t *testing.T) {
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/workspaces/actions/search" || r.Method != http.MethodPost {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &gotBody)
		_, _ = w.Write([]byte(`{"matched":1,"workspaces":[
		  {"id":"ws-1","name":"prod","execution-mode":"agent","execution-backend":"tofu",
		   "terraform-version":"1.9.0","agent-pool-id":"apool-9","labels":{"env":"prod"}}
		]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	res, err := c.SearchWorkspaces(t.Context(), WorkspaceFilter{Labels: map[string]string{"env": "prod"}})
	if err != nil {
		t.Fatal(err)
	}
	if res.Matched != 1 || len(res.Workspaces) != 1 {
		t.Fatalf("unexpected result: %+v", res)
	}
	if res.Workspaces[0].Name != "prod" || res.Workspaces[0].AgentPoolID == nil {
		t.Errorf("unexpected workspace: %+v", res.Workspaces[0])
	}
	// Filter was sent under the snake_case "labels" dimension.
	filter, _ := gotBody["filter"].(map[string]any)
	if filter == nil || filter["labels"] == nil {
		t.Errorf("filter not forwarded: %+v", gotBody)
	}
}

func TestBulkUpdateWorkspaces_DryRun(t *testing.T) {
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/workspaces/actions/bulk-update" {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &gotBody)
		_, _ = w.Write([]byte(`{"dry_run":true,"matched":2,"would_change":[
		  {"id":"ws-1","name":"a","diff":{"terraform_version":{"from":"1.8.0","to":"1.9.0"}}}
		],"unchanged":[{"id":"ws-2","name":"b"}]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	res, err := c.BulkUpdateWorkspaces(
		t.Context(),
		WorkspaceFilter{All: true},
		map[string]any{"terraform-version": "1.9.0"},
		true,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !res.DryRun || res.Matched != 2 || len(res.WouldChange) != 1 {
		t.Fatalf("unexpected result: %+v", res)
	}
	if gotBody["dry_run"] != true {
		t.Errorf("dry_run not forwarded: %+v", gotBody)
	}
	if update, _ := gotBody["update"].(map[string]any); update["terraform-version"] != "1.9.0" {
		t.Errorf("update not forwarded: %+v", gotBody)
	}
}

func TestSearchWorkspaces_Exclude(t *testing.T) {
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &gotBody)
		_, _ = w.Write([]byte(`{"matched":3,"workspaces":[],"excluded":2,"exclude_empty":[1]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	res, err := c.SearchWorkspaces(t.Context(), WorkspaceFilter{
		NamePrefix: "prod-",
		Exclude: []WorkspaceFilter{
			{Labels: map[string]string{"tier": "gold"}},
			{NameGlob: "*-legacy"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Excluded != 2 || len(res.ExcludeEmpty) != 1 || res.ExcludeEmpty[0] != 1 {
		t.Errorf("exclude counts not parsed: %+v", res)
	}
	filter, _ := gotBody["filter"].(map[string]any)
	exclude, _ := filter["exclude"].([]any)
	if len(exclude) != 2 {
		t.Fatalf("exclude not forwarded: %+v", gotBody)
	}
	first, _ := exclude[0].(map[string]any)
	second, _ := exclude[1].(map[string]any)
	if first["labels"] == nil || second["name_glob"] != "*-legacy" {
		t.Errorf("exclude entries lost their dimensions: %+v", exclude)
	}
}

func TestWorkspaceFilter_EmptyExcludeOmitted(t *testing.T) {
	b, err := json.Marshal(WorkspaceFilter{NamePrefix: "prod-", Exclude: []WorkspaceFilter{}})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	_ = json.Unmarshal(b, &got)
	if _, ok := got["exclude"]; ok {
		t.Errorf("empty exclude should be omitted: %s", b)
	}
}

func TestWorkspaceFilter_AutodiscoveryRuleID(t *testing.T) {
	const id = "0190a5e0-7c1e-7000-8000-000000000001"
	b, err := json.Marshal(WorkspaceFilter{
		AutodiscoveryRuleID: id,
		Exclude:             []WorkspaceFilter{{AutodiscoveryRuleID: id}},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	_ = json.Unmarshal(b, &got)
	exclude, _ := got["exclude"].([]any)
	entry, _ := exclude[0].(map[string]any)
	if got["autodiscovery_rule_id"] != id || entry["autodiscovery_rule_id"] != id {
		t.Errorf("autodiscovery_rule_id not forwarded: %s", b)
	}

	b, err = json.Marshal(WorkspaceFilter{NamePrefix: "prod-"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "autodiscovery_rule_id") {
		t.Errorf("unset autodiscovery_rule_id should be omitted: %s", b)
	}

	var decoded WorkspaceFilter
	if err := json.Unmarshal([]byte(`{"autodiscovery_rule_id":"`+id+`"}`), &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.AutodiscoveryRuleID != id {
		t.Errorf("autodiscovery_rule_id not decoded: %+v", decoded)
	}
}

func TestBulkUpdateWorkspaces_DryRunExclude(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"dry_run":true,"matched":4,"excluded":3,"exclude_empty":[0,2]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	res, err := c.BulkUpdateWorkspaces(
		t.Context(),
		WorkspaceFilter{All: true, Exclude: []WorkspaceFilter{{NamePrefix: "a"}}},
		map[string]any{"execution-mode": "agent"},
		true,
	)
	if err != nil {
		t.Fatal(err)
	}
	if res.Excluded != 3 || len(res.ExcludeEmpty) != 2 || res.ExcludeEmpty[1] != 2 {
		t.Errorf("exclude counts not parsed: %+v", res)
	}
}

func TestBulkUpdateWorkspaces_ApplyExclude(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"dry_run":false,"matched":4,"applied":4,"changes":[],"unchanged":[],"errors":[],"excluded":3,"exclude_empty":[]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	res, err := c.BulkUpdateWorkspaces(
		t.Context(),
		WorkspaceFilter{All: true, Exclude: []WorkspaceFilter{{NamePrefix: "a"}}},
		map[string]any{"execution-mode": "agent"},
		false,
	)
	if err != nil {
		t.Fatal(err)
	}
	if res.DryRun || res.Applied != 4 || res.Excluded != 3 || len(res.ExcludeEmpty) != 0 {
		t.Errorf("apply exclude counts not parsed: %+v", res)
	}
}
