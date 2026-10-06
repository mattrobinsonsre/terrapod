package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// newSigningKeyFixture serves the two signing-key routes and records what was
// asked of it, so a test can assert the METHOD and PATH as well as the decode —
// a GET sent where the server expects a POST decodes a 404 body into a
// zero-valued result, which reads as "no keys" rather than as a mistake.
func newSigningKeyFixture(t *testing.T, listBody, rotateBody string, rotateStatus int) (*Client, *[]string) {
	t.Helper()
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/oidc/signing-keys":
			_, _ = w.Write([]byte(listBody))
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/oidc/signing-keys/actions/rotate":
			w.WriteHeader(rotateStatus)
			_, _ = w.Write([]byte(rotateBody))
		default:
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"not found"}]}`))
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c, &seen
}

// Two published keys mid-rotation: the older one still signing, the newer one
// published but not yet active. That is the state a rotation actually leaves
// behind, and it is the one where reading only the timestamps gives the wrong
// answer about which key is signing.
const twoKeysMidRotation = `{"data":[
  {"type":"oidc-signing-keys","id":"kid-old","attributes":{
    "kid":"kid-old","created-at":"2026-01-01T00:00:00Z",
    "activates-at":"2026-01-01T00:00:00Z","retired-at":null,"signing":true}},
  {"type":"oidc-signing-keys","id":"kid-new","attributes":{
    "kid":"kid-new","created-at":"2026-06-01T00:00:00Z",
    "activates-at":"2026-06-01T01:00:00Z","retired-at":null,"signing":false}}],
  "meta":{"signing-kid":"kid-old"}}`

func TestListOIDCSigningKeys(t *testing.T) {
	c, seen := newSigningKeyFixture(t, twoKeysMidRotation, "", http.StatusCreated)

	got, err := c.ListOIDCSigningKeys(context.Background())
	if err != nil {
		t.Fatalf("ListOIDCSigningKeys: %v", err)
	}
	if want := []string{"GET /api/terrapod/v1/oidc/signing-keys"}; len(*seen) != 1 || (*seen)[0] != want[0] {
		t.Fatalf("requests = %v, want %v", *seen, want)
	}
	if len(got.Keys) != 2 {
		t.Fatalf("decoded %d keys, want 2: %+v", len(got.Keys), got.Keys)
	}
	if got.SigningKID != "kid-old" {
		t.Errorf("meta signing-kid = %q, want kid-old", got.SigningKID)
	}

	// The newer key is published and NOT signing. A consumer that inferred
	// "newest key signs" from created-at would get this backwards, and a token
	// signed by a key the federation target has not fetched cannot be
	// verified — so `signing` has to come off the wire, per key.
	byKid := map[string]OIDCSigningKey{}
	for _, k := range got.Keys {
		byKid[k.Kid] = k
	}
	if !byKid["kid-old"].Signing {
		t.Error("kid-old should be signing")
	}
	if byKid["kid-new"].Signing {
		t.Error("kid-new is published but not yet active — it must not read as signing")
	}
	if byKid["kid-new"].ActivatesAt != "2026-06-01T01:00:00Z" {
		t.Errorf("kid-new activates-at = %q", byKid["kid-new"].ActivatesAt)
	}
	// A null retired-at must decode as empty, not as the literal "null".
	if byKid["kid-old"].RetiredAt != "" {
		t.Errorf("a null retired-at decoded as %q, want empty", byKid["kid-old"].RetiredAt)
	}
}

// Nothing signing is a legitimate state (the issuer is off, or every key's
// activation window is still ahead), so it must come back as an empty
// SigningKID and no error — not as a failure a caller has to special-case.
func TestListOIDCSigningKeysWithNothingSigning(t *testing.T) {
	c, _ := newSigningKeyFixture(t, `{"data":[],"meta":{"signing-kid":null}}`, "", http.StatusCreated)

	got, err := c.ListOIDCSigningKeys(context.Background())
	if err != nil {
		t.Fatalf("ListOIDCSigningKeys: %v", err)
	}
	if got.SigningKID != "" {
		t.Errorf("signing-kid = %q, want empty", got.SigningKID)
	}
	if len(got.Keys) != 0 {
		t.Errorf("keys = %+v, want none", got.Keys)
	}
}

const rotated = `{"data":{"type":"oidc-signing-keys","id":"kid-new","attributes":{
    "kid":"kid-new","created-at":"2026-06-01T00:00:00Z",
    "activates-at":"2026-06-01T01:00:00Z","retired-at":null,"signing":false}},
  "meta":{"note":"Published now; signs from activates-at."}}`

func TestRotateOIDCSigningKey(t *testing.T) {
	c, seen := newSigningKeyFixture(t, "", rotated, http.StatusCreated)

	got, err := c.RotateOIDCSigningKey(context.Background())
	if err != nil {
		t.Fatalf("RotateOIDCSigningKey: %v", err)
	}
	if want := "POST /api/terrapod/v1/oidc/signing-keys/actions/rotate"; len(*seen) != 1 || (*seen)[0] != want {
		t.Fatalf("requests = %v, want [%s]", *seen, want)
	}
	if got.Key.Kid != "kid-new" {
		t.Errorf("rotated key kid = %q, want kid-new", got.Key.Kid)
	}
	// The new key is published but does not sign yet. Reporting it as signing
	// would tell an operator the rotation had taken effect before any
	// federation target had a chance to fetch the JWKS.
	if got.Key.Signing {
		t.Error("a freshly rotated key must not read as signing")
	}
	if got.Note == "" {
		t.Error("meta.note was dropped — it is the only statement of when the key starts signing")
	}
}

// An operator-supplied key is not Terrapod's to rotate, and the server says so
// with 409. The SDK must surface that as a *ConflictError rather than a bare
// error, because the caller's response differs: this is a configuration fact,
// not a transient failure to retry.
func TestRotateOIDCSigningKeyConflictOnOperatorSuppliedKey(t *testing.T) {
	body := `{"errors":[{"detail":"the deployment signs with an operator-supplied key","status":"409"}]}`
	c, _ := newSigningKeyFixture(t, "", body, http.StatusConflict)

	got, err := c.RotateOIDCSigningKey(context.Background())
	if err == nil {
		t.Fatalf("expected an error, got %+v", got)
	}
	if got != nil {
		t.Errorf("a failed rotation must return nil, got %+v", got)
	}
	if !IsConflict(err) {
		t.Errorf("error %v (%T) is not a *ConflictError", err, err)
	}
}

func TestListOIDCSigningKeysSurfacesAnAuthorizationFailure(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"admin required","status":"403"}]}`))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	got, err := c.ListOIDCSigningKeys(context.Background())
	if err == nil {
		t.Fatalf("expected an error, got %+v", got)
	}
	if got != nil {
		t.Errorf("a failed list must return nil, got %+v", got)
	}
	// Asserted as *AuthorizationError specifically rather than through IsAuth,
	// which also matches a 401. These two endpoints are platform-admin gated,
	// so "your token is fine but is not an admin's" is the answer a caller has
	// to be able to tell from "your token is no good".
	var ze *AuthorizationError
	if !errors.As(err, &ze) {
		t.Errorf("error %v (%T) is not an *AuthorizationError", err, err)
	}
}

