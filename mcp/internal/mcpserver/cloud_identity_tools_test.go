package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Per-workspace cloud identity (#1901) reaches an agent through three surfaces:
// `oidc_audiences` on the workspace write tools, which is the opt-in; a
// read-only view of the signing keys Terrapod publishes, which is what the
// agent reads when a federated run cannot authenticate; and the rotation,
// which is the one call here that can break every federated workspace at once.
//
// The audiences are a MAP keyed on the provider configuration a token is for —
// `aws`, or `aws.west` for one aliased configuration — and every value is a
// list even when it holds one entry. Four things can go wrong on wrappers this
// thin, and each is silent: the map never reaches the server; an unset field is
// sent as empty and silently drops every override; an aliased key is folded
// into its bare provider so two targets become one; or the agent is handed a
// switch whose description does not say that setting it grants nothing by
// itself.

// twoTargetsAliased is the shape that distinguishes a map from a flat list: a
// bare provider, the same provider aliased, and a second provider holding two
// interchangeable audiences.
func twoTargetsAliased() map[string]any {
	return map[string]any{
		"aws":      []any{"sts.example.com"},
		"aws.west": []any{"sts-west.example.com"},
		"vault":    []any{"https://vault-a.example.com", "https://vault-b.example.com"},
	}
}

func TestWorkspaceCreateSendsOIDCAudiences(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":{"aws":["sts.example.com"],`+
			`"aws.west":["sts-west.example.com"],`+
			`"vault":["https://vault-a.example.com","https://vault-b.example.com"]}}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_create", map[string]any{
		"name":           "my-workspace",
		"oidc_audiences": twoTargetsAliased(),
	})

	sent, ok := attrs["oidc-audiences"].(map[string]any)
	if !ok {
		t.Fatalf("oidc-audiences never reached the server as an object; attributes were %v", attrs)
	}
	if len(sent) != 3 {
		t.Fatalf("server received %d targets, want 3: %v", len(sent), sent)
	}
	// The alias is part of the key. Folding `aws.west` into `aws` would silently
	// merge two targets, and the surviving one's audiences would be minted for
	// the other's provider configuration too.
	for _, key := range []string{"aws", "aws.west", "vault"} {
		if _, present := sent[key]; !present {
			t.Errorf("key %q did not reach the server; keys were %v", key, sent)
		}
	}
	if got, _ := sent["aws"].([]any); len(got) != 1 || got[0] != "sts.example.com" {
		t.Errorf("oidc-audiences[aws] = %v, want [sts.example.com]", sent["aws"])
	}
	// Several audiences for one target are a deliberate
	// these-are-interchangeable statement, and ORDER is the operator's: the
	// server stores the list verbatim, so reordering here would make a
	// provider's plan disagree with its own apply.
	vault, _ := sent["vault"].([]any)
	if len(vault) != 2 || vault[0] != "https://vault-a.example.com" || vault[1] != "https://vault-b.example.com" {
		t.Errorf("oidc-audiences[vault] = %v, want both values verbatim and in order", sent["vault"])
	}

	// The created workspace must report them back, or an agent cannot confirm
	// what it just configured.
	got, ok := out["oidc-audiences"].(map[string]any)
	if !ok || len(got) != 3 {
		t.Errorf("created workspace reports oidc-audiences %v, want three targets", out["oidc-audiences"])
	}
}

// Unset must be OMITTED. Sent as an empty object it would read as a deliberate
// "drop every override" — the same bytes an operator sends to opt a workspace
// back to the deployment's catalogue — which is harmless on create only
// because there is nothing to drop yet, and wrong the moment an agent reuses
// the same field on an update.
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
			`{"name":"my-workspace","oidc-audiences":{"vault.eu":["https://vault.example.com"]}}}}`)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":   "ws-1",
		"oidc_audiences": map[string]any{"vault.eu": []any{"https://vault.example.com"}},
	})

	sent, ok := attrs["oidc-audiences"].(map[string]any)
	if !ok {
		t.Fatalf("update sent oidc-audiences=%#v, want an object", attrs["oidc-audiences"])
	}
	// `vault.eu`, not `vault`: an aliased configuration is its own target, and
	// the alias has to survive the write or the override lands on the wrong one.
	got, _ := sent["vault.eu"].([]any)
	if len(got) != 1 || got[0] != "https://vault.example.com" {
		t.Errorf("update sent oidc-audiences=%v, want vault.eu to carry the one audience", sent)
	}
}

// Clearing every override is how a workspace is dropped back to inheriting the
// deployment's catalogue wholesale, and it is the direction that matters: an
// agent asked to stop a workspace overriding must actually stop it rather than
// silently leave the old overrides in place. An empty object has to travel.
func TestWorkspaceUpdateSendsAnEmptyOIDCAudienceMapToClearOverrides(t *testing.T) {
	var attrs map[string]any
	sess := crudCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":{}}}}`)
	})

	callTool(t, sess, "terrapod_workspace_update", map[string]any{
		"workspace_id":   "ws-1",
		"oidc_audiences": map[string]any{},
	})

	sent, ok := attrs["oidc-audiences"]
	if !ok {
		t.Fatalf("an explicit empty oidc_audiences was dropped, so the overrides cannot be "+
			"cleared; attributes were %v", attrs)
	}
	m, isMap := sent.(map[string]any)
	if !isMap || len(m) != 0 {
		t.Errorf("update sent oidc-audiences=%#v, want an empty object", sent)
	}
}

