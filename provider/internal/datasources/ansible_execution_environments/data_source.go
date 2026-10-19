// Package ansible_execution_environments implements the
// terrapod_ansible_execution_environments data source (#2154): which Ansible
// execution environments this deployment can run.
//
// A workspace's `ansible_version` selects one of the built-in images and its
// `ansible_execution_environment` selects one the operator added, overriding
// the line. Both are validated server-side at apply, so without this data
// source the only way to find out which values are acceptable is to apply and
// read the 422 — and for the built-in half the answer is not static: the
// supported ansible-core range floats as upstream publishes new lines.
//
// # listing is load-bearing, not diagnostic
//
// The built-in set is read from a registry, so it can be unavailable. When it
// is, `listing` is "unknown" and the server ACCEPTS any line rather than
// refusing one it cannot confirm — otherwise an unreachable registry would
// refuse ordinary workspace edits. So a configuration that validates a line
// against this data source must treat "unknown" as "do not block", or it will
// fail applies the server would have allowed, for a reason that has nothing to
// do with the configuration.
package ansible_execution_environments

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &eeDataSource{}

type eeDataSource struct {
	tc *terrapod.Client
}

type eeModel struct {
	Environments types.List   `tfsdk:"environments"`
	Lines        types.List   `tfsdk:"lines"`
	Labels       types.List   `tfsdk:"labels"`
	Listing      types.String `tfsdk:"listing"`
}

// NewDataSource returns a new execution-environments data source.
func NewDataSource() datasource.DataSource {
	return &eeDataSource{}
}

func (d *eeDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_ansible_execution_environments"
}

func (d *eeDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The Ansible execution environments this deployment can run — the " +
			"built-in image per ansible-core line, and any the operator has added under a " +
			"label. Use it to validate or offer the values `terrapod_workspace`'s " +
			"`ansible_version` and `ansible_execution_environment` accept.",
		Attributes: map[string]schema.Attribute{
			"environments": schema.ListNestedAttribute{
				Computed: true,
				Description: "Every selectable environment, both kinds together. The " +
					"precedence between them is not symmetric — a label overrides a line — " +
					"so they are one list rather than two.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"id": schema.StringAttribute{
							Computed: true,
							Description: "The value a workspace would be set to: the " +
								"ansible-core line for a built-in, the label for an " +
								"operator-supplied one.",
						},
						"kind": schema.StringAttribute{
							Computed:    true,
							Description: "`built-in` or `operator`.",
						},
						"ansible_core_line": schema.StringAttribute{
							Computed: true,
							Description: "The ansible-core line, for a built-in. Empty for " +
								"an operator-supplied environment, whose ansible-core " +
								"version is the operator's own business.",
						},
						"label": schema.StringAttribute{
							Computed:    true,
							Description: "The label, for an operator-supplied environment. Empty for a built-in.",
						},
						"image": schema.StringAttribute{
							Computed: true,
							Description: "The image reference a run would execute in, " +
								"resolved by the server's own resolver — so it cannot drift " +
								"from what actually runs.",
						},
					},
				},
			},
			"lines": schema.ListAttribute{
				Computed:    true,
				ElementType: types.StringType,
				Description: "Just the built-in ansible-core lines, newest first — the " +
					"values `ansible_version` accepts. Convenience for a `contains()` check " +
					"or a `for_each`, but read `listing` first.",
			},
			"labels": schema.ListAttribute{
				Computed:    true,
				ElementType: types.StringType,
				Description: "Just the operator-supplied labels — the values " +
					"`ansible_execution_environment` accepts. Always authoritative: it comes " +
					"from the deployment's own configuration rather than from a registry.",
			},
			"listing": schema.StringAttribute{
				Computed: true,
				Description: "`complete` when the registry answered, so an absent line does " +
					"not exist and the server will refuse it. `unknown` when the set could " +
					"not be determined, in which case the server accepts ANY line — so a " +
					"configuration that gates on `lines` must not block on `unknown`, or it " +
					"will fail applies the server would have allowed. An empty `lines` with " +
					"`complete` is a real answer; with `unknown` it means nothing at all.",
			},
		},
	}
}

func (d *eeDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *eeDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config eeModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	available, err := d.tc.ListAnsibleExecutionEnvironments(ctx)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read Ansible execution environments", err.Error())
		return
	}

	objType := types.ObjectType{AttrTypes: map[string]attr.Type{
		"id":                types.StringType,
		"kind":              types.StringType,
		"ansible_core_line": types.StringType,
		"label":             types.StringType,
		"image":             types.StringType,
	}}

	objs := make([]attr.Value, 0, len(available.Environments))
	lines := make([]attr.Value, 0, len(available.Environments))
	labels := make([]attr.Value, 0, len(available.Environments))
	for _, e := range available.Environments {
		obj, diags := types.ObjectValue(objType.AttrTypes, map[string]attr.Value{
			"id":                types.StringValue(e.ID),
			"kind":              types.StringValue(e.Kind),
			"ansible_core_line": types.StringValue(e.AnsibleCoreLine),
			"label":             types.StringValue(e.Label),
			"image":             types.StringValue(e.Image),
		})
		resp.Diagnostics.Append(diags...)
		if resp.Diagnostics.HasError() {
			return
		}
		objs = append(objs, obj)
		switch e.Kind {
		case terrapod.KindBuiltIn:
			lines = append(lines, types.StringValue(e.AnsibleCoreLine))
		case terrapod.KindOperator:
			labels = append(labels, types.StringValue(e.Label))
		}
	}

	// Empty lists, never null: a practitioner running `contains(…, var.line)`
	// against a deployment with no operator environments should get `false`,
	// not an error about a null collection.
	envList, diags := types.ListValue(objType, objs)
	resp.Diagnostics.Append(diags...)
	lineList, diags := types.ListValue(types.StringType, lines)
	resp.Diagnostics.Append(diags...)
	labelList, diags := types.ListValue(types.StringType, labels)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}

	config.Environments = envList
	config.Lines = lineList
	config.Labels = labelList
	config.Listing = types.StringValue(available.Listing)

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
