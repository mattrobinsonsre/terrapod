package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// A registry module's subdirectory (#1583): sent on create and update, read
// back from the response, and left out of an update that does not set it.

func subdirModuleClient(t *testing.T) (*Client, *map[string]any) {
	t.Helper()
	sent := map[string]any{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var doc struct {
			Data struct {
				Attributes map[string]any `json:"attributes"`
			} `json:"data"`
		}
		_ = json.Unmarshal(body, &doc)
		sent = doc.Data.Attributes
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusCreated)
		}
		_, _ = w.Write([]byte(`{"data":{"id":"mod-sub","type":"registry-modules","attributes":{
		  "name":"create","provider":"azurerm","namespace":"default",
		  "vcs-repo-url":"https://github.com/org/management-groups","subdirectory":"modules/create"}}}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &sent
}

func TestCreateRegistryModuleWithASubdirectory(t *testing.T) {
	c, sent := subdirModuleClient(t)

	m, err := c.CreateRegistryModule(t.Context(), CreateRegistryModuleRequest{
		Name:         "create",
		ProviderName: "azurerm",
		VCSRepoURL:   "https://github.com/org/management-groups",
		Subdirectory: "modules/create",
	})
	if err != nil {
		t.Fatal(err)
	}
	if (*sent)["subdirectory"] != "modules/create" {
		t.Errorf("create sent %+v", *sent)
	}
	if m.Subdirectory != "modules/create" {
		t.Errorf("parsed subdirectory = %q", m.Subdirectory)
	}
}

func TestCreateRegistryModuleAtTheRootSendsNoSubdirectory(t *testing.T) {
	c, sent := subdirModuleClient(t)

	if _, err := c.CreateRegistryModule(t.Context(), CreateRegistryModuleRequest{Name: "vpc", ProviderName: "aws"}); err != nil {
		t.Fatal(err)
	}
	if _, ok := (*sent)["subdirectory"]; ok {
		t.Errorf("a root module's create should not carry a subdirectory: %+v", *sent)
	}
}

func TestUpdateRegistryModuleSubdirectory(t *testing.T) {
	c, sent := subdirModuleClient(t)

	sub := "modules/create"
	if _, err := c.UpdateRegistryModule(t.Context(), "create", "azurerm", UpdateRegistryModuleRequest{Subdirectory: &sub}); err != nil {
		t.Fatal(err)
	}
	if (*sent)["subdirectory"] != "modules/create" {
		t.Errorf("update sent %+v", *sent)
	}

	// Leaving it nil leaves it alone: the field must be absent, not "".
	branch := "main"
	if _, err := c.UpdateRegistryModule(t.Context(), "create", "azurerm", UpdateRegistryModuleRequest{VCSBranch: &branch}); err != nil {
		t.Fatal(err)
	}
	if _, ok := (*sent)["subdirectory"]; ok {
		t.Errorf("an update that doesn't set the subdirectory must not send one: %+v", *sent)
	}
}
