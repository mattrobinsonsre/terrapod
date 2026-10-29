package mcpserver

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The service-catalog tools (#535, #2019) driven end to end: a real MCP client
// against a real server, backed by a fake Terrapod. The catalogue golden pins
// registration and input schemas, and TestEveryToolIsAnnotated pins the
// annotations; these pin that each tool reaches the right endpoint and sends
// the right wire shape, which is what a wrapper this thin can still get wrong.
//
// The load-bearing group is the PATCH shape. An item's four Ansible attributes
// carry a meaning in their EMPTY value -- clearing `ansible-directory` is how
// the half is withdrawn, and an empty `default-limit` widens a triggered
// configure to the whole inventory -- so "the caller omitted it" and "the
// caller cleared it" have to reach the server as different requests. Nothing
// about a tool's schema says which it does; only a request does.

// catalogToolCaller mirrors toolCaller but registers the catalog tools.
//
// Its own helper rather than extending the shared one, for the reason
// inventoryToolCaller gives: the shared one is a fixture for a different group
// and widening it would make every one of those tests depend on this
// registration.
func catalogToolCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}

	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerCatalog(srv, c)

	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, st, nil); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil).
		Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	return sess
}

// capturedRequest is what the fake Terrapod saw.
type capturedRequest struct {
	Method string
	Path   string
	Query  string
	Attrs  map[string]any
}

// catalogCall drives one tool against a fake Terrapod that answers `body` and
// records what it was asked for.
func catalogCall(
	t *testing.T, status int, body, tool string, args map[string]any,
) (*mcp.CallToolResult, *capturedRequest) {
	t.Helper()
	got := &capturedRequest{}
	sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		got.Method, got.Path, got.Query = r.Method, r.URL.Path, r.URL.RawQuery
		if raw, err := io.ReadAll(r.Body); err == nil && len(raw) > 0 {
			var doc struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			if json.Unmarshal(raw, &doc) == nil {
				got.Attrs = doc.Data.Attributes
			}
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		t.Fatalf("CallTool %s: %.500v", tool, err)
	}
	return res, got
}

const (
	itemBody = `{"data":{"id":"11111111-1111-1111-1111-111111111111","type":"catalog-items",` +
		`"attributes":{"name":"vpc","configure-enabled":true,"ansible-directory":"ansible",` +
		`"default-playbook":"site.yml","default-limit":"canary","created-at":"2026-01-01T00:00:00Z"}}}`
	templateBody = `{"data":{"id":"22222222-2222-2222-2222-222222222222","type":"provider-templates",` +
		`"attributes":{"name":"aws-prod","provider-type":"aws","body":"region = var.region","parameters":[]}}}`
	instanceBody = `{"data":{"id":"33333333-3333-3333-3333-333333333333","type":"catalog-instances",` +
		`"attributes":{"name":"team-a-vpc","catalog-item-id":"11111111-1111-1111-1111-111111111111"}}}`
	runRefBody = `{"data":{"id":"run-1","type":"runs","attributes":{"status":"planned","is-destroy":false}}}`
)

// ── Each tool reaches the endpoint its name claims ──────────────────────

