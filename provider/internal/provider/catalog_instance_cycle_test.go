package provider

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// A catalog instance driven through the provider's real protocol server, the
// way Terraform core drives it: plan → apply → refresh → plan (must be empty)
// → change an input → plan → apply → refresh → plan (empty).
//
// It exists because its absence is how two defects shipped (#2425 §1.1), and
// both are invisible to a test that calls the resource's helpers directly --
// which is all this resource had.
//
//  1. `version_pin`, `execution_backend` and `engine_version` are Optional +
//     Computed with no Default, so the DOCUMENTED usage -- omit all three and
//     take the deployment's default -- planned unknown, and `Create` persisted
//     the plan. UseStateForUnknown cannot rescue a create (there is no prior
//     state), so three unknowns reached the state and Terraform refused the
//     result: "the provider still indicated an unknown value", once per
//     attribute. The instance was provisioned server-side first, so every
//     attempt orphaned a workspace and tainted the resource, which made the
//     next apply destroy and recreate it and fail identically.
//
//  2. The input-values merge copied the server's map wholesale. The server
//     materialises the module's own variable DEFAULTS, so its map is a
//     SUPERSET of the configuration, and refresh introduced keys no config
//     has -- a permanent in-place update on every plan, for any module whose
//     variables have defaults.
//
// The second was only reachable once the first was fixed, which is the reason
// for the empty-plan assertions rather than a create-only test: an apply that
// completes and a resource that settles are different claims.

// fakeCatalogAPI serves one catalog instance. Its GET deliberately reports a
// `name` input the provision call never sent -- that is the module default the
// real server materialises, and a fake that echoed only what it was given
// could not see defect 2 at all.
type fakeCatalogAPI struct {
	mu                 sync.Mutex
	inputs             map[string]string
	provisioned        map[string]any
	provisions         int
	reconfigures       int
	destroys           int
	defaultedInputName string
}

func (f *fakeCatalogAPI) handler(t *testing.T) http.HandlerFunc {
	const instanceID = "01a1bb6b-34e3-74bf-80dc-be9570a753f5"
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

		// What a real provision response carries, measured against a live
		// instance: name, agent-pool-id, labels, catalog-update-block,
		// catalog-version-pin, engine-version and execution-backend -- and
		// NOT input-values or auto-apply. The version comes back RESOLVED
		// ("1.13" was answered "1.13.9"), which is why a configured
		// engine_version must not be overwritten from it.
		provisionBody := func() map[string]any {
			attrs := map[string]any{
				"name":                 f.provisioned["name"],
				"agent-pool-id":        f.provisioned["agent-pool-id"],
				"labels":               map[string]any{},
				"catalog-update-block": nil,
				"catalog-version-pin":  f.provisioned["version-pin"],
				"execution-backend":    "tofu",
				"engine-version":       "1.13",
			}
			if _, ok := f.provisioned["engine-version"]; ok {
				attrs["engine-version"] = "1.13.9"
			}
			return map[string]any{"data": map[string]any{
				"id": instanceID, "type": "catalog-instances", "attributes": attrs,
			}}
		}

		// The GET additionally reports input-values, including the module
		// default the configuration never set.
		readBody := func() map[string]any {
			body := provisionBody()
			inputs := map[string]any{}
			for k, v := range f.inputs {
				inputs[k] = v
			}
			if f.defaultedInputName != "" {
				if _, set := inputs[f.defaultedInputName]; !set {
					inputs[f.defaultedInputName] = "the-module-default"
				}
			}
			body["data"].(map[string]any)["attributes"].(map[string]any)["input-values"] = inputs
			return body
		}

		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/provision"):
			f.provisions++
			f.provisioned = decode()
			f.inputs = map[string]string{}
			for k, v := range mapOfStrings(f.provisioned["input-values"]) {
				f.inputs[k] = v
			}
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(provisionBody())
		case r.Method == http.MethodPatch:
			f.reconfigures++
			for k, v := range mapOfStrings(decode()["input-values"]) {
				f.inputs[k] = v
			}
			// A reconfigure answers with a RUN reference, not the instance.
			_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{
				"id": "run-1", "type": "runs", "attributes": map[string]any{"status": "pending"},
			}})
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/destroy"):
			f.destroys++
			_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{
				"id": "run-2", "type": "runs", "attributes": map[string]any{"status": "pending", "is-destroy": true},
			}})
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(readBody())
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func mapOfStrings(v any) map[string]string {
	out := map[string]string{}
	m, ok := v.(map[string]any)
	if !ok {
		return out
	}
	for k, raw := range m {
		if s, ok := raw.(string); ok {
			out[k] = s
		}
	}
	return out
}

