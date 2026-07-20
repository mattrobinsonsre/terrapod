package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func newCostEstimateFixture(t *testing.T, status int, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"no cost estimate for this run"}]}`))
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

const costEstimateBody = `{"data":{"id":"cost-estimate-abc","type":"cost-estimates",
  "attributes":{
    "currency":"USD",
    "total":{"min":219.0,"max":219.0},
    "previous":{"min":73.0,"max":73.0},
    "diff":{"min":146.0,"max":146.0},
    "resources":[
      {"address":"aws_instance.eu","type":"aws_instance","name":"eu","change":"add","monthly":{"min":146.0,"max":146.0}},
      {"address":"aws_instance.us","type":"aws_instance","name":"us","change":"noop","monthly":{"min":73.0,"max":73.0}}
    ],
    "unpriced":[
      {"address":"random_pet.name","type":"random_pet","change":"add"}
    ]
  },
  "relationships":{"run":{"data":{"id":"run-abc","type":"runs"}}}}}`

func TestGetRunCostEstimate(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costEstimateBody)
	e, err := c.GetRunCostEstimate(t.Context(), "abc") // bare UUID → "run-abc"
	if err != nil {
		t.Fatal(err)
	}
	if e.RunID != "abc" {
		t.Errorf("RunID = %q, want abc (prefix stripped)", e.RunID)
	}
	if e.Currency != "USD" {
		t.Errorf("Currency = %q, want USD", e.Currency)
	}
	if e.Total.Min != 219.0 || e.Diff.Max != 146.0 || e.Previous.Min != 73.0 {
		t.Errorf("ranges not decoded: total=%+v diff=%+v prev=%+v", e.Total, e.Diff, e.Previous)
	}
	if len(e.Resources) != 2 || len(e.Unpriced) != 1 {
		t.Fatalf("resources=%d unpriced=%d, want 2/1", len(e.Resources), len(e.Unpriced))
	}
	if e.Resources[0].Change != "add" || e.Resources[0].Monthly.Min != 146.0 {
		t.Errorf("resource not decoded: %+v", e.Resources[0])
	}
	if e.Unpriced[0].Address != "random_pet.name" {
		t.Errorf("unpriced not decoded: %+v", e.Unpriced[0])
	}
}

func TestGetRunCostEstimateNotFound(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusNotFound, "")
	_, err := c.GetRunCostEstimate(t.Context(), "run-none")
	if !IsNotFound(err) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestGetRunCostEstimateEmptyID(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, costEstimateBody)
	if _, err := c.GetRunCostEstimate(t.Context(), ""); err == nil {
		t.Fatal("want error for empty run id")
	}
}

const workspaceCostBody = `{"data":{"id":"workspace-cost-ws1","type":"workspace-cost-estimates",
  "attributes":{
    "currency":"USD",
    "total":{"min":292.0,"max":292.0},
    "previous":{"min":292.0,"max":292.0},
    "diff":{"min":0.0,"max":0.0},
    "resources":[
      {"address":"aws_instance.web","type":"aws_instance","name":"web","change":"noop","monthly":{"min":73.0,"max":73.0}},
      {"address":"aws_db_instance.main","type":"aws_db_instance","name":"main","change":"noop","monthly":{"min":219.0,"max":219.0}}
    ],
    "unpriced":[
      {"address":"random_pet.name","type":"random_pet","change":"noop"}
    ],
    "state-version":{"id":"sv-xyz","serial":7,"created-at":"2026-07-20T09:00:00Z"}
  },
  "relationships":{"workspace":{"data":{"id":"ws-ws1","type":"workspaces"}}}}}`

const workspaceCostEmptyBody = `{"data":{"id":"workspace-cost-ws1","type":"workspace-cost-estimates",
  "attributes":{
    "currency":"USD",
    "total":{"min":0.0,"max":0.0},
    "previous":{"min":0.0,"max":0.0},
    "diff":{"min":0.0,"max":0.0},
    "resources":[],
    "unpriced":[],
    "state-version":null
  },
  "relationships":{"workspace":{"data":{"id":"ws-ws1","type":"workspaces"}}}}}`

func TestGetWorkspaceCostEstimate(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, workspaceCostBody)
	e, err := c.GetWorkspaceCostEstimate(t.Context(), "ws1") // bare UUID → "ws-ws1"
	if err != nil {
		t.Fatal(err)
	}
	if e.WorkspaceID != "ws1" {
		t.Errorf("WorkspaceID = %q, want ws1 (prefix stripped)", e.WorkspaceID)
	}
	if e.Total.Max != 292.0 || e.Diff.Max != 0.0 {
		t.Errorf("ranges not decoded: total=%+v diff=%+v", e.Total, e.Diff)
	}
	if len(e.Resources) != 2 || len(e.Unpriced) != 1 {
		t.Fatalf("resources=%d unpriced=%d, want 2/1", len(e.Resources), len(e.Unpriced))
	}
	if e.Resources[0].Change != "noop" {
		t.Errorf("state resources must be noop, got %q", e.Resources[0].Change)
	}
	if e.StateVersion == nil || e.StateVersion.ID != "sv-xyz" || e.StateVersion.Serial != 7 {
		t.Errorf("state-version not decoded: %+v", e.StateVersion)
	}
}

func TestGetWorkspaceCostEstimateNoState(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, workspaceCostEmptyBody)
	e, err := c.GetWorkspaceCostEstimate(t.Context(), "ws-ws1")
	if err != nil {
		t.Fatal(err)
	}
	if e.StateVersion != nil {
		t.Errorf("want nil StateVersion for a workspace with no state, got %+v", e.StateVersion)
	}
	if e.Total.Max != 0.0 || len(e.Resources) != 0 {
		t.Errorf("want zeroed empty estimate, got total=%+v resources=%d", e.Total, len(e.Resources))
	}
}

func TestGetWorkspaceCostEstimateEmptyID(t *testing.T) {
	c := newCostEstimateFixture(t, http.StatusOK, workspaceCostBody)
	if _, err := c.GetWorkspaceCostEstimate(t.Context(), ""); err == nil {
		t.Fatal("want error for empty workspace id")
	}
}