// `terrapod_workspace_get` returns the SDK's Workspace verbatim, so the
// audiences are visible to an agent diagnosing a workspace without it having to
// be told where to look. A read surface the write surface can set but the read
// surface cannot show is the shape of drift the API-to-consumer contract exists
// to stop.
//
// This is the MERGED view — the deployment's catalogue with the workspace's
// override applied per key — so the map shape has to survive the read whole.
// Flattening it would hand an agent a list of audiences with no way to tell
// which provider configuration each belongs to, which is the one thing it needs.
func TestWorkspaceGetSurfacesOIDCAudiences(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = io.WriteString(w, `{"data":{"type":"workspaces","id":"ws-1","attributes":`+
			`{"name":"my-workspace","oidc-audiences":{"aws":["sts.example.com"],`+
			`"aws.west":["sts-west.example.com"]}}}}`)
	})

	out := callTool(t, sess, "terrapod_workspace_get", map[string]any{"workspace": "ws-1"})

	got, ok := out["oidc-audiences"].(map[string]any)
	if !ok {
		t.Fatalf("terrapod_workspace_get reports oidc-audiences %#v, want an object",
			out["oidc-audiences"])
	}
	if len(got) != 2 {
		t.Fatalf("read back %d targets, want 2: %v", len(got), got)
	}
	west, _ := got["aws.west"].([]any)
	if len(west) != 1 || west[0] != "sts-west.example.com" {
		t.Errorf("oidc-audiences[aws.west] = %v, want the aliased target intact and separate "+
			"from aws", got["aws.west"])
	}
}

// schemaAccepts reports whether a JSON Schema `type` — which is either one
// string or a union of them — includes want.
func schemaAccepts(raw any, want string) bool {
	switch t := raw.(type) {
	case string:
		return t == want
	case []any:
		for _, one := range t {
			if s, ok := one.(string); ok && s == want {
				return true
			}
		}
	}
	return false
}

