package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newMWLFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/workspace-links"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"mwl-aaa","type":"workspace-links","attributes":{
			  "workspace-id":"ws-app","workspace-name":"app","created-by":"alice@example.com"
			}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/workspace-links"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"mwl-aaa","type":"workspace-links","attributes":{"workspace-id":"ws-app","workspace-name":"app"}},
			  {"id":"mwl-bbb","type":"workspace-links","attributes":{"workspace-id":"ws-api","workspace-name":"api"}}
			]}`))
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

func TestCreateModuleWorkspaceLink(t *testing.T) {
	c := newMWLFixture(t)
	mwl, err := c.CreateModuleWorkspaceLink(t.Context(), CreateModuleWorkspaceLinkRequest{
		ModuleName:     "vpc",
		ModuleProvider: "aws",
		WorkspaceID:    "ws-app",
	})
	if err != nil {
		t.Fatal(err)
	}
	if mwl.ID != "mwl-aaa" || mwl.WorkspaceID != "ws-app" {
		t.Errorf("mwl: %+v", mwl)
	}
}

func TestListModuleWorkspaceLinks(t *testing.T) {
	c := newMWLFixture(t)
	list, err := c.ListModuleWorkspaceLinks(t.Context(), "vpc", "aws")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Errorf("list: %+v", list)
	}
}

func TestGetModuleWorkspaceLink(t *testing.T) {
	c := newMWLFixture(t)
	mwl, err := c.GetModuleWorkspaceLink(t.Context(), "vpc", "aws", "mwl-bbb")
	if err != nil || mwl == nil {
		t.Fatalf("got %v / %v", mwl, err)
	}
	if mwl.WorkspaceID != "ws-api" {
		t.Errorf("mwl: %+v", mwl)
	}
	missing, err := c.GetModuleWorkspaceLink(t.Context(), "vpc", "aws", "nope")
	if err == nil || !IsNotFound(err) {
		t.Errorf("expected NotFoundError for missing link, got %v", err)
	}
	if missing != nil {
		t.Errorf("expected nil for missing link")
	}
}

func TestDeleteModuleWorkspaceLink(t *testing.T) {
	c := newMWLFixture(t)
	if err := c.DeleteModuleWorkspaceLink(t.Context(), "vpc", "aws", "mwl-aaa"); err != nil {
		t.Error(err)
	}
}
