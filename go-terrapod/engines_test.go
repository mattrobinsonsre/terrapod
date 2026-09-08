package terrapod

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

const engineListBody = `{"data":[{"type":"engines","id":"terraform","attributes":{
  "name":"terraform",
  "phases":["plan","apply"],
  "status-phases":{"planning":"plan","planned":"plan","applying":"apply","applied":"apply"},
  "default-execution-backend":"tofu",
  "vocabulary":"terraform"}}],"meta":{"pagination":{"total-count":1}}}`

func TestListEngines(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/engines" {
			t.Errorf("path = %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(engineListBody))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := c.ListEngines(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("want 1 engine, got %d", len(got))
	}
	e := got[0]
	if e.Name != "terraform" || e.DefaultExecutionBackend != "tofu" || e.Vocabulary != "terraform" {
		t.Errorf("engine = %+v", e)
	}
	if len(e.Phases) != 2 || e.Phases[0] != "plan" || e.Phases[1] != "apply" {
		t.Errorf("phases = %v, want [plan apply] in order — the order is the order a run performs them", e.Phases)
	}
}

// The mapping is the reason this endpoint exists, so it is asserted on its own
// rather than only as part of the struct.
func TestEnginePhaseForMapsInternalStatusOntoTheEnginesWord(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(engineListBody))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	engines, err := c.ListEngines(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	e := engines[0]

	for status, want := range map[string]string{
		"planning": "plan", "planned": "plan",
		"applying": "apply", "applied": "apply",
	} {
		got, ok := e.PhaseFor(status)
		if !ok || got != want {
			t.Errorf("PhaseFor(%q) = %q,%v; want %q,true", status, got, ok, want)
		}
	}

	// A terminal status belongs to no phase. Reporting it as a plan would be
	// wrong, not merely imprecise — which is why the second return exists.
	if phase, ok := e.PhaseFor("errored"); ok {
		t.Errorf("PhaseFor(errored) = %q,true; a terminal status has no phase", phase)
	}
}

func TestGetEngine(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/engines/terraform" {
			t.Errorf("path = %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"type":"engines","id":"terraform","attributes":{
		  "name":"terraform","phases":["plan","apply"],
		  "status-phases":{"planning":"plan"},
		  "default-execution-backend":"tofu","vocabulary":"terraform"}}}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	got, err := c.GetEngine(context.Background(), "terraform")
	if err != nil {
		t.Fatal(err)
	}
	if got.Name != "terraform" {
		t.Errorf("name = %q", got.Name)
	}
}

// An engine this deployment does not serve must surface as not-found, so a
// caller can tell "not here" from "here but broken".
func TestGetEngineUnknownIsNotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"Unknown engine: pulumi","status":"404"}]}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	_, err := c.GetEngine(context.Background(), "pulumi")
	if err == nil {
		t.Fatal("want an error for an engine the deployment does not serve")
	}
	if !IsNotFound(err) {
		t.Errorf("want NotFoundError, got %T: %v", err, err)
	}
}