func TestCatalogToolsReachTheRightEndpoint(t *testing.T) {
	cases := []struct {
		tool, method, path, body string
		args                     map[string]any
	}{
		{"terrapod_catalog_item_list", "GET", "/api/terrapod/v1/catalog-items",
			`{"data":[]}`, map[string]any{}},
		{"terrapod_catalog_item_get", "GET", "/api/terrapod/v1/catalog-items/ci-1",
			itemBody, map[string]any{"catalog_item_id": "ci-1"}},
		{"terrapod_catalog_item_form", "GET", "/api/terrapod/v1/catalog-items/ci-1/form",
			`{"data":{"id":"ci-1","type":"catalog-item-forms","attributes":{"resolved-version":"1.0.0","fields":[]}}}`,
			map[string]any{"catalog_item_id": "ci-1"}},
		{"terrapod_catalog_item_create", "POST", "/api/terrapod/v1/catalog-items",
			itemBody, map[string]any{"name": "vpc", "module_id": "mod-1"}},
		{"terrapod_catalog_item_update", "PATCH", "/api/terrapod/v1/catalog-items/ci-1",
			itemBody, map[string]any{"catalog_item_id": "ci-1", "enabled": false}},
		{"terrapod_catalog_item_delete", "DELETE", "/api/terrapod/v1/catalog-items/ci-1",
			``, map[string]any{"catalog_item_id": "ci-1"}},

		{"terrapod_provider_template_list", "GET", "/api/terrapod/v1/provider-templates",
			`{"data":[]}`, map[string]any{}},
		{"terrapod_provider_template_get", "GET", "/api/terrapod/v1/provider-templates/pt-1",
			templateBody, map[string]any{"provider_template_id": "pt-1"}},
		{"terrapod_provider_template_create", "POST", "/api/terrapod/v1/provider-templates",
			templateBody, map[string]any{"name": "aws-prod", "provider_type": "aws", "body": "region = x"}},
		{"terrapod_provider_template_update", "PATCH", "/api/terrapod/v1/provider-templates/pt-1",
			templateBody, map[string]any{"provider_template_id": "pt-1", "body": "region = y"}},
		{"terrapod_provider_template_delete", "DELETE", "/api/terrapod/v1/provider-templates/pt-1",
			``, map[string]any{"provider_template_id": "pt-1"}},

		{"terrapod_catalog_provision", "POST", "/api/terrapod/v1/catalog-items/ci-1/provision",
			instanceBody, map[string]any{
				"catalog_item_id": "ci-1", "name": "team-a-vpc", "agent_pool_id": "apool-1",
			}},
		{"terrapod_catalog_instance_list", "GET", "/api/terrapod/v1/catalog-items/ci-1/instances",
			`{"data":[]}`, map[string]any{"catalog_item_id": "ci-1"}},
		{"terrapod_catalog_instance_get", "GET", "/api/terrapod/v1/catalog-instances/ws-3",
			instanceBody, map[string]any{"workspace_id": "ws-3"}},
		{"terrapod_catalog_instance_reconfigure", "PATCH", "/api/terrapod/v1/catalog-instances/ws-3",
			runRefBody, map[string]any{"workspace_id": "ws-3"}},
		{"terrapod_catalog_instance_destroy", "POST", "/api/terrapod/v1/catalog-instances/ws-3/destroy",
			runRefBody, map[string]any{"workspace_id": "ws-3"}},
		{"terrapod_catalog_instance_confirm", "POST", "/api/terrapod/v1/catalog-instances/ws-3/confirm",
			runRefBody, map[string]any{"workspace_id": "ws-3"}},
		{"terrapod_catalog_instance_discard", "POST", "/api/terrapod/v1/catalog-instances/ws-3/discard",
			runRefBody, map[string]any{"workspace_id": "ws-3"}},
		{"terrapod_catalog_instance_orphan", "DELETE", "/api/terrapod/v1/catalog-instances/ws-3",
			``, map[string]any{"workspace_id": "ws-3"}},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			status := http.StatusOK
			if tc.body == "" {
				status = http.StatusNoContent
			}
			res, got := catalogCall(t, status, tc.body, tc.tool, tc.args)
			if res.IsError {
				t.Fatalf("tool error: %s", resultText(t, res))
			}
			if got.Method != tc.method || got.Path != tc.path {
				t.Errorf("called %s %s, want %s %s", got.Method, got.Path, tc.method, tc.path)
			}
		})
	}
}

