package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newRunTaskFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/run-tasks"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"rt-aaa","type":"run-tasks","attributes":{
			  "name":"opa-check","url":"https://opa.example/api/v1/data","stage":"post_plan",
			  "enforcement-level":"mandatory","enabled":true,"has-hmac-key":true
			}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/run-tasks/"):
			_, _ = w.Write([]byte(`{"data":{"id":"rt-aaa","type":"run-tasks","attributes":{"name":"opa-check","url":"https://opa.example/api/v1/data","stage":"post_plan","enforcement-level":"mandatory"}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/run-tasks"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"rt-aaa","type":"run-tasks","attributes":{"name":"opa-check","stage":"post_plan","enforcement-level":"mandatory"}}
			]}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"rt-aaa","type":"run-tasks","attributes":{"name":"opa-check","stage":"post_plan","enforcement-level":"advisory"}}}`))
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

func TestCreateRunTask(t *testing.T) {
	c := newRunTaskFixture(t)
	rt, err := c.CreateRunTask(t.Context(), "ws-app", CreateRunTaskRequest{
		Name:             "opa-check",
		URL:              "https://opa.example/api/v1/data",
		Stage:            "post_plan",
		EnforcementLevel: "mandatory",
		HMACKey:          "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	if rt.Stage != "post_plan" || !rt.HasHMACKey {
		t.Errorf("rt: %+v", rt)
	}
}

func TestGetRunTask(t *testing.T) {
	c := newRunTaskFixture(t)
	rt, err := c.GetRunTask(t.Context(), "rt-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if rt.Name != "opa-check" {
		t.Errorf("rt: %+v", rt)
	}
}

func TestListRunTasks(t *testing.T) {
	c := newRunTaskFixture(t)
	list, err := c.ListRunTasks(t.Context(), "ws-app")
	if err != nil || len(list) != 1 {
		t.Errorf("list: %v / %v", list, err)
	}
}

func TestUpdateRunTask(t *testing.T) {
	c := newRunTaskFixture(t)
	rt, err := c.UpdateRunTask(t.Context(), "rt-aaa", UpdateRunTaskRequest{
		EnforcementLevel: "advisory",
	})
	if err != nil {
		t.Fatal(err)
	}
	if rt.EnforcementLevel != "advisory" {
		t.Errorf("rt: %+v", rt)
	}
}

func TestDeleteRunTask(t *testing.T) {
	c := newRunTaskFixture(t)
	if err := c.DeleteRunTask(t.Context(), "rt-aaa"); err != nil {
		t.Error(err)
	}
}
