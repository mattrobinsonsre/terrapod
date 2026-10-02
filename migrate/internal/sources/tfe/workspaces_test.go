package tfe

import (
	"net/http"
	"strings"
	"testing"
)

// workspaceListBody assembles a TFE JSON:API workspace-list response
// with the given items. The shape matches what TFE returns for
// `GET /organizations/{org}/workspaces`; we don't model every attribute
// — only the ones the migration consumes.
func workspaceListBody(items ...string) string {
	return `{
  "data": [` + strings.Join(items, ",\n") + `],
  "meta": {"pagination": {"current-page": 1, "total-pages": 1, "total-count": ` + intLen(items) + `}}
}`
}

func intLen(s []string) string {
	switch len(s) {
	case 0:
		return "0"
	case 1:
		return "1"
	case 2:
		return "2"
	case 3:
		return "3"
	default:
		return "many" // not used by go-tfe pagination; total-count is informational
	}
}

// wsItem is a builder for one workspace inside a list response.
// Keeping it as a function rather than a struct mirrors how operators
// would copy-paste from real TFE responses while authoring fixtures.
func wsItem(id, name, execMode, tfVersion, workingDir string, autoApply bool, vcsRepo string, tags string) string {
	autoApplyStr := "false"
	if autoApply {
		autoApplyStr = "true"
	}
	return `{
    "id": "` + id + `",
    "type": "workspaces",
    "attributes": {
      "name": "` + name + `",
      "execution-mode": "` + execMode + `",
      "terraform-version": "` + tfVersion + `",
      "working-directory": "` + workingDir + `",
      "auto-apply": ` + autoApplyStr + `
      ` + vcsRepo + `
    }` + tags + `
  }`
}

func vcsRepoAttr(repoURL, oauthID, branch string) string {
	return `,
      "vcs-repo": {
        "repository-http-url": "` + repoURL + `",
        "oauth-token-id": "` + oauthID + `",
        "branch": "` + branch + `",
        "identifier": ""
      }`
}

// tagsRel will be reintroduced when we add a per-workspace tags
// fixture test — TFE's tag relation requires the items to be in the
// response's `included` array AND referenced in the workspace's
// `relationships.tags.data`, and go-tfe's deserialiser is strict
// about both halves. The current tests cover translateTags() unit-
// shape; tagsRel-using integration tests land alongside variable
// migration (4c).

func TestEmitWorkspaces_Happy(t *testing.T) {
	// The simple multi-workspace case: two workspaces, both agent
	// mode, no VCS, no tags. Verifies basic round-trip from go-tfe
	// JSON:API to ir.Workspace.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/organizations/acme/workspaces", func(w http.ResponseWriter, r *http.Request) {
		body := workspaceListBody(
			wsItem("ws-aaa", "api-prod", "remote", "1.12.0", "services/api", false, "", ""),
			wsItem("ws-bbb", "api-staging", "agent", "1.11.4", "services/api", true, "", ""),
		)
		_, _ = w.Write([]byte(body))
	})

	c, err := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	ws, conns, skipped, err := c.EmitWorkspaces(t.Context())
	if err != nil {
		t.Fatalf("EmitWorkspaces: %v", err)
	}
	if len(ws) != 2 {
		t.Fatalf("expected 2 workspaces, got %d: %+v", len(ws), ws)
	}
	if ws[0].SourceID != "ws-aaa" || ws[0].Name != "api-prod" {
		t.Errorf("workspace 0: %+v", ws[0])
	}
	// TFE "remote" → Terrapod "agent"
	if ws[0].ExecutionMode != "agent" {
		t.Errorf("remote→agent translation: got %q", ws[0].ExecutionMode)
	}
	// TFE's boolean becomes a mode. Both endpoints are asserted: mapping
	// only the true case would let false silently produce "" and inherit
	// whatever the writer or server happens to default to.
	if ws[1].ExecutionMode != "agent" || ws[1].AutoApplyMode != "always" {
		t.Errorf("agent + auto-apply: %+v", ws[1])
	}
	if ws[0].AutoApplyMode != "never" {
		t.Errorf("auto-apply false should map to never, got %q", ws[0].AutoApplyMode)
	}
	if ws[0].TerraformVersion != "1.12.0" || ws[1].TerraformVersion != "1.11.4" {
		t.Errorf("terraform versions: %q %q", ws[0].TerraformVersion, ws[1].TerraformVersion)
	}
	if ws[0].WorkingDirectory != "services/api" {
		t.Errorf("working directory: %q", ws[0].WorkingDirectory)
	}
	// No VCS in any workspace → no connections.
	if len(conns) != 0 {
		t.Errorf("unexpected VCS connections: %+v", conns)
	}
	if len(skipped) != 0 {
		t.Errorf("unexpected skipped: %+v", skipped)
	}
}

