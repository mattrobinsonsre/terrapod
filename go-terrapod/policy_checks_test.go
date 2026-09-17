package terrapod

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const policyCheckBody = `{"id":"polchk-opa-0000","type":"policy-checks","attributes":{
  "status":"soft_failed","scope":"organization",
  "result":{"result":false,"passed":1,"total-failed":1,"hard-failed":0,"soft-failed":1,"advisory-failed":0,"duration":0},
  "actions":{"is-overridable":true},"permissions":{"can-override":true},
  "status-timestamps":{"queued-at":"2026-09-17T12:30:00Z","soft-failed-at":"2026-09-17T12:30:00Z"}},
  "relationships":{"run":{"data":{"id":"run-0000","type":"runs"}}}}`

func newPolicyCheckFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		switch {
		case r.Method == http.MethodGet && p == "/api/v2/runs/run-0000/policy-checks":
			_, _ = w.Write([]byte(`{"data":[` + policyCheckBody + `],"meta":{"pagination":{"total-count":1}}}`))
		case r.Method == http.MethodGet && p == "/api/v2/policy-checks/polchk-opa-0000":
			_, _ = w.Write([]byte(`{"data":` + policyCheckBody + `}`))
		case r.Method == http.MethodGet && p == "/api/v2/policy-checks/polchk-opa-0000/output":
			w.Header().Set("Content-Type", "text/plain")
			_, _ = w.Write([]byte("Policy set 'baseline' (mandatory): failed\n"))
		case r.Method == http.MethodPost && p == "/api/v2/policy-checks/polchk-opa-0000/actions/override":
			_, _ = w.Write([]byte(`{"data":` + strings.Replace(
				strings.Replace(policyCheckBody, `"soft_failed"`, `"overridden"`, 1),
				`"is-overridable":true`, `"is-overridable":false`, 1) + `}`))
		case r.Method == http.MethodPost && p == "/api/v2/policy-checks/polchk-scan-0000/actions/override":
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"errors":[{"status":"409","detail":"Policy check is passed"}]}`))
		default:
			http.Error(w, `{"errors":[{"status":"404"}]}`, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestListRunPolicyChecks(t *testing.T) {
	c := newPolicyCheckFixture(t)
	checks, err := c.ListRunPolicyChecks(t.Context(), "run-0000")
	if err != nil {
		t.Fatal(err)
	}
	if len(checks) != 1 {
		t.Fatalf("checks: %+v", checks)
	}
	pc := checks[0]
	if pc.Status != PolicyCheckSoftFailed || !pc.IsOverridable || !pc.CanOverride {
		t.Errorf("status/actions: %+v", pc)
	}
	if pc.RunID != "run-0000" || pc.Scope != "organization" || pc.SoftFailed != 1 || pc.Passed != 1 {
		t.Errorf("fields: %+v", pc)
	}
}

func TestGetPolicyCheckAndOutput(t *testing.T) {
	c := newPolicyCheckFixture(t)
	pc, err := c.GetPolicyCheck(t.Context(), "polchk-opa-0000")
	if err != nil || pc.ID != "polchk-opa-0000" {
		t.Fatalf("get: %+v %v", pc, err)
	}
	out, err := c.GetPolicyCheckOutput(t.Context(), "polchk-opa-0000")
	if err != nil || !strings.Contains(out, "(mandatory): failed") {
		t.Fatalf("output: %q %v", out, err)
	}
}

func TestOverridePolicyCheck(t *testing.T) {
	c := newPolicyCheckFixture(t)
	pc, err := c.OverridePolicyCheck(t.Context(), "polchk-opa-0000")
	if err != nil {
		t.Fatal(err)
	}
	if pc.Status != PolicyCheckOverridden || pc.IsOverridable {
		t.Errorf("after override: %+v", pc)
	}
	if _, err := c.OverridePolicyCheck(t.Context(), "polchk-scan-0000"); !IsConflict(err) {
		t.Errorf("overriding a passed check: want conflict, got %v", err)
	}
}

func TestPolicyCheckIDMustBeAPolicyCheck(t *testing.T) {
	c := newPolicyCheckFixture(t)
	if _, err := c.GetPolicyCheck(t.Context(), "run-0000"); err == nil {
		t.Error("expected an error for a non-polchk id")
	}
	if _, err := c.GetPolicyCheck(t.Context(), "polchk-opa-missing"); !IsNotFound(err) {
		t.Errorf("want not found, got %v", err)
	}
}