// The orphan is the ONE call whose query string is load-bearing: the API
// refuses to abandon infrastructure without it and answers 409 instead, so a
// tool that dropped it would report a refusal rather than doing the thing its
// name says.
func TestOrphanSendsTheExplicitOrphanFlag(t *testing.T) {
	res, got := catalogCall(t, http.StatusNoContent, "", "terrapod_catalog_instance_orphan",
		map[string]any{"workspace_id": "ws-3"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if !strings.Contains(got.Query, "orphan=true") {
		t.Errorf("query = %q, want orphan=true: without it the API refuses (409)", got.Query)
	}
}

// ── The Ansible half reaches the wire, kebab-cased ──────────────────────

func TestItemCreateSendsTheAnsibleHalfAsKebabCase(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_create",
		map[string]any{
			"name": "vpc", "module_id": "mod-1",
			"configure_enabled": true,
			"ansible_directory": "ansible",
			"default_playbook":  "site.yml",
			"default_limit":     "canary",
		})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	for key, want := range map[string]any{
		"configure-enabled": true,
		"ansible-directory": "ansible",
		"default-playbook":  "site.yml",
		"default-limit":     "canary",
	} {
		if got.Attrs[key] != want {
			t.Errorf("attribute %q = %#v, want %#v (the wire names are kebab-case)", key, got.Attrs[key], want)
		}
	}
}

// `configure_enabled` is a *bool so that false is SENT rather than dropped.
// The item's own default is false, so this makes no difference today — but an
// omitted key and an explicit false are different requests, and a plain bool
// would make "I did not say" indistinguishable from "turn it off".
func TestItemCreateSendsAnExplicitConfigureEnabledFalse(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_create",
		map[string]any{"name": "vpc", "module_id": "mod-1", "configure_enabled": false})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	v, ok := got.Attrs["configure-enabled"]
	if !ok {
		t.Fatal("configure-enabled was dropped; an explicit false must reach the server")
	}
	if v != false {
		t.Errorf("configure-enabled = %#v, want false", v)
	}
}

func TestItemCreateOmitsWhatTheCallerDidNotSupply(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_create",
		map[string]any{"name": "vpc", "module_id": "mod-1"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	for _, key := range []string{
		"configure-enabled", "ansible-directory", "default-playbook", "default-limit",
		"enabled", "labels", "variable-options", "allowed-agent-pool-ids",
	} {
		if _, ok := got.Attrs[key]; ok {
			t.Errorf("attribute %q was sent although the caller left it alone, so the "+
				"server's own default cannot stand", key)
		}
	}
}

// ── The PATCH shape: omitted and cleared are different requests ─────────

// Withdrawing the Ansible half means clearing the directory AND turning the
// switch off in the same call, because the server refuses the switch on with
// no directory. That is only expressible if an empty string reaches the wire.
func TestItemUpdateSendsAnEmptyAnsibleDirectoryToClearIt(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_update",
		map[string]any{
			"catalog_item_id":   "ci-1",
			"configure_enabled": false,
			"ansible_directory": "",
			"default_playbook":  "",
			"default_limit":     "",
		})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	for _, key := range []string{"ansible-directory", "default-playbook", "default-limit"} {
		v, ok := got.Attrs[key]
		if !ok {
			t.Errorf("%q was dropped; an empty value is a MEANING here (clear it) and has "+
				"to be distinguishable from leaving it alone", key)
			continue
		}
		if v != "" {
			t.Errorf("%q = %#v, want the empty string", key, v)
		}
	}
	if got.Attrs["configure-enabled"] != false {
		t.Errorf("configure-enabled = %#v, want false", got.Attrs["configure-enabled"])
	}
}

func TestItemUpdateOmitsTheFieldsTheCallerLeftAlone(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_update",
		map[string]any{"catalog_item_id": "ci-1", "default_limit": "canary"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.Attrs["default-limit"] != "canary" {
		t.Errorf("default-limit = %#v", got.Attrs["default-limit"])
	}
	// Re-validating a value the caller never mentioned is how a stored row
	// becomes uneditable (#316), so the other keys must not be restated.
	for _, key := range []string{"ansible-directory", "default-playbook", "configure-enabled", "name"} {
		if _, ok := got.Attrs[key]; ok {
			t.Errorf("attribute %q was sent although the caller left it alone", key)
		}
	}
}

func TestItemUpdateNeedsAtLeastOneField(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_update",
		map[string]any{"catalog_item_id": "ci-1"})
	if !res.IsError {
		t.Error("an update with nothing to change should be refused locally")
	}
	if got.Path != "" {
		t.Errorf("it reached the API at %s; an empty PATCH is not worth a request", got.Path)
	}
}