func TestEmitWorkspaces_VCSConnection_DedupedAcrossWorkspaces(t *testing.T) {
	// Two workspaces sharing the same OAuth token → one VCS
	// connection record. The migration tool creates the connection
	// once on Terrapod and attaches both workspaces to it.
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/organizations/acme/workspaces", func(w http.ResponseWriter, r *http.Request) {
		body := workspaceListBody(
			wsItem("ws-aaa", "api", "remote", "1.12.0", "", false,
				vcsRepoAttr("https://github.com/acme/infra", "ot-shared", "main"), ""),
			wsItem("ws-bbb", "web", "remote", "1.12.0", "", false,
				vcsRepoAttr("https://github.com/acme/infra", "ot-shared", "release"), ""),
		)
		_, _ = w.Write([]byte(body))
	})

	c, err := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	ws, conns, _, err := c.EmitWorkspaces(t.Context())
	if err != nil {
		t.Fatalf("EmitWorkspaces: %v", err)
	}
	if len(conns) != 1 {
		t.Fatalf("expected 1 deduped VCS connection, got %d: %+v", len(conns), conns)
	}
	if conns[0].SourceID != "ot-shared" || conns[0].Provider != "github" {
		t.Errorf("connection record: %+v", conns[0])
	}
	if ws[0].VCSConnectionRef != "ot-shared" || ws[1].VCSConnectionRef != "ot-shared" {
		t.Errorf("workspaces should reference same connection: %q vs %q", ws[0].VCSConnectionRef, ws[1].VCSConnectionRef)
	}
	if ws[0].VCSBranch != "main" || ws[1].VCSBranch != "release" {
		t.Errorf("per-workspace branch: %q vs %q", ws[0].VCSBranch, ws[1].VCSBranch)
	}
}

func TestEmitWorkspaces_UnrecognisedExecutionModeSkipped(t *testing.T) {
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/organizations/acme/workspaces", func(w http.ResponseWriter, r *http.Request) {
		body := workspaceListBody(
			wsItem("ws-aaa", "api", "remote", "1.12.0", "", false, "", ""),
			wsItem("ws-bbb", "weird", "experimental", "1.12.0", "", false, "", ""),
		)
		_, _ = w.Write([]byte(body))
	})

	c, err := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	ws, _, skipped, err := c.EmitWorkspaces(t.Context())
	if err != nil {
		t.Fatalf("EmitWorkspaces: %v", err)
	}
	if len(ws) != 1 || ws[0].Name != "api" {
		t.Errorf("only the recognised one should make it through: %+v", ws)
	}
	if len(skipped) != 1 || skipped[0].Kind != "tfe-workspace" || !strings.Contains(skipped[0].Reason, "experimental") {
		t.Errorf("expected experimental SkippedItem, got %+v", skipped)
	}
}

func TestTranslateExecutionMode(t *testing.T) {
	cases := []struct{ tfe, want string }{
		{"agent", "agent"},
		{"remote", "agent"}, // the headline rule
		{"local", "local"},
		{"", ""},             // empty mode → unrecognised
		{"experimental", ""}, // future-TFE mode we don't know
	}
	for _, c := range cases {
		if got := translateExecutionMode(c.tfe); got != c.want {
			t.Errorf("translateExecutionMode(%q) = %q, want %q", c.tfe, got, c.want)
		}
	}
}

func TestTranslateTags(t *testing.T) {
	got := translateTags([]string{"env:prod", "team:platform", "production", ""})
	want := map[string]string{"env": "prod", "team": "platform", "production": ""}
	if len(got) != len(want) {
		t.Fatalf("translateTags: %+v", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("translateTags[%q] = %q, want %q", k, got[k], v)
		}
	}
}

func TestGuessProviderFromRepoURL(t *testing.T) {
	cases := []struct{ url, ident, want string }{
		{"https://github.com/acme/infra", "", "github"},
		{"https://gitlab.com/acme/infra", "", "gitlab"},
		{"https://gitlab.example.com/acme/infra", "", "gitlab"},
		{"", "acme/infra", "github"},
		{"https://bitbucket.org/acme/infra", "", "github"}, // unsupported provider — defaults sensibly
	}
	for _, c := range cases {
		if got := guessProviderFromRepoURL(c.url, c.ident); got != c.want {
			t.Errorf("guess(%q, %q) = %q, want %q", c.url, c.ident, got, c.want)
		}
	}
}

