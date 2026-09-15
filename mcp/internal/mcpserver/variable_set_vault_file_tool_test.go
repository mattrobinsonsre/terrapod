package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// terrapod_variable_set with a Vault reference that asks for file delivery
// (#1619), driven through a real MCP client against a fake Terrapod. The tool
// must pass the reference through as the opaque string it is — an agent that
// writes `file` into a reference has to see it land at the API unchanged.

const vaultFileRef = `{"source":"vault","mount":"secret","path":"apps/gcp","field":"sa_json","file":{"name":"gcp/adc.json"}}`

// crudCaller is toolCaller plus the CRUD tools, which is where
// terrapod_variable_set is registered.
func crudCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerCRUD(srv, c)

	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, st, nil); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	return sess
}

// writeAttrs decodes a JSON:API write body and returns its attributes.
func writeAttrs(t *testing.T, r *http.Request) map[string]any {
	t.Helper()
	raw, _ := io.ReadAll(r.Body)
	var body struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Errorf("write body is not JSON: %v: %s", err, raw)
	}
	return body.Data.Attributes
}

func echoVar(w http.ResponseWriter, value any) {
	out, _ := json.Marshal(map[string]any{"data": map[string]any{
		"id": "var-1", "type": "vars", "attributes": map[string]any{
			"key": "GOOGLE_APPLICATION_CREDENTIALS", "category": "env", "sensitive": true,
			"value-source": "vault", "value": value,
		},
	}})
	_, _ = w.Write(out)
}

func TestVariableSetToolCreatesAVaultFileReferenceVerbatim(t *testing.T) {
	var posted map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/vars"):
			_, _ = w.Write([]byte(`{"data":[]}`)) // the key is new → create
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/vars"):
			posted = writeAttrs(t, r)
			w.WriteHeader(http.StatusCreated)
			echoVar(w, posted["value"])
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_variable_set",
		Arguments: map[string]any{
			"workspace_id": "ws-1", "key": "GOOGLE_APPLICATION_CREDENTIALS",
			"category": "env", "value": vaultFileRef, "value_source": "vault",
		},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if posted == nil {
		t.Fatal("the tool never created the variable")
	}
	if posted["value"] != vaultFileRef {
		t.Errorf("reference changed on the way to the API:\n  %v\nwant\n  %s", posted["value"], vaultFileRef)
	}
	if posted["value-source"] != "vault" {
		t.Errorf("value-source = %v, want vault", posted["value-source"])
	}
	if out := resultText(t, res); !strings.Contains(out, "gcp/adc.json") {
		t.Errorf("the returned variable does not carry the reference: %s", out)
	}
}

func TestVariableSetToolUpdatesAVaultFileReferenceVerbatim(t *testing.T) {
	// The upsert's other branch: the key exists, so the new reference is a
	// PATCH — and that path must not rewrite it either.
	var patched map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/vars"):
			_, _ = w.Write([]byte(`{"data":[{"id":"var-1","type":"vars","attributes":{` +
				`"key":"GOOGLE_APPLICATION_CREDENTIALS","category":"env","sensitive":true,` +
				`"value-source":"vault","value":"{\"source\":\"vault\",\"mount\":\"secret\",\"path\":\"apps/gcp\",\"field\":\"sa_json\"}"}}]}`))
		case r.Method == http.MethodPatch && strings.HasSuffix(r.URL.Path, "/vars/var-1"):
			patched = writeAttrs(t, r)
			echoVar(w, patched["value"])
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	})

	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "terrapod_variable_set",
		Arguments: map[string]any{
			"workspace_id": "ws-1", "key": "GOOGLE_APPLICATION_CREDENTIALS",
			"category": "env", "value": vaultFileRef, "value_source": "vault",
		},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if res.IsError {
		t.Fatalf("tool reported an error: %s", resultText(t, res))
	}
	if patched == nil {
		t.Fatal("an existing key was not updated in place")
	}
	if patched["value"] != vaultFileRef {
		t.Errorf("reference changed on the way to the API:\n  %v\nwant\n  %s", patched["value"], vaultFileRef)
	}
}

func TestVariableSetToolDescribesFileDelivery(t *testing.T) {
	// An agent only writes `file` into a reference if the tool tells it the
	// key exists. The catalogue golden pins the exact text; this pins intent.
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {})
	tools, err := sess.ListTools(context.Background(), &mcp.ListToolsParams{})
	if err != nil {
		t.Fatalf("ListTools: %v", err)
	}
	for _, tool := range tools.Tools {
		if tool.Name != "terrapod_variable_set" {
			continue
		}
		schema, _ := json.Marshal(tool.InputSchema)
		for _, want := range []string{`\"file\"`, "/var/run/terrapod/files/", "~/"} {
			if !strings.Contains(string(schema), want) {
				t.Errorf("value_source schema does not mention %s: %s", want, schema)
			}
		}
		return
	}
	t.Fatal("terrapod_variable_set is not registered")
}