// An empty list means "no pool is permitted" and null means "any pool is", so
// the two cannot share a field. The flag is the only way to say the second.
func TestItemUpdateLiftsThePoolRestrictionWithNull(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_update",
		map[string]any{"catalog_item_id": "ci-1", "allow_any_agent_pool": true})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	v, ok := got.Attrs["allowed-agent-pool-ids"]
	if !ok {
		t.Fatal("allowed-agent-pool-ids was not sent, so the restriction is not lifted")
	}
	if v != nil {
		t.Errorf("allowed-agent-pool-ids = %#v, want null", v)
	}
}

func TestItemUpdateRestrictsThePoolsWithAList(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_update",
		map[string]any{"catalog_item_id": "ci-1", "allowed_agent_pool_ids": []any{"apool-1"}})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	list, ok := got.Attrs["allowed-agent-pool-ids"].([]any)
	if !ok || len(list) != 1 || list[0] != "apool-1" {
		t.Errorf("allowed-agent-pool-ids = %#v", got.Attrs["allowed-agent-pool-ids"])
	}
}

func TestItemUpdateRefusesBothPoolAnswersAtOnce(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, itemBody, "terrapod_catalog_item_update",
		map[string]any{
			"catalog_item_id":        "ci-1",
			"allowed_agent_pool_ids": []any{"apool-1"},
			"allow_any_agent_pool":   true,
		})
	if !res.IsError {
		t.Error("restricting and unrestricting in one call should be refused")
	}
	if got.Path != "" {
		t.Errorf("it reached the API at %s; the server would silently take one of them", got.Path)
	}
}

// ── Provision and reconfigure ───────────────────────────────────────────

func TestProvisionSendsTheInputValuesAndAutoApply(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, instanceBody, "terrapod_catalog_provision",
		map[string]any{
			"catalog_item_id": "ci-1",
			"name":            "team-a-vpc",
			"agent_pool_id":   "apool-1",
			"input_values":    map[string]any{"cidr": "10.0.0.0/16"},
			"version_pin":     "1.2.0",
		})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.Attrs["name"] != "team-a-vpc" || got.Attrs["agent-pool-id"] != "apool-1" {
		t.Errorf("attrs = %#v", got.Attrs)
	}
	if got.Attrs["version-pin"] != "1.2.0" {
		t.Errorf("version-pin = %#v", got.Attrs["version-pin"])
	}
	iv, ok := got.Attrs["input-values"].(map[string]any)
	if !ok || iv["cidr"] != "10.0.0.0/16" {
		t.Errorf("input-values = %#v (the wire key is kebab-case)", got.Attrs["input-values"])
	}
	// auto-apply is always sent, because its default decides whether real
	// infrastructure is created without a human: leaving it to the server's
	// own default would make the safe setting implicit rather than stated.
	if got.Attrs["auto-apply"] != false {
		t.Errorf("auto-apply = %#v, want an explicit false", got.Attrs["auto-apply"])
	}
}

func TestProvisionNeedsAnAgentPool(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, instanceBody, "terrapod_catalog_provision",
		map[string]any{"catalog_item_id": "ci-1", "name": "team-a-vpc"})
	if !res.IsError {
		t.Error("a provision with no agent pool should be refused locally")
	}
	if got.Path != "" {
		t.Errorf("it reached the API at %s", got.Path)
	}
	if !strings.Contains(resultText(t, res), "agent_pool_id") {
		t.Errorf("the refusal should name the missing field: %s", resultText(t, res))
	}
}

