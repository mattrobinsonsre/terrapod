package terrapod

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestGetEncryptionStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || !strings.HasSuffix(r.URL.Path, "/admin/encryption") {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"type":"encryption-status","attributes":{
		  "enabled":true,"provider":"vault_transit","active_version":2,
		  "dek_versions":[1,2],"canary_ok":true,"decryptable":true}}}`))
	}))
	t.Cleanup(srv.Close)

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	st, err := c.GetEncryptionStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !st.Enabled || st.Provider != "vault_transit" || !st.Decryptable {
		t.Fatalf("unexpected status: %+v", st)
	}
	if st.ActiveVersion == nil || *st.ActiveVersion != 2 || len(st.DEKVersions) != 2 {
		t.Fatalf("unexpected versions: %+v", st)
	}
}
