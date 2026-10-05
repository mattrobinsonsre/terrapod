package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Per-workspace cloud identity (#1901) reaches an agent through two surfaces:
// `oidc_audiences` on the workspace write tools, which is the opt-in; and a
// read-only view of the signing keys Terrapod publishes, which is what the
// agent reads when a federated run cannot authenticate.
//
// Three things can go wrong on wrappers this thin, and each is silent: the
// setting never reaches the server; an unset field is sent as empty and
// silently opts the workspace out; or the agent is handed a switch whose
// description does not say that setting it grants nothing by itself.

func TestWorkspaceCreateSendsOIDCAudiences(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":["sts.example.com","api://example-exchange"]}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name":           "my-workspace",
		"oidc_audiences": []any{"sts.example.com", "api://example-exchange"},
	})

	sent, ok := attrs["oidc-audiences"].([]any)
	if !ok {
		t.Fatalf("oidc-audiences never reached the server as a list; attributes were %v", attrs)
	}
	if len(sent) != 2 || sent[0] != "sts.example.com" || sent[1] != "api://example-exchange" {
		t.Errorf("server received oidc-audiences=%v, want the two values verbatim and in order", sent)
	}

	// The created workspace must report them back, or an agent cannot confirm
	// what it just configured.
	got, ok := out["oidc-audiences"].([]any)
	if !ok || len(got) != 2 {
		t.Errorf("created workspace reports oidc-audiences %v, want two entries", out["oidc-audiences"])
	}
}

// Unset must be OMITTED. Sent as an empty list it would read as a deliberate
// "mint nothing" — the same bytes an operator sends to opt a workspace OUT —
// which is harmless on create only because the default happens to agree, and
// wrong the moment an agent reuses the same field on an update.
func TestWorkspaceCreateOmitsOIDCAudiencesWhenUnset(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w,
			`{"data":{"type":"workspaces","id":"ws-1","attributes":{"name":"my-workspace"}}}`)
	})

	callTool(t, sess, "terrapod_workspace_create", map[string]any{"name": "my-workspace"})

	if _, sent := attrs["oidc-audiences"]; sent {
		t.Errorf("an unset oidc_audiences was still sent: %v", attrs)
	}
}

func TestWorkspaceUpdateSendsOIDCAudiences(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":["sts.example.com"]}}}`)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":   "ws-1",
		"oidc_audiences": []any{"sts.example.com"},
	})

	sent, ok := attrs["oidc-audiences"].([]any)
	if !ok || len(sent) != 1 || sent[0] != "sts.example.com" {
		t.Errorf("update sent oidc-audiences=%v, want [sts.example.com]", attrs["oidc-audiences"])
	}
}

// Clearing the audiences is how a workspace is opted back OUT of minting a
// cloud identity, and it is the direction that matters: an agent asked to stop
// a workspace federating must actually stop it rather than silently leave it
// minting tokens. An empty array has to travel.
func TestWorkspaceUpdateSendsAnEmptyOIDCAudienceListToOptOut(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":[]}}}`)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":   "ws-1",
		"oidc_audiences": []any{},
	})

	sent, ok := attrs["oidc-audiences"]
	if !ok {
		t.Fatalf("an explicit empty oidc_audiences was dropped, so the workspace cannot be "+
			"opted back out; attributes were %v", attrs)
	}
	list, isList := sent.([]any)
	if !isList || len(list) != 0 {
		t.Errorf("update sent oidc-audiences=%#v, want an empty list", sent)
	}
}

// `terrapod_workspace_get` returns the SDK's Workspace verbatim, so the
// audiences are visible to an agent diagnosing a workspace without it having to
// be told where to look. A read surface the write surface can set but the read
// surface cannot show is the shape of drift the API-to-consumer contract exists
// to stop.
func TestWorkspaceGetSurfacesOIDCAudiences(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":["sts.example.com"]}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_get", map[string]any{"workspace": "ws-1"})

	got, ok := out["oidc-audiences"].([]any)
	if !ok || len(got) != 1 || got[0] != "sts.example.com" {
		t.Errorf("terrapod_workspace_get reports oidc-audiences %v, want [sts.example.com]",
			out["oidc-audiences"])
	}
}

