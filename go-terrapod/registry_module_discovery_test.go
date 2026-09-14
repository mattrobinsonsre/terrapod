package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func discoveryClient(t *testing.T, status int, body string) (*Client, *map[string]any, *string) {
	t.Helper()
	sent := map[string]any{}
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.Method + " " + r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		var doc struct {
			Data struct {
				Attributes map[string]any `json:"attributes"`
			} `json:"data"`
		}
		_ = json.Unmarshal(raw, &doc)
		sent = doc.Data.Attributes
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &sent, &path
}

func TestDiscoverRegistryModules(t *testing.T) {
	c, sent, path := discoveryClient(t, http.StatusOK, `{"data":{"id":"discovery-abc","type":"registry-module-discoveries","attributes":{
	  "vcs-repo-url":"https://github.com/org/terraform-azurerm-management-groups","vcs-branch":"main",
	  "candidates":[
	    {"subdirectory":"","suggested-name":"management-groups","suggested-provider":"azurerm","registered-as":null},
	    {"subdirectory":"modules/create","suggested-name":"management-groups-create","suggested-provider":"azurerm",
	     "registered-as":{"name":"mg-create","provider":"azurerm"}}]}}}`)

	d, err := c.DiscoverRegistryModules(t.Context(), DiscoverRegistryModulesRequest{
		VCSConnectionID: "vcs-1",
		VCSRepoURL:      "https://github.com/org/terraform-azurerm-management-groups",
	})
	if err != nil {
		t.Fatal(err)
	}
	if *path != "POST /api/terrapod/v1/registry-modules/discover" {
		t.Errorf("requested %s", *path)
	}
	if (*sent)["vcs-connection-id"] != "vcs-1" {
		t.Errorf("sent %+v", *sent)
	}
	if _, ok := (*sent)["vcs-branch"]; ok {
		t.Errorf("an unset branch should not be sent (the server uses the default branch): %+v", *sent)
	}
	if d.VCSBranch != "main" || len(d.Candidates) != 2 {
		t.Fatalf("discovery: %+v", d)
	}
	if d.Candidates[0].Subdirectory != "" || d.Candidates[0].RegisteredAs != nil {
		t.Errorf("root candidate: %+v", d.Candidates[0])
	}
	if ref := d.Candidates[1].RegisteredAs; ref == nil || ref.Name != "mg-create" {
		t.Errorf("registered-as not parsed: %+v", d.Candidates[1])
	}
}

func TestDiscoverRegistryModulesSendsTheBranch(t *testing.T) {
	c, sent, _ := discoveryClient(t, http.StatusOK,
		`{"data":{"id":"d","type":"registry-module-discoveries","attributes":{"vcs-branch":"release","candidates":[]}}}`)
	if _, err := c.DiscoverRegistryModules(t.Context(), DiscoverRegistryModulesRequest{
		VCSConnectionID: "vcs-1", VCSRepoURL: "https://github.com/org/r", VCSBranch: "release",
	}); err != nil {
		t.Fatal(err)
	}
	if (*sent)["vcs-branch"] != "release" {
		t.Errorf("sent %+v", *sent)
	}
}

func TestDiscoverRegistryModulesRefusal(t *testing.T) {
	c, _, _ := discoveryClient(t, http.StatusForbidden,
		`{"errors":[{"status":"403","detail":"admin only"}],"detail":"admin only"}`)
	_, err := c.DiscoverRegistryModules(t.Context(), DiscoverRegistryModulesRequest{VCSConnectionID: "v", VCSRepoURL: "u"})
	if err == nil {
		t.Fatal("expected an error for a 403")
	}
}
