package terrapod

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const vaultStatusBody = `{"data":[{"id":"default","type":"vault-instance-statuses","attributes":{
  "name":"default","default":true,"address":"https://vault:8200","namespace":"",
  "auth-method":"kubernetes","auth-mount":"kubernetes","auth-role":"terrapod",
  "tls-trust":"instance-ca","reachable":true,"initialized":true,"sealed":false,
  "standby":false,"version":"1.18.0","health-error":null,"login-ok":true,
  "login-error":null,"ttl-seconds":1740,"checked-at":"2026-09-15T10:00:00Z",
  "last-error":{"class":"VaultDenied","message":"variable 'A': denied","at":"2026-09-15T09:00:00Z"}}},
 {"id":"dr","type":"vault-instance-statuses","attributes":{
  "name":"dr","default":false,"address":"https://dr:8200","namespace":"admin",
  "auth-method":"jwt","auth-mount":"jwt","auth-role":"terrapod","tls-trust":"default",
  "reachable":null,"initialized":null,"sealed":null,"standby":null,"version":"",
  "health-error":null,"login-ok":null,"login-error":null,"ttl-seconds":null,
  "checked-at":null,"last-error":null}}],
 "meta":{"pagination":{"current-page":1,"page-size":2,"total-count":2,"total-pages":1},
  "vault":{"enabled":true,"sampled-at":"2026-09-15T10:00:00Z","unavailable-reason":null}}}`

func vaultFixture(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c
}

func TestGetVaultStatus(t *testing.T) {
	c := vaultFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/terrapod/v1/admin/vault" || r.Method != http.MethodGet {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(vaultStatusBody))
	})

	got, err := c.GetVaultStatus(context.Background())
	if err != nil {
		t.Fatalf("GetVaultStatus: %v", err)
	}
	if !got.Enabled || got.SampledAt != "2026-09-15T10:00:00Z" || got.UnavailableReason != "" {
		t.Fatalf("meta = %+v", got)
	}
	if len(got.Instances) != 2 {
		t.Fatalf("instances = %d", len(got.Instances))
	}
	a := got.Instances[0]
	if a.Name != "default" || !a.Default || a.TLSTrust != "instance-ca" || a.Version != "1.18.0" {
		t.Fatalf("instance = %+v", a)
	}
	if a.Reachable == nil || !*a.Reachable || a.Sealed == nil || *a.Sealed {
		t.Fatalf("health = reachable %v sealed %v", a.Reachable, a.Sealed)
	}
	if a.LoginOK == nil || !*a.LoginOK || a.TTLSeconds == nil || *a.TTLSeconds != 1740 {
		t.Fatalf("login = %v ttl %v", a.LoginOK, a.TTLSeconds)
	}
	if a.LastError == nil || a.LastError.Class != "VaultDenied" {
		t.Fatalf("last-error = %+v", a.LastError)
	}
}

// An instance that has not been sampled must read as unknown (nil), never as
// false: "not checked yet" is not "unreachable".
func TestGetVaultStatusUnsampledIsUnknownNotFalse(t *testing.T) {
	c := vaultFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(vaultStatusBody))
	})
	got, err := c.GetVaultStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	dr := got.Instances[1]
	if dr.Reachable != nil || dr.LoginOK != nil || dr.TTLSeconds != nil || dr.LastError != nil {
		t.Fatalf("unsampled instance should be all-nil: %+v", dr)
	}
	if dr.Namespace != "admin" || dr.AuthMethod != "jwt" {
		t.Fatalf("config fields = %+v", dr)
	}
}

func TestGetVaultStatusDisabled(t *testing.T) {
	c := vaultFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"data":[],"meta":{"pagination":{"current-page":1,"page-size":0,
		  "total-count":0,"total-pages":1},"vault":{"enabled":false,"sampled-at":null,
		  "unavailable-reason":null}}}`))
	})
	got, err := c.GetVaultStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got.Enabled || len(got.Instances) != 0 {
		t.Fatalf("got %+v", got)
	}
}

func TestGetVaultStatusForbidden(t *testing.T) {
	c := vaultFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"status":"403","detail":"Admin or audit access required"}]}`))
	})
	_, err := c.GetVaultStatus(context.Background())
	if err == nil || !IsAuth(err) {
		t.Fatalf("want an auth error, got %v", err)
	}
}

