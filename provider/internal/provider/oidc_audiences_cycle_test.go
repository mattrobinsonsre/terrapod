package provider

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"sync"
	"testing"

	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// oidc_audiences (#1901) driven through the provider's real protocol server the
// way Terraform core drives it:
//
//	plan → apply → refresh → plan (must be empty)
//
// against a fake Terrapod that answers the MERGED view — the workspace's own
// override merged over a deployment-wide audience catalogue, which is what the
// real server returns.
//
// That merge is the whole difficulty. The attribute's read is WIDER than its
// write, so a provider that stored the response wholesale would (a) fail the
// apply outright, because the planned value is the configuration's own map and
// the applied value would be the wider merged one, and (b) having got past
// that, promote every inherited entry into a workspace override on the next
// apply. Neither can be seen by calling the read helper directly — they are
// properties of the plan/apply/refresh cycle — so this test drives the cycle.

var deploymentCatalogue = map[string][]string{
	// Supplied by the deployment, never by the configuration below. It must
	// reach the workspace (the server merges it) and must NOT reach state.
	"vault": {"https://vault.example.com"},
}

// fakeWorkspaceAPI serves one workspace and merges the deployment catalogue
// into every oidc-audiences it answers with, exactly as the server does.
type fakeWorkspaceAPI struct {
	mu    sync.Mutex
	attrs map[string]any
	// override is what the workspace itself holds, kept apart from the merged
	// view so the fake cannot accidentally make the two the same thing — which
	// is the one way this test could pass while the provider was wrong.
	override map[string][]string
	patches  int
}

func (f *fakeWorkspaceAPI) merged() map[string][]string {
	out := map[string][]string{}
	for k, v := range deploymentCatalogue {
		out[k] = v
	}
	for k, v := range f.override {
		out[k] = v
	}
	return out
}

func (f *fakeWorkspaceAPI) serve(t *testing.T) http.HandlerFunc {
	t.Helper()
	return func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		w.Header().Set("Content-Type", "application/vnd.api+json")

		decode := func() map[string]any {
			raw, _ := io.ReadAll(r.Body)
			var body struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Errorf("write body is not JSON: %v", err)
			}
			return body.Data.Attributes
		}
		// An absent `oidc-audiences` leaves the override alone; a present one
		// REPLACES it, including when it is empty. That asymmetry is the
		// contract the provider has to drive, so the fake has to honour it or
		// the opt-out case below proves nothing.
		store := func(attrs map[string]any) {
			for k, v := range attrs {
				f.attrs[k] = v
			}
			raw, present := attrs["oidc-audiences"]
			if !present {
				return
			}
			f.override = map[string][]string{}
			m, ok := raw.(map[string]any)
			if !ok {
				t.Errorf("oidc-audiences arrived as %T, want an object", raw)
				return
			}
			for key, list := range m {
				items, ok := list.([]any)
				if !ok {
					t.Errorf("oidc-audiences[%s] arrived as %T, want a list", key, list)
					continue
				}
				auds := make([]string, 0, len(items))
				for _, it := range items {
					auds = append(auds, it.(string))
				}
				f.override[key] = auds
			}
		}
		resource := func() map[string]any {
			attrs := map[string]any{}
			for k, v := range f.attrs {
				attrs[k] = v
			}
			attrs["oidc-audiences"] = f.merged()
			return map[string]any{"id": "ws-1", "type": "workspaces", "attributes": attrs}
		}

		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v2/organizations/default/workspaces":
			f.attrs = map[string]any{}
			store(decode())
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"data": resource()})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v2/workspaces/ws-1":
			_ = json.NewEncoder(w).Encode(map[string]any{"data": resource()})
		case r.Method == http.MethodPatch && r.URL.Path == "/api/v2/workspaces/ws-1":
			f.patches++
			store(decode())
			_ = json.NewEncoder(w).Encode(map[string]any{"data": resource()})
		case r.URL.Path == "/api/terrapod/v1/workspaces/ws-1/remote-state-consumers":
			_ = json.NewEncoder(w).Encode(map[string]any{"data": []any{}})
		default:
			// Includes the /.well-known version probe, whose failure is a
			// warning at most.
			http.Error(w, "not found", http.StatusNotFound)
		}
	}
}

// audienceMap builds the tftypes value for an oidc_audiences configuration.
func audienceMap(entries map[string][]string) tftypes.Value {
	listTyp := tftypes.List{ElementType: tftypes.String}
	mapTyp := tftypes.Map{ElementType: listTyp}
	vals := map[string]tftypes.Value{}
	for k, auds := range entries {
		elems := make([]tftypes.Value, 0, len(auds))
		for _, a := range auds {
			elems = append(elems, tftypes.NewValue(tftypes.String, a))
		}
		vals[k] = tftypes.NewValue(listTyp, elems)
	}
	return tftypes.NewValue(mapTyp, vals)
}