// Omitting `input_values` keeps the stored set; sending one replaces it. The
// two have to be different requests, because a partial set reaches the server
// as a replacement and a required field left out is then a 422.
func TestReconfigureOmitsInputValuesToKeepTheStoredSet(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, runRefBody, "terrapod_catalog_instance_reconfigure",
		map[string]any{"workspace_id": "ws-3", "version_pin": "2.0.0"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if _, ok := got.Attrs["input-values"]; ok {
		t.Error("input-values was sent although the caller omitted it, so the stored " +
			"inputs would be replaced by whatever this call happened to carry")
	}
	if got.Attrs["version-pin"] != "2.0.0" {
		t.Errorf("version-pin = %#v", got.Attrs["version-pin"])
	}
}

func TestReconfigureSendsAnEmptyVersionPinToFloat(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, runRefBody, "terrapod_catalog_instance_reconfigure",
		map[string]any{"workspace_id": "ws-3", "version_pin": ""})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	v, ok := got.Attrs["version-pin"]
	if !ok {
		t.Fatal("version-pin was dropped; an empty pin means FLOAT and has to be " +
			"distinguishable from keeping the current one")
	}
	if v != "" {
		t.Errorf("version-pin = %#v, want the empty string", v)
	}
}

func TestReconfigureReplacesTheInputValuesItIsGiven(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, runRefBody, "terrapod_catalog_instance_reconfigure",
		map[string]any{
			"workspace_id": "ws-3",
			"input_values": map[string]any{"cidr": "10.1.0.0/16"},
		})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	iv, ok := got.Attrs["input-values"].(map[string]any)
	if !ok || iv["cidr"] != "10.1.0.0/16" {
		t.Errorf("input-values = %#v", got.Attrs["input-values"])
	}
	if _, ok := got.Attrs["version-pin"]; ok {
		t.Error("version-pin was sent although the caller omitted it, so the instance's " +
			"own pin would be overwritten by an inputs-only reconfigure")
	}
}

func TestDestroySendsAnExplicitAutoApply(t *testing.T) {
	res, got := catalogCall(t, http.StatusOK, runRefBody, "terrapod_catalog_instance_destroy",
		map[string]any{"workspace_id": "ws-3"})
	if res.IsError {
		t.Fatalf("tool error: %s", resultText(t, res))
	}
	if got.Attrs["auto-apply"] != false {
		t.Errorf("auto-apply = %#v, want an explicit false so a destroy plans and waits",
			got.Attrs["auto-apply"])
	}
}

// ── Refusals and missing arguments ──────────────────────────────────────

func TestCatalogToolsPassARefusalThrough(t *testing.T) {
	res, _ := catalogCall(t, http.StatusForbidden,
		`{"errors":[{"status":"403","detail":"Requires catalog 'use' on this item"}],`+
			`"detail":"Requires catalog 'use' on this item"}`,
		"terrapod_catalog_provision", map[string]any{
			"catalog_item_id": "ci-1", "name": "team-a-vpc", "agent_pool_id": "apool-1",
		})
	if !res.IsError || !strings.Contains(resultText(t, res), "permission denied") {
		t.Errorf("want a permission error, got %s", resultText(t, res))
	}
}

func TestCatalogToolsRefuseAMissingIDWithoutCallingTheAPI(t *testing.T) {
	cases := []struct {
		tool, arg string
	}{
		{"terrapod_catalog_item_get", "catalog_item_id"},
		{"terrapod_catalog_item_form", "catalog_item_id"},
		{"terrapod_catalog_item_delete", "catalog_item_id"},
		{"terrapod_catalog_item_update", "catalog_item_id"},
		{"terrapod_provider_template_get", "provider_template_id"},
		{"terrapod_provider_template_update", "provider_template_id"},
		{"terrapod_provider_template_delete", "provider_template_id"},
		{"terrapod_catalog_instance_list", "catalog_item_id"},
		{"terrapod_catalog_instance_get", "workspace_id"},
		{"terrapod_catalog_instance_reconfigure", "workspace_id"},
		{"terrapod_catalog_instance_destroy", "workspace_id"},
		{"terrapod_catalog_instance_confirm", "workspace_id"},
		{"terrapod_catalog_instance_discard", "workspace_id"},
		{"terrapod_catalog_instance_orphan", "workspace_id"},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			res, got := catalogCall(t, http.StatusOK, itemBody, tc.tool,
				map[string]any{tc.arg: ""})
			if !res.IsError {
				t.Errorf("an empty %s should be refused", tc.arg)
			}
			if got.Path != "" {
				t.Errorf("an empty %s reached the API at %s", tc.arg, got.Path)
			}
		})
	}
}