func TestDeriveServerURL(t *testing.T) {
	cases := []struct{ in, want string }{
		{"https://github.com/acme/infra", ""}, // common-case host → empty (provider default)
		{"https://gitlab.com/acme/infra", ""},
		{"https://gitlab.example.com/acme/infra", "https://gitlab.example.com"},
		{"https://ghe.example.com/acme/infra", "https://ghe.example.com"},
		{"", ""},
		{"not-a-url", ""},
	}
	for _, c := range cases {
		if got := deriveServerURL(c.in); got != c.want {
			t.Errorf("deriveServerURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestCanonicaliseVCSRepoURL_NilSafe(t *testing.T) {
	// Hand-build a nil-ish pointer scenario; the function must not panic.
	if got := canonicaliseVCSRepoURL(nil); got != "" {
		t.Errorf("nil VCSRepo: %q", got)
	}
}

func TestShortID(t *testing.T) {
	cases := []struct{ in, want string }{
		{"ot-abcd1234efgh", "abcd1234"},
		{"abcd1234efgh", "abcd1234"},
		{"short", "short"},
		{"", ""},
	}
	for _, c := range cases {
		if got := shortID(c.in); got != c.want {
			t.Errorf("shortID(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// A TFE tag spelled `access:everyone` is not a label on the Terrapod side — it
// is the grant the built-in `everyone` role keys on, giving every authenticated
// user read on that workspace. A migration applies tags in bulk, so one such
// tag in the source silently opens every workspace carrying it.
func TestAnAccessEveryoneTagIsWithheldFromTheLabels(t *testing.T) {
	labels := translateTags([]string{"env:prod", "access:everyone"})
	withholdAccessGrant(labels)

	if _, present := labels["access"]; present {
		t.Fatalf("the access grant survived into the labels: %+v", labels)
	}
	if labels["terrapod-migration/withheld-access-tag"] != "everyone" {
		t.Errorf("the withheld value was not recorded: %+v", labels)
	}
	if labels["env"] != "prod" {
		t.Errorf("an unrelated tag was lost: %+v", labels)
	}
}

// The server matches the key AND the value exactly, so any other `access` value
// grants nothing and is an ordinary descriptive label. Withholding it would
// lose a legitimate label for no security benefit.
func TestAnAccessTagWithAnotherValueIsLeftAlone(t *testing.T) {
	for _, value := range []string{"production", "restricted", "team", ""} {
		labels := translateTags([]string{"access:" + value})
		withholdAccessGrant(labels)
		if got, present := labels["access"]; !present || got != value {
			t.Errorf("access:%q was withheld but grants nothing: %+v", value, labels)
		}
		if _, recorded := labels["terrapod-migration/withheld-access-tag"]; recorded {
			t.Errorf("access:%q was recorded as withheld: %+v", value, labels)
		}
	}
}

// The newer HCP key/value tag bindings write into the same map by a different
// path, so the filter has to run over the assembled result, not inside the
// flat-string reader.
func TestTheGrantIsWithheldFromTheKeyValueTagShapeToo(t *testing.T) {
	labels := translateTags(nil)
	labels["access"] = "everyone" // as a TagBinding would set it
	withholdAccessGrant(labels)

	if _, present := labels["access"]; present {
		t.Fatalf("a tag-binding grant survived: %+v", labels)
	}
}

// The helper tests above call withholdAccessGrant directly, which proves it is
// correct and nothing about whether EmitWorkspaces reaches it — removing the
// call site leaves them all green. This drives the real path end to end,
// through go-tfe's deserialiser, so the guard cannot be quietly disconnected.
//
// It is also what the file's older note deferred: TFE's tag relation needs the
// tag in the response's `included` array AND referenced from the workspace's
// `relationships.tags.data`, and go-tfe is strict about both halves.
func TestEmitWorkspaces_WithholdsTheAccessGrantEndToEnd(t *testing.T) {
	f := newFakeTFE(t)
	f.orgRead("acme", http.StatusOK, minimalOrgBody)
	f.orgMembershipsList("acme", http.StatusOK)
	f.mux.HandleFunc("/api/v2/organizations/acme/workspaces", func(w http.ResponseWriter, r *http.Request) {
		tagRel := `,
    "relationships": {
      "tags": {"data": [{"id": "tag-1", "type": "tags"}, {"id": "tag-2", "type": "tags"}]}
    }`
		body := `{
  "data": [` + wsItem("ws-aaa", "api-prod", "agent", "1.12.0", "", false, "", tagRel) + `],
  "included": [
    {"id": "tag-1", "type": "tags", "attributes": {"name": "access:everyone"}},
    {"id": "tag-2", "type": "tags", "attributes": {"name": "env:prod"}}
  ],
  "meta": {"pagination": {"current-page": 1, "total-pages": 1, "total-count": 1}}
}`
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(body))
	})

	c, err := NewClient(t.Context(), Config{Address: f.server.URL, Token: "t", OrgName: "acme"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	ws, _, _, err := c.EmitWorkspaces(t.Context())
	if err != nil {
		t.Fatalf("EmitWorkspaces: %v", err)
	}
	if len(ws) != 1 {
		t.Fatalf("expected 1 workspace, got %d: %+v", len(ws), ws)
	}

	// The fixture has to actually carry the tags, or this test would pass on
	// a workspace with no labels at all and prove nothing.
	if ws[0].Labels["env"] != "prod" {
		t.Fatalf("the tag fixture did not deserialise; labels are %+v", ws[0].Labels)
	}
	if _, present := ws[0].Labels["access"]; present {
		t.Errorf("the fleet-wide read grant was migrated onto the workspace: %+v", ws[0].Labels)
	}
	if ws[0].Labels["terrapod-migration/withheld-access-tag"] != "everyone" {
		t.Errorf("the withheld grant was not recorded: %+v", ws[0].Labels)
	}
}