// stateAudiences reads oidc_audiences back out of a state value.
func stateAudiences(t *testing.T, v tftypes.Value) map[string][]string {
	t.Helper()
	var m map[string]tftypes.Value
	if err := v.As(&m); err != nil {
		t.Fatalf("state: %v", err)
	}
	raw := m["oidc_audiences"]
	if raw.IsNull() {
		return nil
	}
	var byKey map[string]tftypes.Value
	if err := raw.As(&byKey); err != nil {
		t.Fatalf("oidc_audiences: %v", err)
	}
	out := map[string][]string{}
	for k, lv := range byKey {
		var elems []tftypes.Value
		if err := lv.As(&elems); err != nil {
			t.Fatalf("oidc_audiences[%s]: %v", k, err)
		}
		auds := make([]string, 0, len(elems))
		for _, e := range elems {
			var s string
			if err := e.As(&s); err != nil {
				t.Fatalf("oidc_audiences[%s] element: %v", k, err)
			}
			auds = append(auds, s)
		}
		out[k] = auds
	}
	return out
}

func keysOf(m map[string][]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// newWorkspaceCycle stands up the fake and the harness and returns a config
// builder that sets `name` plus whatever oidc_audiences value is handed in.
func newWorkspaceCycle(t *testing.T) (*fakeWorkspaceAPI, *protoHarness, func(tftypes.Value) tftypes.Value) {
	t.Helper()
	fake := &fakeWorkspaceAPI{}
	api := httptest.NewServer(fake.serve(t))
	t.Cleanup(api.Close)
	h := newProtoHarness(t, api.URL, "terrapod_workspace")
	config := func(auds tftypes.Value) tftypes.Value {
		return h.config(map[string]tftypes.Value{
			"name":           tftypes.NewValue(tftypes.String, "my-workspace"),
			"oidc_audiences": auds,
		})
	}
	return fake, h, config
}

// The core case. The configuration owns `aws` and `aws.west`; the deployment
// supplies `vault`. After a full cycle, state must hold exactly the two keys
// the configuration declared — and the next plan must be empty, because a
// state carrying the inherited key would diff against the config for ever.
func TestOIDCAudiencesSelectiveReadLeavesInheritedKeysOutOfState(t *testing.T) {
	fake, h, config := newWorkspaceCycle(t)

	owned := map[string][]string{
		"aws": {"sts.example.com"},
		// An alias is part of the KEY. `aws` and `aws.west` are two independent
		// provider configurations that happen to share a prefix; anything that
		// split on the dot would merge them into one.
		"aws.west": {"sts.example.com"},
	}
	cfg := config(audienceMap(owned))
	none := tftypes.NewValue(h.typ, nil)

	// The protocol server does not enforce plan/apply consistency — Terraform
	// core does, and core is not in this harness — so the assertion below is
	// what stands in for core's "Provider produced inconsistent result after
	// apply", together with the empty-plan check at the end.
	applied := h.apply(none, cfg, h.plan(none, cfg))
	if got := keysOf(stateAudiences(t, applied)); len(got) != 2 || got[0] != "aws" || got[1] != "aws.west" {
		t.Fatalf("state after apply holds %v, want exactly [aws aws.west]", got)
	}

	// The override really did reach the server, and the server really is
	// answering something wider — otherwise this test would pass against a
	// provider that simply ignored the attribute.
	if got := keysOf(fake.override); len(got) != 2 {
		t.Fatalf("the server stored override keys %v, want the two the config set", got)
	}
	if _, ok := fake.merged()["vault"]; !ok {
		t.Fatal("the fake is not merging the deployment catalogue, so this test proves nothing")
	}

	refreshed := h.refresh(applied)
	after := stateAudiences(t, refreshed)
	if _, inherited := after["vault"]; inherited {
		t.Error("the inherited `vault` key was pulled into state; the next apply would " +
			"promote the deployment's own audience into a workspace override")
	}
	if got := keysOf(after); len(got) != 2 || got[0] != "aws" || got[1] != "aws.west" {
		t.Fatalf("state after refresh holds %v, want exactly [aws aws.west]", got)
	}

	// Empty plan: the proof that the selective read does not merely survive one
	// apply but converges.
	if pr := h.plan(refreshed, cfg); !h.value(pr.PlannedState).Equal(refreshed) {
		t.Fatalf("oidc_audiences drifts after a refresh: planned %v, state %v",
			stateAudiences(t, h.value(pr.PlannedState)), after)
	}
}

// Several audiences under one key mean "these are interchangeable for this
// target". They must survive in order and unaltered — some federation targets
// refuse a multi-valued `aud`, so a provider that reordered or collapsed them
// would change what the operator asked for.
func TestOIDCAudiencesSeveralPerTargetSurviveInOrder(t *testing.T) {
	_, h, config := newWorkspaceCycle(t)

	want := []string{"https://b.example.com", "https://a.example.com"}
	cfg := config(audienceMap(map[string][]string{"vault": want}))
	none := tftypes.NewValue(h.typ, nil)

	got := stateAudiences(t, h.refresh(h.apply(none, cfg, h.plan(none, cfg))))["vault"]
	if len(got) != len(want) {
		t.Fatalf("oidc_audiences[vault] = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("oidc_audiences[vault] = %v, want %v (entry %d differs)", got, want, i)
		}
	}
}

// Values are opaque strings the federation target itself named, so any
// normalisation — trimming, lower-casing — would make the plan disagree with
// its own apply. Deliberately ugly values that a well-meaning canonicalisation
// would "fix".
func TestOIDCAudiencesAreStoredByteForByte(t *testing.T) {
	_, h, config := newWorkspaceCycle(t)

	const ugly = "  STS.Example.com/Path  "
	cfg := config(audienceMap(map[string][]string{"aws": {ugly}}))
	none := tftypes.NewValue(h.typ, nil)

	got := stateAudiences(t, h.refresh(h.apply(none, cfg, h.plan(none, cfg))))["aws"]
	if len(got) != 1 || got[0] != ugly {
		t.Fatalf("oidc_audiences[aws] = %q, want %q verbatim", got, ugly)
	}
}

// An explicit empty map drops every override and falls the workspace back to
// the deployment catalogue alone. It is the dangerous direction: an operator
// who removes every override must not get a silent no-op that leaves the
// workspace minting against the old set.
func TestOIDCAudiencesAnEmptyMapClearsEveryOverride(t *testing.T) {
	fake, h, config := newWorkspaceCycle(t)

	set := config(audienceMap(map[string][]string{"aws": {"sts.example.com"}}))
	none := tftypes.NewValue(h.typ, nil)
	state := h.refresh(h.apply(none, set, h.plan(none, set)))
	if len(fake.override) != 1 {
		t.Fatalf("setup failed: the server holds override %v", fake.override)
	}

	cleared := config(audienceMap(map[string][]string{}))
	pr := h.plan(state, cleared)
	state = h.apply(state, cleared, pr)
	if len(fake.override) != 0 {
		t.Fatalf("the server still holds override %v after the config cleared it", fake.override)
	}
	// Empty, not null: a null state against an empty config would diff for ever.
	got := stateAudiences(t, h.refresh(state))
	if got == nil {
		t.Fatal("oidc_audiences read back null after being cleared, but the config declares {}")
	}
	if len(got) != 0 {
		t.Fatalf("oidc_audiences = %v after being cleared, want an empty map", got)
	}
}

// A configuration that never mentions the attribute leaves the deployment's
// catalogue alone and records nothing. Without the null-preserving branch the
// merged catalogue would land in state, and the next apply would send it back
// as this workspace's own override — which is the failure the selective read
// exists to prevent, arriving by the other door.
func TestOIDCAudiencesOmittedFromTheConfigAdoptsNothing(t *testing.T) {
	fake, h, config := newWorkspaceCycle(t)

	listTyp := tftypes.List{ElementType: tftypes.String}
	cfg := config(tftypes.NewValue(tftypes.Map{ElementType: listTyp}, nil))
	none := tftypes.NewValue(h.typ, nil)

	state := h.refresh(h.apply(none, cfg, h.plan(none, cfg)))
	if got := stateAudiences(t, state); got != nil {
		t.Errorf("oidc_audiences = %v, want null for a config that does not declare it", got)
	}
	if len(fake.override) != 0 {
		t.Errorf("the server holds override %v for a config that never set one", fake.override)
	}
	if pr := h.plan(state, cfg); !h.value(pr.PlannedState).Equal(state) {
		t.Fatal("a config that omits oidc_audiences drifts on every plan")
	}
	// And nothing was written back on a second apply-shaped round either.
	if fake.patches != 0 {
		t.Errorf("the provider sent %d PATCHes for an unmanaged attribute", fake.patches)
	}
}
