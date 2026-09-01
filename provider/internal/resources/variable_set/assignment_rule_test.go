package variable_set

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// viaWire round-trips the payload through JSON, which is what actually happens
// between ToAPI and FromAPI in production: the map is marshalled, sent, stored,
// returned, and unmarshalled again — so a []string leaves as one and comes back
// as []any. Comparing the two functions in-process would test a path that never
// runs and quietly miss the type handling that does.
func viaWire(t *testing.T, payload map[string]any) map[string]any {
	t.Helper()
	if payload == nil {
		return nil
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}

// The failure this guards against is perpetual plan drift: any dimension the
// API stores but the provider drops on Read comes back absent every cycle, so
// Terraform proposes the same change forever. The two conversions must be exact
// inverses, and a round-trip is the only honest way to assert that.
func TestAssignmentRuleRoundTrips(t *testing.T) {
	original := &assignmentRuleModel{
		Labels:           map[string]string{"env": "prod", "team": "core"},
		NamePrefix:       types.StringValue("prod-"),
		NameGlob:         types.StringValue("*-api"),
		ExecutionBackend: types.StringValue("tofu"),
		ExecutionMode:    types.StringValue("agent"),
		TerraformVersion: types.StringValue("1.9.0"),
		AgentPoolID:      types.StringValue("apool-1"),
		VCSConnectionID:  types.StringValue("vcs-1"),
		OwnerEmail:       types.StringValue("owner@example.com"),
		DriftStatus:      types.StringValue("drifted"),
		Locked:           types.BoolValue(true),
		HasVCS:           types.BoolValue(false),
	}

	got := assignmentRuleFromAPI(viaWire(t, assignmentRuleToAPI(original)))
	if got == nil {
		t.Fatal("round trip lost the whole rule")
	}
	if !reflect.DeepEqual(original, got) {
		t.Errorf("round trip changed the rule:\n want %+v\n  got %+v", original, got)
	}
}

// Every field is covered above only if the model has no field the test forgot.
// Reflection makes that structural rather than a promise: adding a dimension to
// the model without adding it here fails, which is when the drift would
// otherwise be introduced.
func TestEveryRuleDimensionSurvivesTheRoundTrip(t *testing.T) {
	original := &assignmentRuleModel{
		Labels:           map[string]string{"env": "prod"},
		NamePrefix:       types.StringValue("p"),
		NameGlob:         types.StringValue("g"),
		ExecutionBackend: types.StringValue("tofu"),
		ExecutionMode:    types.StringValue("agent"),
		TerraformVersion: types.StringValue("1.9.0"),
		AgentPoolID:      types.StringValue("apool-1"),
		VCSConnectionID:  types.StringValue("vcs-1"),
		OwnerEmail:       types.StringValue("o@e.com"),
		DriftStatus:      types.StringValue("drifted"),
		Locked:           types.BoolValue(true),
		HasVCS:           types.BoolValue(true),
	}
	v := reflect.ValueOf(*original)
	for i := 0; i < v.NumField(); i++ {
		if v.Field(i).IsZero() {
			t.Fatalf("field %s is unset in this fixture, so the round trip does not cover it",
				v.Type().Field(i).Name)
		}
	}

	api := assignmentRuleToAPI(original)
	if len(api) != v.NumField() {
		t.Errorf("model has %d dimensions but only %d reached the API payload: %v",
			v.NumField(), len(api), api)
	}
}

func TestAnEmptyRuleIsNilNotAnEmptyObject(t *testing.T) {
	// An empty filter is rejected by the API, and would mean "every workspace"
	// if it were not. Sending nothing is the only safe encoding of "no rule".
	if got := assignmentRuleToAPI(&assignmentRuleModel{}); got != nil {
		t.Errorf("want nil for an empty rule, got %v", got)
	}
	if got := assignmentRuleToAPI(nil); got != nil {
		t.Errorf("want nil for a nil rule, got %v", got)
	}
	if got := assignmentRuleFromAPI(map[string]any{}); got != nil {
		t.Errorf("want nil model for an empty payload, got %+v", got)
	}
}

func TestAPartialRuleOnlyCarriesWhatWasSet(t *testing.T) {
	got := assignmentRuleToAPI(&assignmentRuleModel{
		Labels: map[string]string{"env": "prod"},
	})
	if len(got) != 1 || got["labels"] == nil {
		t.Errorf("unset dimensions leaked into the payload: %v", got)
	}
}

// The conversions above being correct proves nothing if nothing calls them.
// These cover the wiring — the three functions that sit between the Terraform
// model and the SDK — because a helper that is perfect but unreached would let
// every test here pass while the feature does nothing.

func TestCreateRequestCarriesTheRule(t *testing.T) {
	req := buildCreateVarsetRequest(&variableSetModel{
		Name:           types.StringValue("vs"),
		AssignmentRule: &assignmentRuleModel{Labels: map[string]string{"env": "prod"}},
	})
	if req.AssignmentRule == nil {
		t.Fatal("create dropped the rule")
	}
	if _, ok := req.AssignmentRule["labels"]; !ok {
		t.Errorf("create sent a rule without its labels: %v", req.AssignmentRule)
	}
}

func TestCreateWithoutARuleSendsNone(t *testing.T) {
	req := buildCreateVarsetRequest(&variableSetModel{Name: types.StringValue("vs")})
	if req.AssignmentRule != nil {
		t.Errorf("a config with no rule must not send one: %v", req.AssignmentRule)
	}
}

func TestUpdateAlwaysSendsTheRuleSoRemovalTakesEffect(t *testing.T) {
	// Removing the block from the config has to clear the rule on the server.
	// If update only sent the field when set, deleting it from HCL would leave
	// the set quietly matching workspaces the operator no longer intends.
	req := buildUpdateVarsetRequest(&variableSetModel{Name: types.StringValue("vs")})
	if req.AssignmentRule == nil {
		t.Fatal("update must always send the rule field, even to clear it")
	}
	if *req.AssignmentRule != nil {
		t.Errorf("removing the block must send an empty rule, got %v", *req.AssignmentRule)
	}

	req = buildUpdateVarsetRequest(&variableSetModel{
		Name:           types.StringValue("vs"),
		AssignmentRule: &assignmentRuleModel{NameGlob: types.StringValue("*-prod")},
	})
	if req.AssignmentRule == nil || (*req.AssignmentRule)["name_glob"] != "*-prod" {
		t.Errorf("update dropped the rule: %v", req.AssignmentRule)
	}
}

func TestReadPopulatesTheRuleFromTheAPI(t *testing.T) {
	// Read is where drift would appear: a rule the server holds but Read does
	// not populate reads back as absent, and Terraform proposes the same change
	// on every plan, forever.
	var m variableSetModel
	readVarsetFromSDK(&terrapod.VariableSet{
		ID:             "varset-1",
		Name:           "vs",
		AssignmentRule: map[string]any{"labels": map[string]any{"env": "prod"}},
	}, &m)
	if m.AssignmentRule == nil {
		t.Fatal("read dropped the rule — this is perpetual plan drift")
	}
	if m.AssignmentRule.Labels["env"] != "prod" {
		t.Errorf("read lost the rule contents: %+v", m.AssignmentRule)
	}

	var empty variableSetModel
	readVarsetFromSDK(&terrapod.VariableSet{ID: "varset-2", Name: "vs"}, &empty)
	if empty.AssignmentRule != nil {
		t.Errorf("a set with no rule must read back as nil, got %+v", empty.AssignmentRule)
	}
}