// ── The descriptions, which the catalogue golden does not freeze ────────

// A tool-level Description is pinned by NOTHING: tool_catalogue.json freezes
// `name` and `input_schema` (a FIELD description lives inside the schema and so
// is frozen; the tool's own is not). Four descriptions in this repository have
// gone false that way, and a false one is worse than an absent one because an
// agent cannot sanity-check it. These pin the claims an agent would act on.

func TestProvisionPointsAtTheCatalogConfirmRatherThanTheRunApply(t *testing.T) {
	// The workspace clamp caps every non-platform-admin grant on an instance
	// at read, so an agent told to confirm with terrapod_run_apply gets a 403
	// and reads it as a permission problem rather than the wrong door.
	desc := liveToolsByName(t)["terrapod_catalog_provision"]
	if desc == "" {
		t.Fatal("terrapod_catalog_provision is not registered")
	}
	if !strings.Contains(desc, "terrapod_catalog_instance_confirm") {
		t.Error("the provision tool never names terrapod_catalog_instance_confirm, so an " +
			"agent has no way to know which door confirms its run:\n" + desc)
	}
	if !strings.Contains(desc, "clamp") {
		t.Error("the provision tool does not say the instance workspace is clamped, which " +
			"is WHY the workspace run API refuses the provisioner:\n" + desc)
	}
}

func TestOrphanSaysTheInfrastructureKeepsRunning(t *testing.T) {
	// The one tool whose name does not carry its consequence: "orphan" reads
	// as a tidy-up, and it leaves live, billed, untracked resources behind.
	desc := liveToolsByName(t)["terrapod_catalog_instance_orphan"]
	if desc == "" {
		t.Fatal("terrapod_catalog_instance_orphan is not registered")
	}
	for _, want := range []string{"keep running", "terrapod_catalog_instance_destroy"} {
		if !strings.Contains(desc, want) {
			t.Errorf("the orphan tool never says %q, so an agent may offer it as a "+
				"teardown:\n%s", want, desc)
		}
	}
}

