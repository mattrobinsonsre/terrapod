package terrapod

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newSecurityScanFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		p := r.URL.Path
		switch {
		// No scan recorded → 200 with null data.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/runs/run-none/security-scan"):
			_, _ = w.Write([]byte(`{"data":null,"meta":{"summary":null}}`))

		// A recorded, blocking scan.
		case r.Method == http.MethodGet && strings.HasSuffix(p, "/security-scan"):
			_, _ = w.Write([]byte(`{"data":{"id":"ss-1","type":"security-scan-results","attributes":{
			  "engine":"checkov","enforcement-level":"enforced","severity-threshold":"high",
			  "outcome":"failed","error":null,"overridden-by":null,"overridden-at":"",
			  "created-at":"2026-07-24T10:00:00Z",
			  "findings":[{"engine":"checkov","rule_id":"CKV_AWS_18","severity":"high",
			    "title":"S3 logging","resource":"aws_s3_bucket.b","file":"/plan.json","line":10,"guideline":"https://x"}],
			  "summary":{"total":1,"blocking":1,"status":"blocked"}
			}},"meta":{"summary":{"status":"blocked","total":1}}}`))

		// Override → returns the now-overridden scan.
		case r.Method == http.MethodPost && strings.HasSuffix(p, "/actions/override-security-scan"):
			_, _ = w.Write([]byte(`{"data":{"id":"ss-1","type":"security-scan-results","attributes":{
			  "engine":"checkov","enforcement-level":"enforced","severity-threshold":"high",
			  "outcome":"failed","overridden-by":"admin@x.io","overridden-at":"2026-07-24T11:00:00Z",
			  "findings":[],"summary":{"total":1}
			}},"meta":{"overridden":1,"run-status":"applied"}}`))

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

func TestGetRunSecurityScan_Recorded(t *testing.T) {
	c := newSecurityScanFixture(t)
	s, err := c.GetRunSecurityScan(t.Context(), "run-1")
	if err != nil {
		t.Fatalf("GetRunSecurityScan: %v", err)
	}
	if s == nil {
		t.Fatal("expected a scan, got nil")
	}
	if s.Engine != "checkov" || s.EnforcementLevel != "enforced" || s.Outcome != "failed" {
		t.Errorf("scan fields: %+v", s)
	}
	if len(s.Findings) != 1 || s.Findings[0].RuleID != "CKV_AWS_18" || s.Findings[0].Line != 10 {
		t.Errorf("findings not decoded: %+v", s.Findings)
	}
	if s.Summary["blocking"].(float64) != 1 {
		t.Errorf("summary not decoded: %+v", s.Summary)
	}
}

func TestGetRunSecurityScan_NoScan(t *testing.T) {
	c := newSecurityScanFixture(t)
	s, err := c.GetRunSecurityScan(t.Context(), "run-none")
	if err != nil {
		t.Fatalf("GetRunSecurityScan: %v", err)
	}
	if s != nil {
		t.Errorf("expected nil scan for null data, got %+v", s)
	}
}

func TestOverrideRunSecurityScan(t *testing.T) {
	c := newSecurityScanFixture(t)
	s, err := c.OverrideRunSecurityScan(t.Context(), "run-1")
	if err != nil {
		t.Fatalf("OverrideRunSecurityScan: %v", err)
	}
	if s == nil || s.OverriddenBy != "admin@x.io" {
		t.Errorf("override not reflected: %+v", s)
	}
}

func TestGetRunSecurityScan_EmptyRunID(t *testing.T) {
	c := newSecurityScanFixture(t)
	if _, err := c.GetRunSecurityScan(t.Context(), ""); err == nil {
		t.Error("expected error for empty run id")
	}
}
