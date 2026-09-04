package terrapod

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The defect these cover (#1457): a role rule may bind several accepted values
// to one key — {"env": ["prod", "stg"]} meaning "env is prod OR stg". The
// server has always stored and enforced that, but AllowLabels/DenyLabels were
// map[string]string here, so such a role failed to DECODE.
//
// The blast radius was the reason it mattered: listing roles decodes them all,
// so a single list-valued role made every role unreadable — taking the
// Terraform provider and the migration tool down with it, not just for that one
// role.
//
// It went unnoticed because every test on every surface used single-valued
// rules. The shape that breaks things was precisely the one nothing exercised,
// which is why these pin both shapes rather than only the new one.

func roleServing(t *testing.T, attrs map[string]any) *Client {
	t.Helper()
	body, err := json.Marshal(map[string]any{
		"data": map[string]any{"type": "roles", "id": "r", "name": "r", "attributes": attrs},
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write(body)
	}))
	t.Cleanup(srv.Close)

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestLabelRuleDecodesSeveralValuesForOneKey(t *testing.T) {
	c := roleServing(t, map[string]any{
		"allow-labels": map[string]any{"env": []string{"prod", "stg"}},
		"deny-labels":  map[string]any{"tier": []string{"scratch"}},
	})

	r, err := c.GetRole(t.Context(), "r")
	if err != nil {
		t.Fatalf("a list-valued rule must decode, not error: %v", err)
	}
	if got := r.AllowLabels["env"]; len(got) != 2 || got[0] != "prod" || got[1] != "stg" {
		t.Errorf("allow-labels env = %v, want [prod stg]", got)
	}
	if got := r.DenyLabels["tier"]; len(got) != 1 || got[0] != "scratch" {
		t.Errorf("deny-labels tier = %v, want [scratch]", got)
	}
}

func TestLabelRuleNormalisesAScalarToAOneElementSlice(t *testing.T) {
	// The server may send either shape for the same field, so a scalar has to
	// arrive as a slice too — otherwise callers would need to handle both.
	c := roleServing(t, map[string]any{
		"allow-labels": map[string]any{"team": "sre"},
	})

	r, err := c.GetRole(t.Context(), "r")
	if err != nil {
		t.Fatal(err)
	}
	if got := r.AllowLabels["team"]; len(got) != 1 || got[0] != "sre" {
		t.Errorf("allow-labels team = %v, want [sre]", got)
	}
}

func TestLabelRuleAcceptsBothShapesInOneRule(t *testing.T) {
	// The mixed case is the realistic one: an operator adds a second value to
	// one key and leaves the others alone.
	c := roleServing(t, map[string]any{
		"allow-labels": map[string]any{
			"env":  []string{"prod", "stg"},
			"team": "sre",
		},
	})

	r, err := c.GetRole(t.Context(), "r")
	if err != nil {
		t.Fatal(err)
	}
	if len(r.AllowLabels["env"]) != 2 {
		t.Errorf("env = %v, want two values", r.AllowLabels["env"])
	}
	if len(r.AllowLabels["team"]) != 1 {
		t.Errorf("team = %v, want one value", r.AllowLabels["team"])
	}
}

func TestLabelRuleRejectsAValueThatIsNeither(t *testing.T) {
	// Fail with the offending key named, rather than silently dropping it —
	// a rule that quietly loses a clause is a permission bug.
	c := roleServing(t, map[string]any{
		"allow-labels": map[string]any{"env": 42},
	})

	if _, err := c.GetRole(t.Context(), "r"); err == nil {
		t.Fatal("a non-string, non-list label value must be an error")
	} else if !strings.Contains(err.Error(), "env") {
		t.Errorf("error should name the offending key, got: %v", err)
	}
}

func TestLabelRuleSurvivesTheCreateRoundTrip(t *testing.T) {
	var sent map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&sent)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"type":"roles","id":"r","name":"r","attributes":{"allow-labels":{"env":["prod","stg"]}}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	r, err := c.CreateRole(t.Context(), CreateRoleRequest{
		Name:        "r",
		AllowLabels: map[string][]string{"env": {"prod", "stg"}},
	})
	if err != nil {
		t.Fatal(err)
	}

	// Sent as a list, not flattened on the way out.
	data := sent["data"].(map[string]any)
	attrs := data["attributes"].(map[string]any)
	env := attrs["allow-labels"].(map[string]any)["env"]
	if got, ok := env.([]any); !ok || len(got) != 2 {
		t.Errorf("allow-labels env sent as %#v, want a two-element list", env)
	}
	if len(r.AllowLabels["env"]) != 2 {
		t.Errorf("decoded back as %v, want two values", r.AllowLabels["env"])
	}
}
