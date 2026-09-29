package variable

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// `terraform` and `native` are two names for ONE category (#1898), and which
// one the server returns depends on the surface. This provider reads the
// compatibility surface, which answers `terraform` -- so a config that says
// `native` would be overwritten on every Read, and because `category` forces
// replacement the plan would propose destroying and recreating the variable
// over a difference that is not one.
//
// Perpetual drift on a RequiresReplace attribute is about the worst shape a
// provider bug takes: it is not an error, so nothing fails, and the proposal
// looks like a real change every time someone runs plan.
func TestTheConfiguredSpellingSurvivesARead(t *testing.T) {
	for _, configured := range []string{"terraform", "native", "pulumi_config"} {
		t.Run(configured, func(t *testing.T) {
			m := variableModel{Category: types.StringValue(configured)}
			// What the compatibility surface actually answers.
			readVariableIntoModel(&terrapod.Variable{ID: "var-1", Key: "region", Category: "terraform"}, &m)
			if got := m.Category.ValueString(); got != configured {
				t.Errorf("category = %q, want the configured %q — this is a perpetual diff", got, configured)
			}
		})
	}
}

func TestANativeSurfaceAnswerAlsoLeavesItAlone(t *testing.T) {
	// The mirror case: a future provider reading /api/v1 gets `native` back,
	// and a config saying `terraform` must survive that just as well.
	m := variableModel{Category: types.StringValue("terraform")}
	readVariableIntoModel(&terrapod.Variable{ID: "var-1", Key: "region", Category: "native"}, &m)
	if got := m.Category.ValueString(); got != "terraform" {
		t.Errorf("category = %q, want terraform", got)
	}
}

func TestAGenuineCategoryChangeIsStillDetected(t *testing.T) {
	// The tolerance must not swallow real drift: someone moving a variable
	// from the engine's parameters to the environment out of band is a change
	// Terraform has to see, and it is not an alias of anything.
	m := variableModel{Category: types.StringValue("terraform")}
	readVariableIntoModel(&terrapod.Variable{ID: "var-1", Key: "region", Category: "env"}, &m)
	if got := m.Category.ValueString(); got != "env" {
		t.Errorf("category = %q, want env — real drift was swallowed", got)
	}
}

func TestAnImportTakesWhateverTheServerSays(t *testing.T) {
	// `terraform import` populates the ID alone, so there is no configured
	// spelling to keep and the server's name is the only one available.
	var m variableModel
	readVariableIntoModel(&terrapod.Variable{ID: "var-1", Key: "region", Category: "terraform"}, &m)
	if got := m.Category.ValueString(); got != "terraform" {
		t.Errorf("category = %q, want terraform", got)
	}
}