// The input schema is the contract an agent codes against, and the map shape is
// the half a description cannot enforce. A `[]string` schema would make every
// well-formed call fail validation at the host before it ever reached Terrapod.
func TestOIDCAudiencesSchemaIsAMapOfStringLists(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_workspace_create", "terrapod_workspace_update"} {
		t.Run(tool, func(t *testing.T) {
			raw, ok := schemas[tool]
			if !ok {
				t.Fatalf("tool %s is not registered", tool)
			}
			// Decoded loosely on purpose: a nilable Go field renders `type` as
			// a UNION (["null","array"]), so a `string`-typed field here would
			// fail on a sibling property and take this assertion with it.
			var doc struct {
				Properties map[string]map[string]any `json:"properties"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Fatalf("decode input schema: %v", err)
			}
			prop, ok := doc.Properties["oidc_audiences"]
			if !ok {
				t.Fatalf("%s has no oidc_audiences field", tool)
			}
			if !schemaAccepts(prop["type"], "object") {
				t.Errorf("oidc_audiences is typed %v, want object — it is keyed on the "+
					"provider configuration, not a flat list", prop["type"])
			}
			values, ok := prop["additionalProperties"].(map[string]any)
			if !ok {
				t.Fatal("oidc_audiences states no value type, so an agent cannot tell a list " +
					"of audiences from a single string")
			}
			if !schemaAccepts(values["type"], "array") {
				t.Errorf("oidc_audiences values are typed %v, want array — always a list even "+
					"for one audience", values["type"])
			}
			items, ok := values["items"].(map[string]any)
			if !ok || !schemaAccepts(items["type"], "string") {
				t.Errorf("oidc_audiences values hold %v, want strings", values["items"])
			}
		})
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

// The signing-key read's description carried a sentence saying rotation was
// deliberately not a tool, which went false the moment one was added — and a
// description is read by an agent, so a stale one does not merely mislead, it
// sends the agent off to construct an API call by hand. Nothing caught it,
// because no test reads a tool's prose. This does.
func TestOIDCSigningKeysDescriptionPointsAtTheRotateTool(t *testing.T) {
	srv, _, err := New(Config{Host: "example.test", Name: "terrapod-test", Token: "test-token"})
	if err != nil {
		t.Fatalf("build server: %v", err)
	}
	ct, st := mcp.NewInMemoryTransports()
	go func() { _ = srv.Run(t.Context(), st) }()
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).
		Connect(t.Context(), ct, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	res, err := sess.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}

	var desc string
	for _, tool := range res.Tools {
		if tool.Name == "terrapod_oidc_signing_keys" {
			desc = tool.Description
		}
	}
	if desc == "" {
		t.Fatal("terrapod_oidc_signing_keys is not registered or has no description")
	}
	if !strings.Contains(desc, "terrapod_oidc_signing_key_rotate") {
		t.Errorf("the signing-key read never names the rotate tool, so an agent told to rotate "+
			"has nowhere to go from here:\n%s", desc)
	}
	// The exact phrasing that went stale. Asserted literally because the
	// failure mode is a sentence that stays behind, not one that gets reworded.
	for _, stale := range []string{"NOT exposed as a tool", "not exposed as a tool"} {
		if strings.Contains(desc, stale) {
			t.Errorf("the description still claims rotation is not a tool; it is "+
				"terrapod_oidc_signing_key_rotate:\n%s", desc)
		}
	}
}

// The rotation: a POST to the actions route, and the note has to survive. The
// method matters on its own — a GET sent here decodes a 404 body into a
// zero-valued result, which reads as "nothing rotated" rather than as a mistake.
func TestOIDCSigningKeyRotateTool(t *testing.T) {
	var method, path string
	sess := toolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		method, path = r.Method, r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"data":{"type":"oidc-signing-keys","id":"kid-new",
		  "attributes":{"kid":"kid-new","created-at":"2026-06-01T00:00:00Z",
		  "activates-at":"2026-06-01T01:00:00Z","retired-at":null,"signing":false}},
		  "meta":{"note":"Published now; signs from activates-at."}}`)
	})

	out := callTool(t, sess, "terrapod_oidc_signing_key_rotate", map[string]any{})

	if method != http.MethodPost {
		t.Errorf("tool used %s, want POST", method)
	}
	if want := "/api/terrapod/v1/oidc/signing-keys/actions/rotate"; path != want {
		t.Errorf("tool requested %q, want %q", path, want)
	}
	key, ok := out["Key"].(map[string]any)
	if !ok {
		t.Fatalf("Key = %#v, want the rotated key", out["Key"])
	}
	if key["kid"] != "kid-new" {
		t.Errorf("rotated kid = %v, want kid-new", key["kid"])
	}
	// The new key does not sign yet. Reporting it as signing would tell the
	// agent — and through it the operator — that the rotation had taken effect
	// before any federation target had a chance to fetch the JWKS.
	if s, _ := key["signing"].(bool); s {
		t.Error("a freshly rotated key must not read as signing")
	}
	// The note is the only statement of when the key starts signing, and its
	// value is the operator's configuration, so dropping it leaves the agent
	// with nothing true to say about the window.
	if note, _ := out["Note"].(string); note == "" {
		t.Error("meta.note was dropped; it is the only statement of when the key starts signing")
	}
}

