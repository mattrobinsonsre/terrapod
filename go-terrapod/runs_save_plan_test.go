package terrapod

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// A saved-plan run (`terraform plan -out=FILE`, #1903) is apply-capable but its
// apply is deferred. The SDK's whole job here is to carry the flag both ways: a
// caller that sets it must have it sent, and a caller reading a run back must
// see what the server recorded. The bug this closes was the server silently
// ignoring the attribute, so a client that could not tell the difference is the
// same failure one layer up.

func TestCreateRunSendsSavePlan(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"run-1","type":"runs","attributes":{"save-plan":true}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	run, err := c.CreateRun(context.Background(), CreateRunRequest{
		WorkspaceID: "ws-1",
		SavePlan:    true,
	})
	if err != nil {
		t.Fatalf("CreateRun: %v", err)
	}

	attrs := got["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["save-plan"] != true {
		t.Fatalf("save-plan was not sent; the server would create an ordinary run: %v", attrs)
	}
	if !run.SavePlan {
		t.Fatal("the created run came back without SavePlan — a caller cannot tell it got what it asked for")
	}
}

func TestCreateRunOmitsSavePlanWhenNotAsked(t *testing.T) {
	// Omitted rather than sent false: `save-plan` is one of three attributes the
	// server refuses in combination, so sending it unasked-for would turn an
	// ordinary plan-only run into a 422.
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"data":{"id":"run-1","type":"runs","attributes":{}}}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if _, err := c.CreateRun(context.Background(), CreateRunRequest{
		WorkspaceID: "ws-1",
		PlanOnly:    true,
	}); err != nil {
		t.Fatalf("CreateRun: %v", err)
	}

	attrs := got["data"].(map[string]any)["attributes"].(map[string]any)
	if _, present := attrs["save-plan"]; present {
		t.Fatalf("save-plan was sent for a plan-only run; the server refuses that pair: %v", attrs)
	}
}

func TestGetRunDecodesSavePlan(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(
			`{"data":{"id":"run-1","type":"runs","attributes":{"status":"planned","save-plan":true}}}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	run, err := c.GetRun(context.Background(), "run-1")
	if err != nil {
		t.Fatalf("GetRun: %v", err)
	}
	if !run.SavePlan {
		t.Fatal("SavePlan was dropped on read — a held plan file is indistinguishable from an ordinary planned run")
	}
}
