// Package variable_set_workspace implements the terrapod_variable_set_workspace resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "workspaces" (in relationship payload)
//	Create:  POST   /api/v2/varsets/{varset_id}/relationships/workspaces
//	         body: {"data": [{"id": "ws-xxx", "type": "workspaces"}]}
//	Read:    GET    /api/v2/varsets/{varset_id}  (check included workspaces)
//	Delete:  DELETE /api/v2/varsets/{varset_id}/relationships/workspaces
//	         body: {"data": [{"id": "ws-xxx", "type": "workspaces"}]}
//
// No update — immutable junction resource (delete + recreate).
//
// Attributes:
//
//	varset_id    -> varset_id    (string, required, forces replace)
//	workspace_id -> workspace_id (string, required, forces replace)
//
// Import: varset_id/workspace_id
package variable_set_workspace

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

// variableSetWorkspaceModel maps the Terraform schema to Go types.
type variableSetWorkspaceModel struct {
	ID          types.String `tfsdk:"id"`
	VarsetID    types.String `tfsdk:"varset_id"`
	WorkspaceID types.String `tfsdk:"workspace_id"`
}

var (
	_ resource.Resource                = &variableSetWorkspaceResource{}
	_ resource.ResourceWithImportState = &variableSetWorkspaceResource{}
)

type variableSetWorkspaceResource struct {
	client *client.Client
}

// NewResource returns a new variable set workspace assignment resource.
func NewResource() resource.Resource {
	return &variableSetWorkspaceResource{}
}

func (r *variableSetWorkspaceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_variable_set_workspace"
}

func (r *variableSetWorkspaceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Assigns a Terrapod variable set to a workspace.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Composite ID (varset_id/workspace_id).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"varset_id": schema.StringAttribute{
				Required: true, Description: "Variable set ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"workspace_id": schema.StringAttribute{
				Required: true, Description: "Workspace ID to assign the variable set to.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
		},
	}
}

func (r *variableSetWorkspaceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c
}

func (r *variableSetWorkspaceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan variableSetWorkspaceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := marshalWorkspaceRelationship(plan.WorkspaceID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	_, err = r.client.Post(ctx, fmt.Sprintf("/api/v2/varsets/%s/relationships/workspaces", plan.VarsetID.ValueString()), body)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	plan.ID = types.StringValue(plan.VarsetID.ValueString() + "/" + plan.WorkspaceID.ValueString())
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *variableSetWorkspaceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state variableSetWorkspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Read the variable set and check if the workspace is in its relationships.
	data, err := r.client.Get(ctx, "/api/v2/varsets/"+state.VarsetID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	// Check if workspace_id exists in the "workspaces" relationship.
	if !hasWorkspaceRelationship(res, state.WorkspaceID.ValueString()) {
		resp.State.RemoveResource(ctx)
		return
	}

	state.ID = types.StringValue(state.VarsetID.ValueString() + "/" + state.WorkspaceID.ValueString())
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *variableSetWorkspaceResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	// Immutable resource — both attributes force replace. Update is never called.
	resp.Diagnostics.AddError("Update not supported", "Variable set workspace assignments are immutable. Changes require replacement.")
}

func (r *variableSetWorkspaceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state variableSetWorkspaceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := marshalWorkspaceRelationship(state.WorkspaceID.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	err = r.client.DeleteWithBody(ctx, fmt.Sprintf("/api/v2/varsets/%s/relationships/workspaces", state.VarsetID.ValueString()), body)
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *variableSetWorkspaceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import format: varset_id/workspace_id
	parts := strings.SplitN(req.ID, "/", 2)
	if len(parts) != 2 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: varset_id/workspace_id")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("varset_id"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("workspace_id"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
}

// marshalWorkspaceRelationship builds the JSON body for workspace relationship add/remove.
func marshalWorkspaceRelationship(workspaceID string) ([]byte, error) {
	payload := map[string]any{
		"data": []map[string]any{
			{
				"id":   workspaceID,
				"type": "workspaces",
			},
		},
	}
	return json.Marshal(payload)
}

// hasWorkspaceRelationship checks if a workspace ID exists in the varset's workspaces relationship.
func hasWorkspaceRelationship(res *client.Resource, workspaceID string) bool {
	rel, ok := res.Relationships["workspaces"]
	if !ok || len(rel.Data) == 0 || string(rel.Data) == "null" {
		return false
	}

	var items []client.RelationshipResource
	if err := json.Unmarshal(rel.Data, &items); err != nil {
		return false
	}

	for _, item := range items {
		if item.ID == workspaceID {
			return true
		}
	}
	return false
}
