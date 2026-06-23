package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newCatalogFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		p := r.URL.Path
		switch {
		// Provider templates
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/provider-templates":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"pt-1","type":"provider-templates","attributes":{
			  "name":"aws-default","provider-type":"aws","body":"provider \"aws\" {}","parameters":[],
			  "created-at":"2026-01-01T00:00:00Z"}}}`))
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/provider-templates":
			_, _ = w.Write([]byte(`{"data":[{"id":"pt-1","type":"provider-templates","attributes":{"name":"aws-default"}}]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(p, "/api/terrapod/v1/provider-templates/"):
			_, _ = w.Write([]byte(`{"data":{"id":"pt-1","type":"provider-templates","attributes":{"name":"aws-default"}}}`))
		case r.Method == http.MethodPatch && strings.HasPrefix(p, "/api/terrapod/v1/provider-templates/"):
			_, _ = w.Write([]byte(`{"data":{"id":"pt-1","type":"provider-templates","attributes":{"name":"aws-renamed"}}}`))
		case r.Method == http.MethodDelete && strings.HasPrefix(p, "/api/terrapod/v1/provider-templates/"):
			w.WriteHeader(http.StatusNoContent)

		// Catalog items
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/catalog-items":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"ci-1","type":"catalog-items","attributes":{
			  "name":"vpc","module-name":"vpc","enabled":true,"created-at":"2026-01-01T00:00:00Z"}}}`))
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/catalog-items":
			_, _ = w.Write([]byte(`{"data":[{"id":"ci-1","type":"catalog-items","attributes":{"name":"vpc"}}]}`))
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/catalog-items/ci-1/form":
			_, _ = w.Write([]byte(`{"data":{"id":"ci-1","type":"catalog-item-forms","attributes":{
			  "resolved-version":"1.2.0","fields":[{"name":"cidr","type":"string","required":true}]}}}`))
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/catalog-items/ci-1/instances":
			_, _ = w.Write([]byte(`{"data":[{"id":"ws-9","type":"catalog-instances","attributes":{"name":"vpc-dev"}}]}`))
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/catalog-items/ci-1/provision":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"ws-9","type":"catalog-instances","attributes":{"name":"vpc-dev","catalog-item-id":"ci-1"}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(p, "/api/terrapod/v1/catalog-items/"):
			_, _ = w.Write([]byte(`{"data":{"id":"ci-1","type":"catalog-items","attributes":{"name":"vpc"}}}`))
		case r.Method == http.MethodPatch && strings.HasPrefix(p, "/api/terrapod/v1/catalog-items/"):
			_, _ = w.Write([]byte(`{"data":{"id":"ci-1","type":"catalog-items","attributes":{"name":"vpc","enabled":false}}}`))
		case r.Method == http.MethodDelete && strings.HasPrefix(p, "/api/terrapod/v1/catalog-items/"):
			w.WriteHeader(http.StatusNoContent)

		// Catalog instances
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/catalog-instances/ws-9":
			_, _ = w.Write([]byte(`{"data":{"id":"ws-9","type":"catalog-instances","attributes":{"name":"vpc-dev","input-values":{"cidr":"10.0.0.0/16"}}}}`))
		case r.Method == http.MethodPatch && p == "/api/terrapod/v1/catalog-instances/ws-9":
			_, _ = w.Write([]byte(`{"data":{"id":"run-1","type":"runs","attributes":{"status":"queued","is-destroy":false}}}`))
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/catalog-instances/ws-9/destroy":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"run-2","type":"runs","attributes":{"status":"queued","is-destroy":true}}}`))
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/catalog-instances/ws-9/confirm":
			_, _ = w.Write([]byte(`{"data":{"id":"run-1","type":"runs","attributes":{"status":"confirmed","is-destroy":false}}}`))
		case r.Method == http.MethodPost && p == "/api/terrapod/v1/catalog-instances/ws-9/discard":
			_, _ = w.Write([]byte(`{"data":{"id":"run-1","type":"runs","attributes":{"status":"discarded","is-destroy":false}}}`))
		case r.Method == http.MethodDelete && p == "/api/terrapod/v1/catalog-instances/ws-9":
			// The orphan escape hatch MUST carry ?orphan=true — without it the
			// server 409s. Assert the SDK actually sends it.
			if r.URL.RawQuery != "orphan=true" {
				http.Error(w, "missing orphan=true: "+r.URL.RawQuery, http.StatusConflict)
				return
			}
			w.WriteHeader(http.StatusNoContent)

		default:
			http.Error(w, "unhandled: "+r.Method+" "+p, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestProviderTemplateCRUD(t *testing.T) {
	c := newCatalogFixture(t)
	tmpl, err := c.CreateProviderTemplate(t.Context(), map[string]any{
		"name": "aws-default", "provider-type": "aws", "body": `provider "aws" {}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if tmpl.ID != "pt-1" || tmpl.Attributes["name"] != "aws-default" {
		t.Errorf("create: %+v", tmpl)
	}
	if tmpl.CreatedAt == "" {
		t.Errorf("created-at not parsed: %+v", tmpl)
	}

	list, err := c.ListProviderTemplates(t.Context())
	if err != nil || len(list) != 1 {
		t.Fatalf("list: %v %+v", err, list)
	}

	upd, err := c.UpdateProviderTemplate(t.Context(), "pt-1", map[string]any{"name": "aws-renamed"})
	if err != nil || upd.Attributes["name"] != "aws-renamed" {
		t.Fatalf("update: %v %+v", err, upd)
	}
	if err := c.DeleteProviderTemplate(t.Context(), "pt-1"); err != nil {
		t.Error(err)
	}
}

func TestCatalogItemCRUDAndForm(t *testing.T) {
	c := newCatalogFixture(t)
	item, err := c.CreateCatalogItem(t.Context(), map[string]any{
		"name": "vpc", "module-id": "mod-1",
	})
	if err != nil || item.ID != "ci-1" {
		t.Fatalf("create: %v %+v", err, item)
	}
	if list, err := c.ListCatalogItems(t.Context()); err != nil || len(list) != 1 {
		t.Fatalf("list: %v %+v", err, list)
	}
	form, err := c.GetCatalogItemForm(t.Context(), "ci-1")
	if err != nil {
		t.Fatal(err)
	}
	if form["resolved-version"] != "1.2.0" {
		t.Errorf("form: %+v", form)
	}
	fields, ok := form["fields"].([]any)
	if !ok || len(fields) != 1 {
		t.Errorf("fields: %+v", form["fields"])
	}
	if _, err := c.UpdateCatalogItem(t.Context(), "ci-1", map[string]any{"enabled": false}); err != nil {
		t.Error(err)
	}
	if err := c.DeleteCatalogItem(t.Context(), "ci-1"); err != nil {
		t.Error(err)
	}
}

func TestCatalogProvisionAndLifecycle(t *testing.T) {
	c := newCatalogFixture(t)
	inst, err := c.ProvisionCatalogItem(t.Context(), "ci-1", map[string]any{
		"name": "vpc-dev", "agent-pool-id": "ap-1",
		"input-values": map[string]any{"cidr": "10.0.0.0/16"},
		"auto-apply":   true,
	})
	if err != nil || inst.ID != "ws-9" {
		t.Fatalf("provision: %v %+v", err, inst)
	}

	instances, err := c.ListCatalogInstances(t.Context(), "ci-1")
	if err != nil || len(instances) != 1 {
		t.Fatalf("list instances: %v %+v", err, instances)
	}

	got, err := c.GetCatalogInstance(t.Context(), "ws-9")
	if err != nil {
		t.Fatal(err)
	}
	if got.Attributes["name"] != "vpc-dev" {
		t.Errorf("get instance: %+v", got)
	}

	run, err := c.ReconfigureCatalogInstance(t.Context(), "ws-9", map[string]any{
		"input-values": map[string]any{"cidr": "10.1.0.0/16"}, "auto-apply": true,
	})
	if err != nil || run.ID != "run-1" || run.IsDestroy {
		t.Fatalf("reconfigure: %v %+v", err, run)
	}

	drun, err := c.DestroyCatalogInstance(t.Context(), "ws-9", map[string]any{"auto-apply": true})
	if err != nil || drun.ID != "run-2" || !drun.IsDestroy {
		t.Fatalf("destroy: %v %+v", err, drun)
	}

	crun, err := c.ConfirmCatalogInstanceRun(t.Context(), "ws-9")
	if err != nil || crun.Status != "confirmed" {
		t.Fatalf("confirm: %v %+v", err, crun)
	}

	xrun, err := c.DiscardCatalogInstanceRun(t.Context(), "ws-9")
	if err != nil || xrun.Status != "discarded" {
		t.Fatalf("discard: %v %+v", err, xrun)
	}

	if err := c.OrphanCatalogInstance(t.Context(), "ws-9"); err != nil {
		t.Fatalf("orphan: %v", err)
	}
}
