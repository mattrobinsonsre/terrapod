package mcpserver

import (
	"io"
	"net/http"
	"testing"
)

// The self-contained catalog feature on the MCP catalog tools (#2372).
//
// TestToolCatalogue asserts these fields are DECLARED, and that is a presence
// check that is not enough: the input schema is built from the struct, so a
// field can be declared, advertised to an agent, accepted without complaint and
// dropped on the floor because nobody wrote it into the attrs map below. These
// assert the WIRE -- the same argument ansible_roots_tools_test.go makes for the
// configure roots, and the same defect the whole surface shipped with, which was
// that none of these fields existed at all.
//
// The properties are chosen for what each would corrupt rather than merely lose:
//
//   - the kebab-case SPELLING. The Go field is `self_contained` and the API
//     attribute is `self-contained`; a handler sending the snake form is
//     accepted by the router and silently ignored, so the item is published as
//     an ordinary wrapper while the agent is told it is self-contained.
//   - an explicit FALSE must travel. `SelfContained` is *bool for that reason;
//     a handler dropping a zero value could not turn the flag back off.
//   - a call mentioning NEITHER must send neither, so the server's own defaults
//     stand and an update does not blank a field it was not asked about.
//   - `unconstrain_execution_backends` must send an explicit NULL. The API
//     refuses an empty map (422), so null is the only clear, and a handler
//     sending `{}` would turn "unconstrain this item" into an error.
//   - provision and reconfigure must send the engine choice. Without it a
//     CONSTRAINED item is unprovisionable through MCP whenever the deployment's
//     default backend or version falls outside the item's declared range --
//     which is the case the range exists to express.

func echoItem(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/vnd.api+json")
	_, _ = io.WriteString(w, itemBody)
}

func TestItemCreateSendsTheSelfContainedAttributes(t *testing.T) {
	var attrs map[string]any
	sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoItem(w)
	})

	callTool(t, sess, "terrapod_catalog_item_create", map[string]any{
		"name":           "vpc",
		"module_id":      "11111111-1111-1111-1111-111111111111",
		"self_contained": true,
		"execution_backend_constraints": map[string]any{
			"tofu": ">=1.8,<2.0",
		},
	})

	if got, sent := attrs["self-contained"]; !sent || got != true {
		t.Errorf("create sent self-contained = %#v (sent=%v), want true", got, sent)
	}
	cons, sent := attrs["execution-backend-constraints"]
	if !sent {
		t.Fatal("create sent no execution-backend-constraints")
	}
	m, ok := cons.(map[string]any)
	if !ok || m["tofu"] != ">=1.8,<2.0" {
		t.Errorf("create sent execution-backend-constraints = %#v, want {tofu: >=1.8,<2.0}", cons)
	}
	// The snake spelling would be accepted by the router and ignored.
	if _, leaked := attrs["self_contained"]; leaked {
		t.Error("create sent the snake_case self_contained, which the API ignores")
	}
}

func TestAnExplicitFalseSelfContainedTravels(t *testing.T) {
	var attrs map[string]any
	sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoItem(w)
	})

	callTool(t, sess, "terrapod_catalog_item_update", map[string]any{
		"catalog_item_id": "11111111-1111-1111-1111-111111111111",
		"self_contained":  false,
	})

	got, sent := attrs["self-contained"]
	if !sent {
		t.Fatal("update dropped an explicit self_contained: false")
	}
	if got != false {
		t.Errorf("update sent self-contained = %#v, want false", got)
	}
}

func TestAnItemCallMentioningNeitherSendsNeither(t *testing.T) {
	var attrs map[string]any
	sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoItem(w)
	})

	callTool(t, sess, "terrapod_catalog_item_update", map[string]any{
		"catalog_item_id": "11111111-1111-1111-1111-111111111111",
		"description":     "just the description",
	})

	for _, k := range []string{"self-contained", "execution-backend-constraints"} {
		if _, sent := attrs[k]; sent {
			t.Errorf("update sent %s on a call that never mentioned it", k)
		}
	}
}

func TestUnconstrainSendsAnExplicitNull(t *testing.T) {
	var attrs map[string]any
	sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoItem(w)
	})

	callTool(t, sess, "terrapod_catalog_item_update", map[string]any{
		"catalog_item_id":                "11111111-1111-1111-1111-111111111111",
		"unconstrain_execution_backends": true,
	})

	got, sent := attrs["execution-backend-constraints"]
	if !sent {
		t.Fatal("unconstrain sent no execution-backend-constraints at all")
	}
	if got != nil {
		t.Errorf("unconstrain sent %#v, want an explicit null -- an empty map is refused 422", got)
	}
}

func TestProvisionAndReconfigureSendTheEngineChoice(t *testing.T) {
	for _, tc := range []struct {
		name, tool string
		args       map[string]any
		body       string
	}{
		{
			name: "provision",
			tool: "terrapod_catalog_provision",
			args: map[string]any{
				"catalog_item_id":   "11111111-1111-1111-1111-111111111111",
				"name":              "team-a-vpc",
				"agent_pool_id":     "apool-1",
				"execution_backend": "terraform",
				"engine_version":    "1.9.8",
			},
			body: instanceBody,
		},
		{
			name: "reconfigure",
			tool: "terrapod_catalog_instance_reconfigure",
			args: map[string]any{
				"workspace_id":      "ws-1",
				"execution_backend": "terraform",
				"engine_version":    "1.9.8",
			},
			body: runRefBody,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var attrs map[string]any
			body := tc.body
			sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
				attrs = writeAttrs(t, r)
				w.Header().Set("Content-Type", "application/vnd.api+json")
				_, _ = io.WriteString(w, body)
			})

			callTool(t, sess, tc.tool, tc.args)

			if got, sent := attrs["execution-backend"]; !sent || got != "terraform" {
				t.Errorf("%s sent execution-backend = %#v (sent=%v), want terraform", tc.name, got, sent)
			}
			if got, sent := attrs["engine-version"]; !sent || got != "1.9.8" {
				t.Errorf("%s sent engine-version = %#v (sent=%v), want 1.9.8", tc.name, got, sent)
			}
		})
	}
}

func TestAVariableOptionCarriesItsSensitiveFlag(t *testing.T) {
	var attrs map[string]any
	sess := catalogToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		attrs = writeAttrs(t, r)
		echoItem(w)
	})

	callTool(t, sess, "terrapod_catalog_item_create", map[string]any{
		"name":      "vpc",
		"module_id": "11111111-1111-1111-1111-111111111111",
		"variable_options": []any{
			map[string]any{"name": "db_password", "sensitive": true},
		},
	})

	vo, sent := attrs["variable-options"]
	if !sent {
		t.Fatal("create sent no variable-options")
	}
	list, ok := vo.([]any)
	if !ok || len(list) != 1 {
		t.Fatalf("create sent variable-options = %#v, want one entry", vo)
	}
	entry, ok := list[0].(map[string]any)
	if !ok {
		t.Fatalf("variable-options[0] = %#v, want an object", list[0])
	}
	if got, present := entry["sensitive"]; !present || got != true {
		t.Errorf("variable-options[0].sensitive = %#v (present=%v), want true", got, present)
	}
}