// An operator-supplied signing key is not Terrapod's to rotate and the server
// says so with 409. It has to surface as a tool error: a conflict swallowed
// into an empty result reads as a rotation that happened, and the agent's next
// move would be to tell the operator to wait out a propagation window that is
// never going to start.
func TestOIDCSigningKeyRotateToolSurfacesTheOperatorSuppliedConflict(t *testing.T) {
	sess := toolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusConflict)
		_, _ = io.WriteString(w,
			`{"errors":[{"detail":"the deployment signs with an operator-supplied key","status":"409"}]}`)
	})

	res, err := sess.CallTool(t.Context(), &mcp.CallToolParams{
		Name: "terrapod_oidc_signing_key_rotate", Arguments: map[string]any{},
	})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !res.IsError {
		t.Fatalf("a 409 came back as a success: %v", res.StructuredContent)
	}
	if txt := resultText(t, res); !strings.Contains(strings.ToLower(txt), "operator-supplied") {
		t.Errorf("error text %q does not say why there is nothing to rotate", txt)
	}
}

// Rotation IS a tool (everything the feature can do is reachable through MCP),
// and the annotation is the whole safety mechanism: a host decides whether to
// ask the operator from it. Asserted LIVE rather than through the catalogue
// golden, because `destructive` is `omitempty` there — a tool that lost its
// annotation entirely would serialise identically to one deliberately marked
// non-destructive, so the snapshot would accept the regression and freeze it.
//
// Destructive rather than mutating, because the blast radius is the deployment:
// it retires the key every federation target has already fetched, and a
// mishandled rotation stops every federated run authenticating at once.
func TestOIDCSigningKeyRotateToolIsAnnotatedDestructive(t *testing.T) {
	for _, e := range liveCatalogue(t) {
		if e.Name != "terrapod_oidc_signing_key_rotate" {
			continue
		}
		if !e.Destructive {
			t.Error("terrapod_oidc_signing_key_rotate must be annotated destructive: it retires " +
				"a published trust root's signing key for every federated workspace at once, " +
				"so a host must put it in front of a human")
		}
		if e.ReadOnly {
			t.Error("terrapod_oidc_signing_key_rotate must not be annotated read-only")
		}
		return
	}
	t.Fatal("terrapod_oidc_signing_key_rotate is not registered")
}

// The description is the whole safety mechanism on the write side. An agent
// handed a bare "oidc_audiences" map has no way to know what it is and will
// guess — most likely that adding an audience grants access, when the
// federation target's own trust policy is what decides that; or that the map it
// just read is the map to write back, when the read is merged and the write is
// the override.
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
			// wrong: that this is an opt-in rather than a grant; that the write
			// is an override MERGED over the deployment's, so the merged value
			// it read is not the value to write back; that an alias is part of
			// the key; that an audience is the federation target's own string;
			// that a multi-target token is replayable; and that the trust
			// policy, not this field, decides what the token may do.
			for _, want := range []string{
				"opt-in", "MERGED", "merged per key", "alias is part of the KEY",
				"federation target", "replayable", "trust policy",
			} {
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
						"and the audience map is the only per-workspace value:\n%s", banned, desc)
				}
			}
		})
	}
}

// The update tool's description has to say what an empty object does, because
// it is the destructive-by-omission direction: an agent that believes an empty
// object is a no-op will send one to mean "leave it alone" and drop every
// override the operator set.
func TestOIDCAudiencesUpdateDescriptionExplainsClearing(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	var doc struct {
		Properties map[string]struct {
			Description string `json:"description"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(schemas["terrapod_workspace_update"], &doc); err != nil {
		t.Fatalf("decode input schema: %v", err)
	}
	desc := doc.Properties["oidc_audiences"].Description
	for _, want := range []string{"Omitting the field leaves", "EMPTY object to clear"} {
		if !strings.Contains(desc, want) {
			t.Errorf("update description never says %q, so an agent cannot tell leaving the "+
				"overrides alone from clearing them:\n%s", want, desc)
		}
	}
}
