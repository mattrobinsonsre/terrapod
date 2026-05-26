package tfe

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeTFE spins up a tiny httptest server that returns canonical TFE
// responses for the endpoints the Client probes. Used in lieu of a
// real HCP account — the migration tool's contract with TFE is just
// "go-tfe talks to a JSON:API server", and a hand-rolled server
// gives full control over status codes + payloads.
//
// Each handler is keyed by URL pattern; missing patterns 404 so a
// test exercises only what it sets up.
type fakeTFE struct {
	t       *testing.T
	server  *httptest.Server
	mux     *http.ServeMux
	calls   []string // ordered record of every path the test caused
}

func newFakeTFE(t *testing.T) *fakeTFE {
	t.Helper()
	f := &fakeTFE{t: t, mux: http.NewServeMux()}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls = append(f.calls, r.Method+" "+r.URL.Path)
		// Every TFE response sets the JSON:API content type. We do
		// this in the wrapper so individual handlers focus on bodies.
		w.Header().Set("Content-Type", "application/vnd.api+json")
		f.mux.ServeHTTP(w, r)
	}))
	t.Cleanup(f.server.Close)
	return f
}

// orgRead registers a stub for `GET /organizations/{name}` returning
// either a 200 with a minimal shape or the chosen status.
func (f *fakeTFE) orgRead(name string, status int, body string) {
	f.mux.HandleFunc("/api/v2/organizations/"+name, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(status)
		if body != "" {
			_, _ = w.Write([]byte(body))
		}
	})
}

// orgMembershipsList stubs the token-tier probe. Owner tokens get
// a 200; worker tokens get a 403 (the probe falls back to TokenTierWorker
// on any non-200).
func (f *fakeTFE) orgMembershipsList(orgName string, status int) {
	f.mux.HandleFunc("/api/v2/organizations/"+orgName+"/organization-memberships", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		if status == http.StatusOK {
			_, _ = w.Write([]byte(`{"data":[],"meta":{"pagination":{"current-page":1,"total-pages":1,"total-count":0}}}`))
		}
	})
}

const minimalOrgBody = `{
  "data": {
    "id": "acme",
    "type": "organizations",
    "attributes": {"name": "acme", "external-id": "org-aaa"}
  }
}`

func TestNewClient_Happy_OwnerTier(t *testing.T) {
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)

	c, err := NewClient(t.Context(), Config{
		Address: f.server.URL,
		Token:   "t",
		OrgName: "acme",
	})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if c.OrgName != "acme" {
		t.Errorf("OrgName: %q", c.OrgName)
	}
	if c.TokenTier != TokenTierOwner {
		t.Errorf("TokenTier = %q, want %q", c.TokenTier, TokenTierOwner)
	}
	if c.Address != f.server.URL {
		t.Errorf("Address = %q, want %q", c.Address, f.server.URL)
	}
}

func TestNewClient_WorkerTier(t *testing.T) {
	// 403 on org-memberships → token is plain-member; sensitive
	// variables will return value="" from the API.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusForbidden)

	c, err := NewClient(t.Context(), Config{
		Address: f.server.URL,
		Token:   "t",
		OrgName: "acme",
	})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if c.TokenTier != TokenTierWorker {
		t.Errorf("TokenTier = %q, want %q", c.TokenTier, TokenTierWorker)
	}
}

func TestNewClient_OrgNotFound(t *testing.T) {
	f := newFakeTFE(t)
	f.orgRead("missing", http.StatusNotFound, `{"errors":[{"status":"404","title":"not found"}]}`)

	_, err := NewClient(t.Context(), Config{
		Address: f.server.URL,
		Token:   "t",
		OrgName: "missing",
	})
	if !errors.Is(err, ErrOrgNotFound) {
		t.Errorf("expected ErrOrgNotFound, got: %v", err)
	}
	if !strings.Contains(err.Error(), "missing") {
		t.Errorf("error should name the missing org, got: %v", err)
	}
}

func TestNewClient_MissingToken(t *testing.T) {
	_, err := NewClient(context.Background(), Config{
		Address: "https://app.terraform.io",
		OrgName: "acme",
	})
	if !errors.Is(err, ErrMissingToken) {
		t.Errorf("expected ErrMissingToken, got: %v", err)
	}
}

func TestNewClient_MissingOrg(t *testing.T) {
	_, err := NewClient(context.Background(), Config{
		Address: "https://app.terraform.io",
		Token:   "t",
	})
	if !errors.Is(err, ErrMissingOrg) {
		t.Errorf("expected ErrMissingOrg, got: %v", err)
	}
}

// The default-HCP fallback path (empty Config.Address → DefaultTFEAddress)
// is verified by TestNormaliseAddress's "" case below — exercising it
// end-to-end through NewClient would require a fake HCP, which adds
// nothing the unit test doesn't already cover.

func TestNormaliseAddress(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", DefaultTFEAddress},
		{"app.terraform.io", "https://app.terraform.io"},
		{"https://app.terraform.io", "https://app.terraform.io"},
		{"https://app.terraform.io/", "https://app.terraform.io"},
		{"https://app.terraform.io//", "https://app.terraform.io"},
		{"https://tfe.example.com/api/v2/", "https://tfe.example.com"},
		{"http://tfe-dev.example.com", "http://tfe-dev.example.com"},
		{"  https://app.terraform.io  ", "https://app.terraform.io"},
	}
	for _, c := range cases {
		t.Run(fmt.Sprintf("%q->%q", c.in, c.want), func(t *testing.T) {
			if got := normaliseAddress(c.in); got != c.want {
				t.Errorf("normaliseAddress(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}
}