const checkBody = `{"data":{"id":"vrc-1","type":"vault-reference-checks","attributes":{
  "ok":false,"vault-enabled":true,"parses":true,"parse-error":null,"instance":"default",
  "instance-known":true,"engine":"kv2","read-path":"secret/data/apps/x","path-allowed":true,
  "readable":true,"capabilities":["read"],"required-capabilities":["read"],
  "keys":["token","user"],"fields-present":false,"missing-fields":["password"],"notes":[],
  "checks":[{"name":"parses","status":"pass","detail":""},
            {"name":"fields-present","status":"fail","detail":"not present: password"}]}}}`

func TestCheckWorkspaceVaultReference(t *testing.T) {
	var gotPath string
	var gotBody map[string]any
	c := vaultFixture(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = w.Write([]byte(checkBody))
	})

	got, err := c.CheckWorkspaceVaultReference(context.Background(), "abc", VaultReferenceCheckOptions{
		Reference: map[string]any{"mount": "secret", "path": "apps/x", "field": "password"},
		Key:       "DB_PASSWORD",
	})
	if err != nil {
		t.Fatalf("check: %v", err)
	}
	if gotPath != "/api/terrapod/v1/workspaces/ws-abc/vault-reference-checks" {
		t.Fatalf("path = %s", gotPath)
	}
	attrs := gotBody["data"].(map[string]any)["attributes"].(map[string]any)
	if attrs["key"] != "DB_PASSWORD" || attrs["reference"].(map[string]any)["field"] != "password" {
		t.Fatalf("body attrs = %v", attrs)
	}
	if got.ID != "vrc-1" || got.OK || !got.Parses || got.Engine != "kv2" {
		t.Fatalf("result = %+v", got)
	}
	if len(got.Keys) != 2 || got.MissingFields[0] != "password" || *got.FieldsPresent {
		t.Fatalf("fields = %+v", got)
	}
	if len(got.Checks) != 2 || got.Checks[1].Status != "fail" {
		t.Fatalf("checks = %+v", got.Checks)
	}
}

func TestCheckVariableSetVaultReferenceByVariable(t *testing.T) {
	var gotPath, gotRaw string
	c := vaultFixture(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		gotRaw = string(raw)
		_, _ = w.Write([]byte(`{"data":{"id":"vrc-2","type":"vault-reference-checks","attributes":{
		  "ok":true,"parses":true,"engine":"dynamic","keys":null,"notes":["dynamic-not-read"],
		  "checks":[]}}}`))
	})
	got, err := c.CheckVariableSetVaultReference(context.Background(), "varset-9",
		VaultReferenceCheckOptions{VariableID: "123"})
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/api/terrapod/v1/varsets/varset-9/vault-reference-checks" {
		t.Fatalf("path = %s", gotPath)
	}
	if !strings.Contains(gotRaw, `"variable-id":"var-123"`) || strings.Contains(gotRaw, `"reference":`) {
		t.Fatalf("body = %s", gotRaw)
	}
	if got.Keys != nil || got.Notes[0] != "dynamic-not-read" {
		t.Fatalf("result = %+v", got)
	}
}

func TestCheckVaultReferenceNeedsExactlyOneTarget(t *testing.T) {
	called := false
	c := vaultFixture(t, func(w http.ResponseWriter, _ *http.Request) { called = true })
	for _, opts := range []VaultReferenceCheckOptions{
		{},
		{Reference: map[string]any{"mount": "m"}, VariableID: "v"},
	} {
		if _, err := c.CheckWorkspaceVaultReference(context.Background(), "ws-1", opts); err == nil {
			t.Fatalf("want an error for %+v", opts)
		}
	}
	if called {
		t.Fatal("an invalid request must not reach the server")
	}
}

func TestCheckVaultReferenceRateLimited(t *testing.T) {
	c := vaultFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "30")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"errors":[{"status":"429","detail":"at most 20 Vault reference checks a minute"}]}`))
	})
	_, err := c.CheckWorkspaceVaultReference(context.Background(), "ws-1",
		VaultReferenceCheckOptions{VariableID: "v"})
	if err == nil || !strings.Contains(err.Error(), "20 Vault reference checks") {
		t.Fatalf("want the server's detail, got %v", err)
	}
}