// The signing-key read: the path has to be right, and `signing` has to come off
// the wire per key rather than be inferred from the timestamps. A rotation
// publishes the new key immediately and signs with it only after a propagation
// window, so "the newest key signs" is exactly the wrong conclusion for an
// agent to draw while diagnosing a rejected token.
func TestOIDCSigningKeysTool(t *testing.T) {
	var path string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":[
		  {"type":"oidc-signing-keys","id":"kid-old","attributes":{"kid":"kid-old",
		    "created-at":"2026-01-01T00:00:00Z","activates-at":"2026-01-01T00:00:00Z",
		    "retired-at":null,"signing":true}},
		  {"type":"oidc-signing-keys","id":"kid-new","attributes":{"kid":"kid-new",
		    "created-at":"2026-06-01T00:00:00Z","activates-at":"2026-06-01T01:00:00Z",
		    "retired-at":null,"signing":false}}],
		  "meta":{"signing-kid":"kid-old"}}`)
	})

	out := callTool(t, sess, "terrapod_oidc_signing_keys", map[string]any{})

	if want := "/api/terrapod/v1/oidc/signing-keys"; path != want {
		t.Errorf("tool requested %q, want %q", path, want)
	}
	if out["SigningKID"] != "kid-old" {
		t.Errorf("SigningKID = %v, want kid-old", out["SigningKID"])
	}
	keys, ok := out["Keys"].([]any)
	if !ok || len(keys) != 2 {
		t.Fatalf("Keys = %v, want two entries", out["Keys"])
	}
	signing := map[string]bool{}
	for _, raw := range keys {
		k, ok := raw.(map[string]any)
		if !ok {
			t.Fatalf("key entry is %T, want an object", raw)
		}
		kid, _ := k["kid"].(string)
		s, _ := k["signing"].(bool)
		signing[kid] = s
	}
	if !signing["kid-old"] {
		t.Error("kid-old should report signing=true")
	}
	if signing["kid-new"] {
		t.Error("kid-new is published but not yet active; reporting it as signing would tell an " +
			"agent the rotation had taken effect before any federation target fetched the JWKS")
	}
}

// Platform admin only, and a 403 has to surface as a tool error rather than an
// empty key set — "no keys published" and "you may not look" lead an agent to
// opposite conclusions about why a run cannot authenticate.
func TestOIDCSigningKeysToolSurfacesAForbidden(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, `{"errors":[{"detail":"admin required","status":"403"}]}`)
	})

	res, err := sess.CallTool(t.Context(), &mcp.CallToolParams{
		Name: "terrapod_oidc_signing_keys", Arguments: map[string]any{},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatalf("a 403 came back as a success: %v", res.StructuredContent)
	}
	if txt := resultText(t, res); !strings.Contains(strings.ToLower(txt), "admin") {
		t.Errorf("error text %q does not say the call needs admin", txt)
	}
}

// Read-only, and it must stay that way: an MCP host may auto-approve a
// read-only tool, and this one reads a published trust root's public half.
func TestOIDCSigningKeysToolIsReadOnly(t *testing.T) {
	for _, e := range liveCatalogue(t) {
		if e.Name != "terrapod_oidc_signing_keys" {
			continue
		}
		if !e.ReadOnly {
			t.Error("terrapod_oidc_signing_keys must be annotated read-only")
		}
		if e.Destructive {
			t.Error("terrapod_oidc_signing_keys must not be annotated destructive")
		}
		return
	}
	t.Fatal("terrapod_oidc_signing_keys is not registered")
}

// Rotation is deliberately NOT a tool. It replaces a published trust root for
// every federated workspace at once and the new key only signs after a
// propagation window, so it is an operator act — a Terraform plan a human
// reads, or a deliberate API call — not something an agent should reach for to
// unblock the task in front of it. The absence is the decision; this is what
// stops it being added back without one.
func TestNoOIDCSigningKeyRotateTool(t *testing.T) {
	for _, e := range liveCatalogue(t) {
		if strings.Contains(e.Name, "signing_key") && strings.Contains(e.Name, "rotate") {
			t.Errorf("tool %q rotates a published trust root; that is an operator act, not an "+
				"agent's. If it is genuinely wanted, annotate it destructive and record the "+
				"decision here rather than deleting this test.", e.Name)
		}
	}
}

// The description is the whole safety mechanism on the write side. An agent
// handed a bare "oidc_audiences" list has no way to know what it is and will
// guess — most likely that adding an audience grants access, when the
// federation target's own trust policy is what decides that.
func TestOIDCAudiencesDescriptionExplainsTheOptIn(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		t.Run(tool, func(t *testing.T) {
			raw, ok := schemas[tool]
			if !ok {
				t.Fatalf("tool %s is not registered", tool)
			}
			var doc struct {
				Properties map[string]struct {
					Description string `json:"description"`
				} `json:"properties"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Fatalf("decode input schema: %v", err)
			}
			desc := doc.Properties["oidc_audiences"].Description
			if desc == "" {
				t.Fatalf("%s has no oidc_audiences field", tool)
			}
			// Each phrase stands for something an agent would otherwise get
			// wrong: that empty is the default and means "mints nothing"; that
			// an audience is the federation target's own string rather than
			// anything Terrapod defines; and that setting it grants nothing by
			// itself.
			for _, want := range []string{"opt-in", "Empty", "federation target", "replayable"} {
				if !strings.Contains(desc, want) {
					t.Errorf("description never mentions %q, so an agent cannot tell what "+
						"setting this does:\n%s", want, desc)
				}
			}
			// No cloud is named: the whole point is that Terrapod mints a token
			// and the operator's own provider configuration consumes it. A
			// description that names one would teach an agent the opposite.
			for _, banned := range []string{"role ARN", "role_arn", "tenant id", "tenant_id"} {
				if strings.Contains(strings.ToLower(desc), strings.ToLower(banned)) {
					t.Errorf("description mentions %q; nothing in Terrapod is cloud-specific "+
						"and the audience list is the only per-workspace value:\n%s", banned, desc)
				}
			}
		})
	}
}
