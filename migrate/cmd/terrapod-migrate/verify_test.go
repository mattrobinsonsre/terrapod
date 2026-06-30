package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
)

// verifyFakeServer reports a workspace's name, variable count, and
// current state (serial/lineage) so runVerify's parity checks can be
// exercised. A serial of -1 means "no current state version" (404).
func newVerifyServer(t *testing.T, name string, varCount int, serial int64, lineage string) *terrapod.Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/vars"):
			var items []string
			for i := range varCount {
				items = append(items, fmt.Sprintf(`{"id":"var-%d","type":"vars","attributes":{"key":"k%d","category":"terraform"}}`, i, i))
			}
			_, _ = fmt.Fprintf(w, `{"data":[%s]}`, strings.Join(items, ","))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/current-state-version"):
			if serial < 0 {
				http.Error(w, "no state", http.StatusNotFound)
				return
			}
			_, _ = fmt.Fprintf(w, `{"data":{"id":"sv-x","type":"state-versions","attributes":{"serial":%d,"lineage":%q,"state-size":10}}}`, serial, lineage)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v2/workspaces/"):
			_, _ = fmt.Fprintf(w, `{"data":{"id":"ws-a","type":"workspaces","attributes":{"name":%q}}}`, name)
		default:
			http.Error(w, "unhandled "+r.Method+" "+r.URL.Path, http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func baselineState() *framework.State {
	return &framework.State{Workspaces: []framework.WorkspaceRecord{{
		SourceName: "app", TerrapodID: "ws-a", State: "created",
		ExpectedVarCount: 2, StateLineage: "lin-1", StateSerial: 5,
	}}}
}

func TestVerify_Parity_OK(t *testing.T) {
	c := newVerifyServer(t, "app", 2, 5, "lin-1")
	r := runVerify(t.Context(), c, baselineState())
	if r.FailedCount != 0 {
		t.Fatalf("expected parity OK, got failures: %+v", r.Workspaces)
	}
}

func TestVerify_Parity_VarCountDrop_Fails(t *testing.T) {
	c := newVerifyServer(t, "app", 1, 5, "lin-1") // 1 var on dest, migrated 2
	r := runVerify(t.Context(), c, baselineState())
	if r.FailedCount != 1 {
		t.Fatalf("expected var-count mismatch to fail: %+v", r.Workspaces)
	}
}

func TestVerify_Parity_LineageMismatch_Fails(t *testing.T) {
	c := newVerifyServer(t, "app", 2, 5, "OTHER-lineage")
	r := runVerify(t.Context(), c, baselineState())
	if r.FailedCount != 1 {
		t.Fatalf("expected lineage mismatch to fail: %+v", r.Workspaces)
	}
}

func TestVerify_Parity_StateMissing_Fails(t *testing.T) {
	c := newVerifyServer(t, "app", 2, -1, "") // no current state version
	r := runVerify(t.Context(), c, baselineState())
	if r.FailedCount != 1 {
		t.Fatalf("expected missing-state to fail: %+v", r.Workspaces)
	}
}
