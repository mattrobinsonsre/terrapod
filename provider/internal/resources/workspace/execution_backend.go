package workspace

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// pulumiEngine is the `engine` value for Pulumi workspaces, matching the
// column the server stores.
const pulumiEngine = "pulumi"

// terraformBackendDefault is the value the provider has always planned for
// `execution_backend` when a configuration does not set one. It is NOT the
// server's default (that is the deployment-wide `default_execution_backend`,
// `tofu` out of the box) — the provider has picked `terraform` since the
// attribute existed, and changing that would silently repoint every
// provider-managed workspace's binary on the next apply.
const terraformBackendDefault = "terraform"

// executionBackendPlan decides the planned `execution_backend` for a workspace,
// returning ("", false) to leave the framework's own planned value alone.
//
// `execution_backend` picks tofu vs terraform WITHIN the Terraform engine. It
// has no meaning on a Pulumi workspace: there is one binary, and the Pulumi
// engine strategy never reads the column. Until #1911 the attribute carried an
// unconditional schema default, so every provider-managed Pulumi workspace was
// created asserting `execution_backend = "terraform"` — a value the operator
// never wrote, about a choice their engine does not offer.
//
// This reproduces that default exactly for every engine but Pulumi, so no
// Terraform workspace sees a change; for Pulumi it plans nothing and lets the
// server assign, which is what the provider should have been doing.
//
// It deliberately does not REFUSE a configured `execution_backend` on a Pulumi
// workspace. The server accepts one on any engine — create stores whatever it
// is given, update validates it is terraform-or-tofu and nothing more — so an
// error here would be the provider taking a position the API does not, and
// would break a configuration that applies cleanly today. The plan modifier
// warns instead.
func executionBackendPlan(config, engine types.String) (string, bool) {
	// The operator said what they wanted; it stands, whatever the engine.
	if !config.IsNull() && !config.IsUnknown() {
		return "", false
	}
	if isPulumiEngine(engine) {
		return "", false
	}
	return terraformBackendDefault, true
}

// executionBackendIsMeaningless reports whether a configuration has set
// `execution_backend` on a workspace whose engine does not have one.
func executionBackendIsMeaningless(config, engine types.String) bool {
	if config.IsNull() || config.IsUnknown() {
		return false
	}
	return isPulumiEngine(engine)
}

// isPulumiEngine answers only for an engine value that is actually KNOWN.
//
// An unknown engine is treated as not-Pulumi on purpose: that is the ambiguous
// case (a create whose `engine` is itself computed), and the safe direction is
// the long-standing Terraform behaviour rather than a silent change to it. A
// workspace cannot become Pulumi by accident — `engine = "pulumi"` has to be
// written in the configuration — so the value is known wherever it matters.
func isPulumiEngine(engine types.String) bool {
	if engine.IsNull() || engine.IsUnknown() {
		return false
	}
	return engine.ValueString() == pulumiEngine
}

// engineAwareBackendDefault supplies `execution_backend`'s default only for the
// engines that have one, replacing the unconditional schema default (#1911).
//
// It has to be a plan modifier rather than a `Default`: a default cannot see the
// rest of the configuration, and the answer here depends on `engine`.
type engineAwareBackendDefault struct{}

func (engineAwareBackendDefault) Description(_ context.Context) string {
	return "Defaults to \"terraform\" on the Terraform engine; left to the server on Pulumi, where the attribute has no meaning."
}

func (m engineAwareBackendDefault) MarkdownDescription(ctx context.Context) string {
	return m.Description(ctx)
}

func (engineAwareBackendDefault) PlanModifyString(ctx context.Context, req planmodifier.StringRequest, resp *planmodifier.StringResponse) {
	// No plan to modify on destroy.
	if req.Plan.Raw.IsNull() {
		return
	}
	var engine types.String
	// The plan's engine, not the config's: on update the configuration may omit
	// it entirely while the workspace has been Pulumi all along, and reading
	// config would make this fall back to the Terraform branch on exactly the
	// workspaces it exists for.
	if diags := req.Plan.GetAttribute(ctx, path.Root("engine"), &engine); diags.HasError() {
		resp.Diagnostics.Append(diags...)
		return
	}

	if executionBackendIsMeaningless(req.ConfigValue, engine) {
		resp.Diagnostics.AddAttributeWarning(
			path.Root("execution_backend"),
			"execution_backend has no meaning on a Pulumi workspace",
			"`execution_backend` chooses between the `tofu` and `terraform` binaries "+
				"within the Terraform engine. Pulumi has one binary, so this workspace "+
				"stores the value and never uses it.\n\n"+
				"The setting is accepted rather than rejected because the API accepts "+
				"it too; remove it to stop recording a choice the engine does not offer.",
		)
	}

	if v, ok := executionBackendPlan(req.ConfigValue, engine); ok {
		resp.PlanValue = types.StringValue(v)
	}
}
