package variable

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// Each phase flag reaches the server on a create and comes back on a read.
//
// A model field with a tfsdk tag and no assignment is the shape to guard
// against: it declares the attribute, accepts it in HCL, shows it in the
// schema golden, and silently never leaves the provider. Nothing structural
// catches that, so the mapping functions are driven directly.
func TestThePhaseFlagsSurviveCreateAndRead(t *testing.T) {
	for _, tc := range []struct {
		name                     string
		applyOnly, configureOnly bool
	}{
		{"ordinary", false, false},
		{"apply-only", true, false},
		{"configure-only", false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := variableModel{
				Key:           types.StringValue("ANSIBLE_BECOME_PASS"),
				Category:      types.StringValue("env"),
				ApplyOnly:     types.BoolValue(tc.applyOnly),
				ConfigureOnly: types.BoolValue(tc.configureOnly),
			}

			req := buildCreateVariableRequest(&m)
			if req.ApplyOnly != tc.applyOnly {
				t.Errorf("create sends apply-only=%v, want %v", req.ApplyOnly, tc.applyOnly)
			}
			if req.ConfigureOnly != tc.configureOnly {
				t.Errorf("create sends configure-only=%v, want %v", req.ConfigureOnly, tc.configureOnly)
			}

			var back variableModel
			readVariableIntoModel(&terrapod.Variable{
				ID: "var-1", Key: "ANSIBLE_BECOME_PASS", Category: "env",
				ApplyOnly: tc.applyOnly, ConfigureOnly: tc.configureOnly,
			}, &back)
			if back.ApplyOnly.ValueBool() != tc.applyOnly {
				t.Errorf("read back apply_only=%v, want %v", back.ApplyOnly.ValueBool(), tc.applyOnly)
			}
			if back.ConfigureOnly.ValueBool() != tc.configureOnly {
				t.Errorf("read back configure_only=%v, want %v", back.ConfigureOnly.ValueBool(), tc.configureOnly)
			}
		})
	}
}

// The update request is the partial-update shape, so each flag is a pointer:
// nil leaves the server's value alone and a set pointer changes it. Sending
// nil for a flag the operator set would make the attribute unmanageable after
// create -- it could be declared once and never turned off.
func TestThePhaseFlagsAreSentOnAnUpdate(t *testing.T) {
	m := variableModel{
		Key:           types.StringValue("ANSIBLE_BECOME_PASS"),
		Category:      types.StringValue("env"),
		ApplyOnly:     types.BoolValue(false),
		ConfigureOnly: types.BoolValue(true),
	}
	req := buildUpdateVariableRequest(&m)
	if req.ApplyOnly == nil {
		t.Error("update omits apply-only, so it can never be turned off")
	} else if *req.ApplyOnly {
		t.Error("update sends apply-only=true, want false")
	}
	if req.ConfigureOnly == nil {
		t.Error("update omits configure-only, so it can never be turned on")
	} else if !*req.ConfigureOnly {
		t.Error("update sends configure-only=false, want true")
	}
}

// An unset flag is omitted from the update body entirely, so an import or a
// config that never mentions it does not write a value the operator did not
// choose.
func TestAnUnsetPhaseFlagIsOmittedFromAnUpdate(t *testing.T) {
	m := variableModel{
		Key:           types.StringValue("TOKEN"),
		Category:      types.StringValue("env"),
		ApplyOnly:     types.BoolNull(),
		ConfigureOnly: types.BoolUnknown(),
	}
	req := buildUpdateVariableRequest(&m)
	if req.ApplyOnly != nil {
		t.Errorf("a null apply_only was sent as %v", *req.ApplyOnly)
	}
	if req.ConfigureOnly != nil {
		t.Errorf("an unknown configure_only was sent as %v", *req.ConfigureOnly)
	}
	if c := buildCreateVariableRequest(&m); c.ApplyOnly || c.ConfigureOnly {
		t.Errorf("create sent apply-only=%v configure-only=%v for unset flags",
			c.ApplyOnly, c.ConfigureOnly)
	}
}
