package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newPolicySetFixture(t *testing.T) (*Client, *http.Request) {
	t.Helper()
	var lastReq *http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lastReq = r.Clone(r.Context())
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			_ = r.Body.Close()
			lastReq.Body = io.NopCloser(strings.NewReader(string(b)))
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/policy-sets":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"polset-aaa","type":"policy-sets","attributes":{
			  "name":"sec-baseline","enforcement-level":"mandatory","enabled":true,
			  "global-scope":true,"source":"vcs","policy-count":3,
			  "vcs-connection-id":"vcs-aaa","vcs-repo-url":"https://github.com/org/policies",
			  "vcs-branch":"main","policy-path":"policies",
			  "vcs-last-commit-sha":"abc123","vcs-last-synced-at":"2026-05-28T00:00:00Z"
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/policy-sets":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"polset-aaa","type":"policy-sets","attributes":{"name":"sec-baseline","source":"inline","enabled":true,"enforcement-level":"advisory","policy-count":2}},
			  {"id":"polset-bbb","type":"policy-sets","attributes":{"name":"cost-controls","source":"vcs","enabled":true,"enforcement-level":"mandatory","policy-count":5}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/policy-sets/"):
			_, _ = w.Write([]byte(`{"data":{"id":"polset-aaa","type":"policy-sets","attributes":{
			  "name":"sec-baseline","enforcement-level":"mandatory","enabled":true,
			  "global-scope":false,"source":"vcs","policy-count":3,
			  "vcs-connection-id":"vcs-aaa","vcs-repo-url":"https://github.com/org/policies",
			  "vcs-branch":"main","policy-path":"opa/","vcs-last-error":"branch not found"
			}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"polset-aaa","type":"policy-sets","attributes":{
			  "name":"renamed","enforcement-level":"advisory","enabled":true,"source":"inline","policy-count":0
			}}}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/actions/sync"):
			w.WriteHeader(http.StatusAccepted)
			_, _ = w.Write([]byte(`{"data":{"id":"polset-aaa","type":"policy-sets","attributes":{
			  "name":"sec-baseline","source":"vcs","enabled":true,"enforcement-level":"mandatory","policy-count":3,
			  "vcs-last-commit-sha":"def456","vcs-last-synced-at":"2026-05-28T01:00:00Z"
			}}}`))
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
	return c, lastReq
}

func TestCreatePolicySet_VCS(t *testing.T) {
	c, _ := newPolicySetFixture(t)
	ps, err := c.CreatePolicySet(t.Context(), CreatePolicySetRequest{
		Name:             "sec-baseline",
		EnforcementLevel: "mandatory",
		Enabled:          true,
		GlobalScope:      true,
		Source:           "vcs",
		VCSConnectionID:  "vcs-aaa",
		VCSRepoURL:       "https://github.com/org/policies",
		VCSBranch:        "main",
		PolicyPath:       "policies",
	})
	if err != nil {
		t.Fatal(err)
	}
	if ps.ID != "polset-aaa" {
		t.Errorf("ID = %q", ps.ID)
	}
	if ps.Source != "vcs" {
		t.Errorf("Source = %q", ps.Source)
	}
	if ps.VCSConnectionID != "vcs-aaa" {
		t.Errorf("VCSConnectionID = %q", ps.VCSConnectionID)
	}
	if ps.VCSRepoURL != "https://github.com/org/policies" {
		t.Errorf("VCSRepoURL = %q", ps.VCSRepoURL)
	}
	if ps.PolicyCount != 3 {
		t.Errorf("PolicyCount = %d", ps.PolicyCount)
	}
}

func TestListPolicySets(t *testing.T) {
	c, _ := newPolicySetFixture(t)
	list, err := c.ListPolicySets(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Fatalf("len = %d", len(list))
	}
	if list[1].Source != "vcs" {
		t.Errorf("list[1].Source = %q", list[1].Source)
	}
}

func TestGetPolicySet(t *testing.T) {
	c, _ := newPolicySetFixture(t)
	ps, err := c.GetPolicySet(t.Context(), "polset-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if ps.VCSLastError != "branch not found" {
		t.Errorf("VCSLastError = %q", ps.VCSLastError)
	}
	if ps.PolicyPath != "opa/" {
		t.Errorf("PolicyPath = %q", ps.PolicyPath)
	}
}

func TestUpdatePolicySet(t *testing.T) {
	c, _ := newPolicySetFixture(t)
	name := "renamed"
	ps, err := c.UpdatePolicySet(t.Context(), "polset-aaa", UpdatePolicySetRequest{
		Name: &name,
	})
	if err != nil {
		t.Fatal(err)
	}
	if ps.Name != "renamed" {
		t.Errorf("Name = %q", ps.Name)
	}
}

func TestDeletePolicySet(t *testing.T) {
	c, _ := newPolicySetFixture(t)
	if err := c.DeletePolicySet(t.Context(), "polset-aaa"); err != nil {
		t.Fatal(err)
	}
}

func TestSyncPolicySet(t *testing.T) {
	c, _ := newPolicySetFixture(t)
	ps, err := c.SyncPolicySet(t.Context(), "polset-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if ps.VCSLastCommitSHA != "def456" {
		t.Errorf("VCSLastCommitSHA = %q", ps.VCSLastCommitSHA)
	}
}

// #1457: scoping was settable through Create/Update and never read back, so a
// caller could scope a policy set and then not see the scoping it had applied.
// Policy-set scoping is not merely similar to a role's — it is the same
// matcher, reused deliberately (policy_set_service._labels_match).
func TestPolicySetReadsBackItsScoping(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"type":"policy-sets","id":"ps-1","attributes":{
			"name":"prod-guardrails",
			"allow-labels":{"env":["prod","stg"],"team":"sre"},
			"deny-labels":{"tier":["scratch"]}
		}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	ps, err := c.GetPolicySet(t.Context(), "ps-1")
	if err != nil {
		t.Fatal(err)
	}

	if got := ps.AllowLabels["env"]; len(got) != 2 || got[0] != "prod" || got[1] != "stg" {
		t.Errorf("allow-labels env = %v, want [prod stg]", got)
	}
	// A scalar normalises the same way it does for a role, so callers have one
	// shape to handle rather than two.
	if got := ps.AllowLabels["team"]; len(got) != 1 || got[0] != "sre" {
		t.Errorf("allow-labels team = %v, want [sre]", got)
	}
	if got := ps.DenyLabels["tier"]; len(got) != 1 || got[0] != "scratch" {
		t.Errorf("deny-labels tier = %v, want [scratch]", got)
	}
}

func TestPolicySetWithNoScopingReadsBackEmpty(t *testing.T) {
	// Global sets carry no labels; absent must be nil rather than an empty map
	// that reads as "scoped to nothing".
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"type":"policy-sets","id":"ps-2","attributes":{"name":"all","global-scope":true,"allow-labels":{}}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	ps, err := c.GetPolicySet(t.Context(), "ps-2")
	if err != nil {
		t.Fatal(err)
	}
	if ps.AllowLabels != nil {
		t.Errorf("empty scoping should decode as nil, got %v", ps.AllowLabels)
	}
	if !ps.GlobalScope {
		t.Error("global-scope lost")
	}
}
