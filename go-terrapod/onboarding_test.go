package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// sessionAttrs is a representative detail-read session payload (schema_ready,
// with a discovery surface + a nullable count).
const sessionAttrs = `{
  "workspace-id":"ws-app","status":"schema_ready","provider":"aws",
  "provider-version":"~> 5.0","engine":"tofu","engine-version":"1.9.0",
  "selected-types":[],"ai-assisted":false,"error":null,
  "data-source-count":2,
  "discovery-surface":{"count":2,"data_sources":[
    {"provider":"registry.opentofu.org/hashicorp/aws","name":"aws_vpcs",
     "has_filter":true,"has_tags":false,"returns_list":true,
     "id_list_attr":"ids","resource_type":"aws_vpc",
     "inputs":["filter","tags"],"required_inputs":[]},
    {"provider":"registry.opentofu.org/hashicorp/aws","name":"aws_subnets",
     "has_filter":true,"has_tags":false,"returns_list":true,
     "id_list_attr":"ids","resource_type":"aws_subnet",
     "inputs":["filter"],"required_inputs":[]}
  ]},
  "generated-config":null,"import-blocks":null,
  "polished-config":null,"polished-import-blocks":null,
  "paired-config":null,"paired-polished-config":null,
  "discovery-run-id":null,"result-run-id":null,
  "created-by":"alice@example.com",
  "created-at":"2026-07-17T10:00:00Z","updated-at":"2026-07-17T10:01:00Z"
}`

func newOnboardingFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reqBody []byte
		if r.Body != nil {
			reqBody, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		p := r.URL.Path
		switch {
		case r.Method == http.MethodGet && p == "/api/terrapod/v1/onboarding":
			_, _ = w.Write([]byte(`{"data":{"type":"onboarding-availability","attributes":{
			  "ai-available":true,"ai-model-configured":true}}}`))

		case r.Method == http.MethodPost && strings.HasSuffix(p, "/onboarding-sessions"):
			// Echo back a pending session; assert the client sent provider attrs.
			var doc struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			_ = json.Unmarshal(reqBody, &doc)
			if doc.Data.Attributes["provider"] != "aws" {
				http.Error(w, "missing provider", http.StatusUnprocessableEntity)
				return
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"11111111-1111-1111-1111-111111111111",
			  "type":"onboarding-sessions","attributes":{
			  "workspace-id":"ws-app","status":"pending","provider":"aws",
			  "provider-version":"~> 5.0","ai-assisted":false,
			  "data-source-count":null,"discovery-surface":null,
			  "created-by":"alice@example.com","created-at":"2026-07-17T10:00:00Z",
			  "updated-at":"2026-07-17T10:00:00Z"}}}`))

		case r.Method == http.MethodGet && strings.HasSuffix(p, "/onboarding-sessions"):
			// List — surface omitted per the server contract.
			_, _ = w.Write([]byte(`{"data":[{"id":"11111111-1111-1111-1111-111111111111",
			  "type":"onboarding-sessions","attributes":{
			  "workspace-id":"ws-app","status":"schema_ready","provider":"aws",
			  "ai-assisted":false,"data-source-count":null,"discovery-surface":null,
			  "created-at":"2026-07-17T10:00:00Z","updated-at":"2026-07-17T10:01:00Z"}}]}`))

		case r.Method == http.MethodGet && strings.Contains(p, "/onboarding-sessions/"):
			if strings.HasSuffix(p, "/missing") {
				http.Error(w, `{"errors":[{"status":"404","title":"Not Found","detail":"Onboarding session not found"}]}`, http.StatusNotFound)
				return
			}
			_, _ = w.Write([]byte(`{"data":{"id":"11111111-1111-1111-1111-111111111111",
			  "type":"onboarding-sessions","attributes":` + sessionAttrs + `}}`))

		case r.Method == http.MethodPost && strings.HasSuffix(p, "/discover"):
			var doc struct {
				Data struct {
					Attributes struct {
						SelectedTypes []string `json:"selected-types"`
					} `json:"attributes"`
				} `json:"data"`
			}
			_ = json.Unmarshal(reqBody, &doc)
			if len(doc.Data.Attributes.SelectedTypes) == 0 {
				http.Error(w, `{"errors":[{"status":"422","title":"Unprocessable Entity","detail":"selected-types must be a list of strings"}]}`, http.StatusUnprocessableEntity)
				return
			}
			_, _ = w.Write([]byte(`{"data":{"id":"11111111-1111-1111-1111-111111111111",
			  "type":"onboarding-sessions","attributes":` + sessionAttrs + `}}`))

		default:
			http.Error(w, "unhandled "+r.Method+" "+p, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestGetOnboardingAvailability(t *testing.T) {
	c := newOnboardingFixture(t)
	a, err := c.GetOnboardingAvailability(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if !a.AIAvailable || !a.AIModelConfigured {
		t.Errorf("availability: %+v", a)
	}
}

func TestCreateOnboardingSession(t *testing.T) {
	c := newOnboardingFixture(t)
	s, err := c.CreateOnboardingSession(t.Context(), CreateOnboardingSessionRequest{
		WorkspaceID:     "ws-app",
		Provider:        "aws",
		ProviderVersion: "~> 5.0",
	})
	if err != nil {
		t.Fatal(err)
	}
	if s.ID == "" || s.Status != "pending" || s.Provider != "aws" {
		t.Errorf("session: %+v", s)
	}
	if s.DataSourceCount != nil || s.DiscoverySurface != nil {
		t.Errorf("expected nil count/surface on pending session: %+v", s)
	}
}

func TestCreateOnboardingSessionRequiresWorkspace(t *testing.T) {
	c := newOnboardingFixture(t)
	if _, err := c.CreateOnboardingSession(t.Context(), CreateOnboardingSessionRequest{Provider: "aws"}); err == nil {
		t.Fatal("expected error for missing workspace id")
	}
}

func TestListOnboardingSessions(t *testing.T) {
	c := newOnboardingFixture(t)
	list, err := c.ListOnboardingSessions(t.Context(), "ws-app")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].Status != "schema_ready" {
		t.Errorf("list: %+v", list)
	}
	// Surface is omitted on the list read.
	if list[0].DiscoverySurface != nil {
		t.Errorf("expected nil surface on list entry: %+v", list[0])
	}
}

func TestGetOnboardingSessionParsesSurface(t *testing.T) {
	c := newOnboardingFixture(t)
	s, err := c.GetOnboardingSession(t.Context(), "11111111-1111-1111-1111-111111111111")
	if err != nil {
		t.Fatal(err)
	}
	if s.DataSourceCount == nil || *s.DataSourceCount != 2 {
		t.Fatalf("data-source-count: %+v", s.DataSourceCount)
	}
	if s.DiscoverySurface == nil || s.DiscoverySurface.Count != 2 || len(s.DiscoverySurface.DataSources) != 2 {
		t.Fatalf("surface: %+v", s.DiscoverySurface)
	}
	ds := s.DiscoverySurface.DataSources[0]
	if ds.Name != "aws_vpcs" || !ds.HasFilter || !ds.ReturnsList || ds.IDListAttr != "ids" || ds.ResourceType != "aws_vpc" {
		t.Errorf("data source: %+v", ds)
	}
}

func TestGetOnboardingSessionNotFound(t *testing.T) {
	c := newOnboardingFixture(t)
	_, err := c.GetOnboardingSession(t.Context(), "missing")
	if err == nil {
		t.Fatal("expected error")
	}
	if _, ok := err.(*NotFoundError); !ok {
		t.Errorf("expected *NotFoundError, got %T: %v", err, err)
	}
}

func TestStartOnboardingDiscovery(t *testing.T) {
	c := newOnboardingFixture(t)
	s, err := c.StartOnboardingDiscovery(t.Context(), "11111111-1111-1111-1111-111111111111", []string{"aws_vpcs"})
	if err != nil {
		t.Fatal(err)
	}
	if s.DiscoverySurface == nil {
		t.Errorf("expected surface on discovery response: %+v", s)
	}
}

func TestStartOnboardingDiscoveryEmptyTypesRejected(t *testing.T) {
	c := newOnboardingFixture(t)
	_, err := c.StartOnboardingDiscovery(t.Context(), "11111111-1111-1111-1111-111111111111", nil)
	if err == nil {
		t.Fatal("expected 422 error for empty selected-types")
	}
	if _, ok := err.(*ValidationError); !ok {
		t.Errorf("expected *ValidationError, got %T: %v", err, err)
	}
}