// ── oidc-audiences on the workspace ──────────────────────────────────
//
// The reflective gates in wire_completeness_test.go cover the REQUEST builders
// for any new field, because `fill` synthesises a value. They do NOT cover the
// decode side for a map of slices: TestEveryWorkspaceFieldIsDecoded skips any
// type its harness cannot synthesise, and `map[string][]string` is one of them.
// So the read-back — the half that silently returned nil for
// `plan-expiry-seconds` and broke the provider's apply — needs its own
// assertion here.

func TestWorkspaceDecodesOIDCAudiences(t *testing.T) {
	res := resourceWithAttrs("ws-1", "workspaces", map[string]any{
		"name": "my-workspace",
		"oidc-audiences": map[string]any{
			"aws":      []string{"sts.example.com"},
			"aws.west": []string{"sts.example.com"},
			"vault":    []string{"https://vault.example.com"},
		},
	})

	ws := workspaceFromResource(res)
	if len(ws.OIDCAudiences) != 3 {
		t.Fatalf("oidc-audiences decoded as %v, want three targets", ws.OIDCAudiences)
	}
	// Byte-for-byte: an audience is an opaque string the federation target
	// chose, so lower-casing or trimming one here would make a provider plan
	// disagree with its own apply.
	if got := ws.OIDCAudiences["vault"]; len(got) != 1 || got[0] != "https://vault.example.com" {
		t.Errorf("oidc-audiences[vault] = %q, want the value verbatim", got)
	}
	// An alias is part of the KEY, not a nested structure. `aws` and `aws.west`
	// are two independent targets that happen to share a prefix, and a decoder
	// that split on the dot would merge them.
	if _, ok := ws.OIDCAudiences["aws.west"]; !ok {
		t.Error("an aliased target was not decoded as its own key")
	}
}

// Empty is the opted-OUT state and must decode as such rather than as "unknown".
func TestWorkspaceDecodesAnEmptyOIDCAudienceMap(t *testing.T) {
	res := resourceWithAttrs("ws-1", "workspaces", map[string]any{
		"name":           "my-workspace",
		"oidc-audiences": map[string]any{},
	})

	if ws := workspaceFromResource(res); len(ws.OIDCAudiences) != 0 {
		t.Errorf("oidc-audiences = %v, want none", ws.OIDCAudiences)
	}
}

// A multi-entry list is legal and must survive. It means "these audiences are
// interchangeable for this target" — correct for a target that accepts any one
// of them, and refused outright by some (AWS rejects a multi-valued `aud`), so
// the SDK must neither collapse it nor reorder it.
func TestWorkspaceDecodesSeveralAudiencesForOneTarget(t *testing.T) {
	res := resourceWithAttrs("ws-1", "workspaces", map[string]any{
		"name":           "my-workspace",
		"oidc-audiences": map[string]any{"vault": []string{"https://a", "https://b"}},
	})

	got := workspaceFromResource(res).OIDCAudiences["vault"]
	if len(got) != 2 || got[0] != "https://a" || got[1] != "https://b" {
		t.Errorf("oidc-audiences[vault] = %v, want both entries in order", got)
	}
}