// unknownAttrs names every attribute of a resource state that is still
// unknown. The framework does not police this -- Terraform CORE is what
// refuses an unknown after apply -- so an assertion here is the only thing
// standing in for core in a protocol-level test.
func unknownAttrs(t *testing.T, v tftypes.Value) []string {
	t.Helper()
	var m map[string]tftypes.Value
	if err := v.As(&m); err != nil {
		t.Fatalf("state: %v", err)
	}
	var out []string
	for name, av := range m {
		if !av.IsKnown() {
			out = append(out, name)
		}
	}
	return out
}

func TestACatalogInstanceOmittingTheComputedTrioAppliesAndSettles(t *testing.T) {
	fake := &fakeCatalogAPI{defaultedInputName: "name"}
	api := httptest.NewServer(fake.handler(t))
	defer api.Close()
	h := newProtoHarness(t, api.URL, "terrapod_catalog_instance")

	// The documented usage: version_pin, execution_backend and engine_version
	// are all absent, and so is input_values.
	cfg := h.config(map[string]tftypes.Value{
		"catalog_item_id": tftypes.NewValue(tftypes.String, "item-1"),
		"name":            tftypes.NewValue(tftypes.String, "web-tier"),
		"agent_pool_id":   tftypes.NewValue(tftypes.String, "apool-1"),
	})
	none := tftypes.NewValue(h.typ, nil)

	created := h.apply(none, cfg, h.plan(none, cfg))
	if unknown := unknownAttrs(t, created); len(unknown) != 0 {
		t.Fatalf("after apply the state still holds unknown %v -- Terraform refuses this", unknown)
	}
	if fake.provisions != 1 {
		t.Fatalf("%d provisions, want 1", fake.provisions)
	}
	if got := h.attr(created, "execution_backend"); got != "tofu" {
		t.Fatalf("execution_backend is %q, want the server's answer", got)
	}
	if got := h.attr(created, "engine_version"); got != "1.13" {
		t.Fatalf("engine_version is %q, want the server's answer", got)
	}

	// Refresh, then plan: nothing may change. This is where the input-values
	// superset showed up -- the server reports a `name` input no config set.
	state := h.refresh(created)
	if unknown := unknownAttrs(t, state); len(unknown) != 0 {
		t.Fatalf("after refresh the state holds unknown %v", unknown)
	}
	if pr := h.plan(state, cfg); !h.value(pr.PlannedState).Equal(state) {
		t.Fatalf("drift after refresh: the module's defaulted inputs leaked into state\n  state   %v\n  planned %v",
			state, h.value(pr.PlannedState))
	}
	if fake.reconfigures != 0 {
		t.Fatalf("%d reconfigures on a settled instance, want 0", fake.reconfigures)
	}
}

func TestACatalogInstanceReconfiguresAnInputAndSettles(t *testing.T) {
	fake := &fakeCatalogAPI{defaultedInputName: "name"}
	api := httptest.NewServer(fake.handler(t))
	defer api.Close()
	h := newProtoHarness(t, api.URL, "terrapod_catalog_instance")

	inputs := func(v string) tftypes.Value {
		return tftypes.NewValue(
			tftypes.Map{ElementType: tftypes.String},
			map[string]tftypes.Value{"size": tftypes.NewValue(tftypes.String, v)},
		)
	}
	config := func(v string) tftypes.Value {
		return h.config(map[string]tftypes.Value{
			"catalog_item_id": tftypes.NewValue(tftypes.String, "item-1"),
			"name":            tftypes.NewValue(tftypes.String, "web-tier"),
			"agent_pool_id":   tftypes.NewValue(tftypes.String, "apool-1"),
			"input_values":    inputs(v),
		})
	}
	none := tftypes.NewValue(h.typ, nil)

	cfg := config("small")
	state := h.refresh(h.apply(none, cfg, h.plan(none, cfg)))
	if pr := h.plan(state, cfg); !h.value(pr.PlannedState).Equal(state) {
		t.Fatalf("drift before the change: planned %v", h.value(pr.PlannedState))
	}

	cfg2 := config("large")
	pr := h.plan(state, cfg2)
	if len(pr.RequiresReplace) != 0 {
		t.Fatalf("changing an input forces a replace of %v", pr.RequiresReplace)
	}
	updated := h.apply(state, cfg2, pr)
	if unknown := unknownAttrs(t, updated); len(unknown) != 0 {
		t.Fatalf("after the update the state holds unknown %v", unknown)
	}
	if fake.reconfigures != 1 || fake.provisions != 1 || fake.destroys != 0 {
		t.Fatalf("writes: %d provision, %d reconfigure, %d destroy; want 1, 1, 0",
			fake.provisions, fake.reconfigures, fake.destroys)
	}
	if fake.inputs["size"] != "large" {
		t.Fatalf("the API holds size=%q after the reconfigure", fake.inputs["size"])
	}

	state = h.refresh(updated)
	if pr := h.plan(state, cfg2); !h.value(pr.PlannedState).Equal(state) {
		t.Fatalf("drift after the update: planned %v", h.value(pr.PlannedState))
	}
}
