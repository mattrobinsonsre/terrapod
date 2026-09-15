package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
)

// File delivery for a Vault reference (#1619). The SDK treats the reference as
// an opaque string, so the only thing that can go wrong here is the SDK
// touching it: re-encoding, reordering or dropping keys. These tests send a
// reference with `file` through create, read and update and require every
// byte to arrive at the server, and come back from it, unchanged.

const fileRef = `{"source":"vault","vault":"default","mount":"kvv2","path":"apps/x","field":"token","engine":"kv2","method":"GET","file":{"name":"gcp/adc.json"}}`
const renamedFileRef = `{"source":"vault","vault":"default","mount":"kvv2","path":"apps/x","field":"token","engine":"kv2","method":"GET","file":{"name":"~/.config/gcloud/adc.json"}}`

// fakeVaultVarServer stores one variable, records every write body, and
// serves the stored value back exactly as written.
type fakeVaultVarServer struct {
	mu     sync.Mutex
	value  string
	writes []recordedWrite
}

type recordedWrite struct {
	method string
	body   map[string]any
}

func (f *fakeVaultVarServer) handler(t *testing.T, collectionSuffix string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		w.Header().Set("Content-Type", "application/vnd.api+json")

		if r.Method == http.MethodPost || r.Method == http.MethodPatch {
			raw, _ := io.ReadAll(r.Body)
			var body map[string]any
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Errorf("request body is not JSON: %v: %s", err, raw)
			}
			f.writes = append(f.writes, recordedWrite{method: r.Method, body: body})
			if v, ok := body["data"].(map[string]any)["attributes"].(map[string]any)["value"].(string); ok {
				f.value = v
			}
		}

		one := func() map[string]any {
			return map[string]any{"id": "var-1", "type": "vars", "attributes": map[string]any{
				"key": "GOOGLE_APPLICATION_CREDENTIALS", "category": "env", "sensitive": true,
				"value-source": "vault", "value": f.value,
			}}
		}
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, collectionSuffix):
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"data": one()})
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, collectionSuffix):
			_ = json.NewEncoder(w).Encode(map[string]any{"data": []any{one()}})
		case r.Method == http.MethodPatch && strings.HasSuffix(r.URL.Path, collectionSuffix+"/var-1"):
			_ = json.NewEncoder(w).Encode(map[string]any{"data": one()})
		default:
			http.Error(w, "unhandled "+r.Method+" "+r.URL.Path, http.StatusNotFound)
		}
	}
}

// attrsOf returns the attributes a write sent, failing if the envelope is not
// the JSON:API one the server expects.
func attrsOf(t *testing.T, w recordedWrite) map[string]any {
	t.Helper()
	data, ok := w.body["data"].(map[string]any)
	if !ok || data["type"] != "vars" {
		t.Fatalf("%s body is not a vars resource: %v", w.method, w.body)
	}
	attrs, ok := data["attributes"].(map[string]any)
	if !ok {
		t.Fatalf("%s body has no attributes: %v", w.method, w.body)
	}
	return attrs
}

func TestWorkspaceVariableCarriesAFileReferenceVerbatim(t *testing.T) {
	f := &fakeVaultVarServer{}
	srv := httptest.NewServer(f.handler(t, "/api/v2/workspaces/ws-1/vars"))
	defer srv.Close()
	c := mustVarClient(t, srv)

	created, err := c.CreateVariable(t.Context(), "ws-1", CreateVariableRequest{
		Key: "GOOGLE_APPLICATION_CREDENTIALS", Category: "env", Value: fileRef, ValueSource: "vault",
	})
	if err != nil {
		t.Fatalf("CreateVariable: %v", err)
	}
	want := map[string]any{
		"key": "GOOGLE_APPLICATION_CREDENTIALS", "category": "env",
		"value": fileRef, "value-source": "vault",
	}
	if got := attrsOf(t, f.writes[0]); !reflect.DeepEqual(got, want) {
		t.Errorf("create sent\n  %v\nwant\n  %v", got, want)
	}
	if created.Value != fileRef {
		t.Errorf("create returned a different reference:\n  %s\nwant\n  %s", created.Value, fileRef)
	}

	read, err := c.GetVariable(t.Context(), "ws-1", "var-1")
	if err != nil {
		t.Fatalf("GetVariable: %v", err)
	}
	if read.Value != fileRef || read.ValueSource != "vault" {
		t.Errorf("read changed the reference: %q (%q)", read.Value, read.ValueSource)
	}

	newValue := renamedFileRef
	updated, err := c.UpdateVariable(t.Context(), "ws-1", "var-1", UpdateVariableRequest{Value: &newValue})
	if err != nil {
		t.Fatalf("UpdateVariable: %v", err)
	}
	// Only the value moves on an update that only sets the value — the SDK
	// must not smuggle in a source, category or sensitivity change.
	if got := attrsOf(t, f.writes[1]); !reflect.DeepEqual(got, map[string]any{"value": renamedFileRef}) {
		t.Errorf("update sent %v, want only the new value", got)
	}
	if updated.Value != renamedFileRef {
		t.Errorf("update returned %q, want %q", updated.Value, renamedFileRef)
	}
}

func TestVarsetVariableCarriesAFileReferenceVerbatim(t *testing.T) {
	f := &fakeVaultVarServer{}
	srv := httptest.NewServer(f.handler(t, "/api/v2/varsets/varset-1/relationships/vars"))
	defer srv.Close()
	c := mustVarClient(t, srv)

	created, err := c.CreateVarsetVariable(t.Context(), "varset-1", CreateVarsetVariableRequest{
		Key: "GOOGLE_APPLICATION_CREDENTIALS", Category: "env", Value: fileRef, ValueSource: "vault",
	})
	if err != nil {
		t.Fatalf("CreateVarsetVariable: %v", err)
	}
	want := map[string]any{
		"key": "GOOGLE_APPLICATION_CREDENTIALS", "category": "env",
		"value": fileRef, "value-source": "vault",
	}
	if got := attrsOf(t, f.writes[0]); !reflect.DeepEqual(got, want) {
		t.Errorf("create sent\n  %v\nwant\n  %v", got, want)
	}
	if created.Value != fileRef {
		t.Errorf("create returned %q", created.Value)
	}

	read, err := c.GetVarsetVariable(t.Context(), "varset-1", "var-1")
	if err != nil {
		t.Fatalf("GetVarsetVariable: %v", err)
	}
	if read.Value != fileRef || read.ValueSource != "vault" {
		t.Errorf("read changed the reference: %q (%q)", read.Value, read.ValueSource)
	}

	newValue := renamedFileRef
	updated, err := c.UpdateVarsetVariable(t.Context(), "varset-1", "var-1", UpdateVarsetVariableRequest{Value: &newValue})
	if err != nil {
		t.Fatalf("UpdateVarsetVariable: %v", err)
	}
	if got := attrsOf(t, f.writes[1]); !reflect.DeepEqual(got, map[string]any{"value": renamedFileRef}) {
		t.Errorf("update sent %v, want only the new value", got)
	}
	if updated.Value != renamedFileRef {
		t.Errorf("update returned %q, want %q", updated.Value, renamedFileRef)
	}
}