// An explicit empty map is how a workspace drops every override, so it has to
// reach the wire. A `len() > 0` guard in the builder would drop it and leave
// the overrides in place after an operator had removed them — which is the
// failure this asserts against, in both directions.
func TestOIDCAudiencesClearsWithAnExplicitEmptyMap(t *testing.T) {
	empty := map[string][]string{}
	for _, tc := range []struct {
		name  string
		attrs map[string]any
	}{
		{"create", workspaceCreateAttrs(CreateWorkspaceRequest{Name: "w", OIDCAudiences: empty})},
		{"update", workspaceUpdateAttrs(UpdateWorkspaceRequest{OIDCAudiences: empty})},
	} {
		got, sent := tc.attrs["oidc-audiences"]
		if !sent {
			t.Errorf("%s dropped an explicit empty oidc-audiences, so the workspace "+
				"cannot be opted back out", tc.name)
			continue
		}
		if m, ok := got.(map[string][]string); !ok || len(m) != 0 {
			t.Errorf("%s sent oidc-audiences = %#v, want an empty map", tc.name, got)
		}
	}
}

// nil means "leave the server-side value alone". Asserting it separately from
// the empty-map case is the whole point: collapsing the two would make every
// PATCH that does not mention audiences clear them.
func TestOIDCAudiencesOmittedWhenNil(t *testing.T) {
	if _, sent := workspaceCreateAttrs(CreateWorkspaceRequest{Name: "w"})["oidc-audiences"]; sent {
		t.Error("create sent oidc-audiences when the caller set nothing")
	}
	if _, sent := workspaceUpdateAttrs(UpdateWorkspaceRequest{})["oidc-audiences"]; sent {
		t.Error("update sent oidc-audiences when the caller set nothing, which would clear it")
	}
}

// The builders are hand-rolled maps, so the marshalled body is the only proof
// the value reaches the server in the shape it expects.
func TestOIDCAudiencesMarshalsIntoTheRequestBody(t *testing.T) {
	attrs := workspaceUpdateAttrs(UpdateWorkspaceRequest{
		OIDCAudiences: map[string][]string{"aws": {"sts.example.com"}},
	})
	body, err := MarshalResourceWithID("ws-1", "workspaces", attrs)
	if err != nil {
		t.Fatalf("MarshalResourceWithID: %v", err)
	}
	var doc struct {
		Data struct {
			Attributes struct {
				OIDCAudiences map[string][]string `json:"oidc-audiences"`
			} `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("unmarshal body: %v", err)
	}
	got := doc.Data.Attributes.OIDCAudiences["aws"]
	if len(got) != 1 || got[0] != "sts.example.com" {
		t.Errorf("body carried oidc-audiences = %v", doc.Data.Attributes.OIDCAudiences)
	}
}

// End to end through a real request: the attribute is sent on an update and the
// server's echo is read back. The unit assertions above cover the builder and
// the decoder in isolation; this one proves the round trip a provider Update
// actually makes, which is where a mismatch surfaces as "Provider produced
// inconsistent result after apply".
func TestUpdateWorkspaceRoundTripsOIDCAudiences(t *testing.T) {
	var sent map[string][]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Data struct {
				Attributes struct {
					OIDCAudiences map[string][]string `json:"oidc-audiences"`
				} `json:"attributes"`
			} `json:"data"`
		}
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			t.Errorf("decode request: %v", err)
		}
		sent = in.Data.Attributes.OIDCAudiences
		w.Header().Set("Content-Type", "application/vnd.api+json")
		// The server echoes the MERGED view, which is wider than what was sent:
		// the workspace set `aws`, and `vault` is inherited from the
		// deployment's catalogue. A consumer must be able to read that back
		// without mistaking the inherited entry for one it owns.
		merged := map[string][]string{"vault": {"https://vault.example.com"}}
		for k, v := range sent {
			merged[k] = v
		}
		out, _ := json.Marshal(map[string]any{"data": map[string]any{
			"id": "ws-1", "type": "workspaces",
			"attributes": map[string]any{"name": "my-workspace", "oidc-audiences": merged},
		}})
		_, _ = w.Write(out)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	ws, err := c.UpdateWorkspace(context.Background(), "ws-1", UpdateWorkspaceRequest{
		OIDCAudiences: map[string][]string{"aws": {"sts.example.com"}},
	})
	if err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	if len(sent) != 1 || len(sent["aws"]) != 1 || sent["aws"][0] != "sts.example.com" {
		t.Fatalf("server received oidc-audiences = %v", sent)
	}
	if got := ws.OIDCAudiences["aws"]; len(got) != 1 || got[0] != "sts.example.com" {
		t.Errorf("read back oidc-audiences[aws] = %v", got)
	}
	if _, ok := ws.OIDCAudiences["vault"]; !ok {
		t.Error("the inherited target was lost on read-back")
	}
}