// The #1966 correction, carried to the catalog item: one field answering two
// questions is the defect, so neither description may imply the path decides
// whether there is an operation.
func TestTheItemAnsibleFieldsSayWhetherAndWhereSeparately(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_catalog_item_create", "terrapod_catalog_item_update"} {
		t.Run(tool, func(t *testing.T) {
			raw, ok := schemas[tool]
			if !ok {
				t.Fatalf("%s is not registered", tool)
			}
			var doc struct {
				Properties map[string]struct {
					Description string `json:"description"`
				} `json:"properties"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Fatalf("decode input schema: %v", err)
			}
			enabled := doc.Properties["configure_enabled"].Description
			dir := doc.Properties["ansible_directory"].Description
			if enabled == "" || dir == "" {
				t.Fatalf("%s is missing configure_enabled or ansible_directory", tool)
			}
			// Each must name the other, or an agent setting one has no way to
			// learn it also has to consider the other.
			if !strings.Contains(enabled, "ansible_directory") {
				t.Error("configure_enabled never mentions ansible_directory:\n" + enabled)
			}
			if !strings.Contains(dir, "config_directory") {
				t.Error("ansible_directory never says it becomes the workspace's " +
					"config_directory:\n" + dir)
			}
			// The cross-field 422 is the refusal an agent will actually meet.
			if !strings.Contains(enabled, "422") {
				t.Error("configure_enabled does not warn that true with no directory is " +
					"refused 422:\n" + enabled)
			}
			// The directory is COPIED, not referenced -- an agent that believes
			// otherwise will point a workspace at .terraform/modules.
			if !strings.Contains(dir, "COPIED") {
				t.Error("ansible_directory does not say the subtree is copied into the " +
					"configuration version:\n" + dir)
			}
		})
	}
}

// Stored byte for byte, refused rather than normalised: a value the server
// trimmed would fail a Terraform apply with "Provider produced inconsistent
// result after apply", so an agent must not expect its input to be cleaned up.
func TestTheItemPathFieldsSayTheyAreRefusedRatherThanNormalised(t *testing.T) {
	schemas := liveInputSchemasByName(t)
	for _, tool := range []string{"terrapod_catalog_item_create", "terrapod_catalog_item_update"} {
		raw := schemas[tool]
		var doc struct {
			Properties map[string]struct {
				Description string `json:"description"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("%s: decode input schema: %v", tool, err)
		}
		for _, field := range []string{"ansible_directory", "default_playbook"} {
			desc := doc.Properties[field].Description
			if desc == "" {
				t.Fatalf("%s has no %s", tool, field)
			}
			// Case-insensitively: both descriptions say this, one of them in
			// capitals for emphasis, and the claim is the content rather than
			// the case.
			for _, want := range []string{"refused", "255"} {
				if !strings.Contains(strings.ToLower(desc), want) {
					t.Errorf("%s.%s never says %q, so an agent may expect a stray slash "+
						"or a long path to be cleaned up:\n%s", tool, field, want, desc)
				}
			}
		}
		limit := doc.Properties["default_limit"].Description
		// The one that is dangerous in the quiet direction: empty is not an
		// unset field, it is every host in the inventory.
		if !strings.Contains(limit, "WHOLE INVENTORY") {
			t.Errorf("%s.default_limit does not say an empty limit means the whole "+
				"inventory:\n%s", tool, limit)
		}
	}
}

// Every catalog tool has to say which catalog capability it needs, because the
// axis is its own: catalog `read`/`use`/`admin` plus platform admin, and a
// workspace grant answers none of them. Without it an agent reads a 403 as a
// workspace permission problem and offers the wrong fix.
func TestEveryCatalogToolNamesThePermissionItNeeds(t *testing.T) {
	descs := liveToolsByName(t)
	tools := []string{
		"terrapod_catalog_item_list", "terrapod_catalog_item_get", "terrapod_catalog_item_form",
		"terrapod_catalog_item_create", "terrapod_catalog_item_update", "terrapod_catalog_item_delete",
		"terrapod_provider_template_list", "terrapod_provider_template_get",
		"terrapod_provider_template_create", "terrapod_provider_template_update",
		"terrapod_provider_template_delete",
		"terrapod_catalog_provision", "terrapod_catalog_instance_list",
		"terrapod_catalog_instance_get", "terrapod_catalog_instance_reconfigure",
		"terrapod_catalog_instance_destroy", "terrapod_catalog_instance_confirm",
		"terrapod_catalog_instance_discard", "terrapod_catalog_instance_orphan",
	}
	// A floor, so a renamed tool fails here rather than making this pass on a
	// shorter list than the catalog actually has.
	if len(tools) < 19 {
		t.Fatalf("the list itself has shrunk to %d", len(tools))
	}
	for _, name := range tools {
		desc, ok := descs[name]
		if !ok {
			t.Errorf("%s is not registered", name)
			continue
		}
		if !strings.Contains(desc, "catalog read") && !strings.Contains(desc, "catalog `use`") &&
			!strings.Contains(desc, "catalog `admin`") && !strings.Contains(desc, "Platform admin") &&
			!strings.Contains(desc, "Admin or audit") {
			t.Errorf("%s never says which permission it needs, so a 403 from it is "+
				"unexplainable:\n%s", name, desc)
		}
	}
}
